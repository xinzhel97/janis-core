import copy
import os
from abc import abstractmethod
from typing import List, Union, Optional, Dict, Tuple, Any

from janis_core.graph.node import Node, NodeTypes
from janis_core.graph.steptaginput import StepTagInput
from janis_core.tool.commandtool import CommandTool
from janis_core.tool.tool import Tool, ToolType, ToolTypes, TInput, TOutput
from janis_core.translationdeps.exportpath import ExportPathKeywords
from janis_core.translationdeps.supportedtranslations import SupportedTranslation
from janis_core.types import (
    DataType,
    ParseableType,
    get_instantiated_type,
    Array,
    InputSelector,
    Filename,
    Operator,
    StepOperator,
    InputOperator,
    AndOperator,
    OrOperator,
    NotOperator,
    or_prev_conds,
    SingleValueOperator,
    TwoValueOperator,
    StringFormatter,
)
from janis_core.types.data_types import is_python_primitive
from janis_core.utils import first_value
from janis_core.utils.logger import Logger
from janis_core.utils.metadata import WorkflowMetadata
from janis_core.utils.pickvalue import PickValue
from janis_core.utils.scatter import ScatterDescription, ScatterMethods
from janis_core.utils.validators import Validators

ConnectionSource = Union[Node, StepOperator, Tuple[Node, str]]


def verify_or_try_get_source(
    source: Union[ConnectionSource, List[ConnectionSource]]
) -> Union[StepOperator, List[StepOperator]]:

    if isinstance(source, StepOperator):
        return source
    if isinstance(source, InputOperator):
        return verify_or_try_get_source(source.input_node)
    if isinstance(source, list):
        return [verify_or_try_get_source(s) for s in source]
    node, tag = None, None
    if isinstance(source, tuple):
        node, tag = source
    else:
        node = source

    outs = node.outputs()
    if tag is None:
        if len(outs) > 1:
            raise Exception(
                f"Too many outputs of {node.id()} to guess the correct output"
            )
        tag = list(outs.keys())[0]

    if tag not in outs:
        tags = ", ".join([f"out.{o}" for o in outs.keys()])
        raise Exception(
            f"Couldn't find tag '{tag}' in outputs of '{node.id()}', "
            f"expected one of {tags}"
        )

    return StepOperator(node, tag)


class InputNode(Node):
    def __init__(
        self,
        wf,
        identifier: str,
        datatype: DataType,
        default: any,
        value: any,
        doc: str = None,
    ):
        super().__init__(wf, NodeTypes.INPUT, identifier)
        self.datatype = datatype
        self.default = default
        self.doc = doc
        self.value = value

    def as_operator(self):
        return InputOperator(self)

    def outputs(self) -> Dict[Optional[str], TOutput]:
        # Program will just grab first value anyway
        return {None: TOutput(self.identifier, self.datatype)}

    def inputs(self):
        return None


class StepNode(Node):
    def __init__(
        self,
        wf,
        identifier,
        tool: Tool,
        doc: str = None,
        scatter: ScatterDescription = None,
        when: Operator = None,
    ):
        super().__init__(wf, NodeTypes.STEP, identifier)
        self.tool = tool
        self.doc = doc
        self.scatter = scatter
        self.when = when

        self.parent_has_conditionals = False
        self.has_conditionals = when is not None

    def inputs(self) -> Dict[str, TInput]:
        ins = self.tool.inputs_map()

        if self.parent_has_conditionals or self.has_conditionals:
            q = {}
            for iv in ins.values():
                intype = copy.copy(iv.intype)
                intype.optional = True
                q[iv.id()] = TInput(
                    iv.id(), intype=intype, doc=iv.doc, default=iv.default
                )

            ins = q

        return ins

    def outputs(self) -> Dict[str, TOutput]:
        outs = self.tool.outputs_map()

        if self.parent_has_conditionals or self.has_conditionals:
            q = {}
            for ov in outs.values():
                outtype = copy.copy(ov.outtype)
                outtype.optional = True
                q[ov.id()] = TOutput(ov.id(), outtype=outtype, doc=ov.doc)

            outs = q

        return outs

    def _add_edge(self, tag: str, source: ConnectionSource):
        stepoperator = verify_or_try_get_source(source)
        node, outtag = stepoperator.node, stepoperator.tag

        if isinstance(node, StepNode):
            if node.has_conditionals or node.parent_has_conditionals:
                self.parent_has_conditionals = True

        if tag not in self.sources:
            self.sources[tag] = StepTagInput(self, tag)

        # If tag is in scatter.fields, then we can
        scatter = self.scatter and tag in self.scatter.fields

        return self.sources[tag].add_source(node, outtag, should_scatter=scatter)

    def __getattr__(self, item):
        if item in self.__dict__:
            return self.__dict__[item]

        return self.get_item(item)

    def __getitem__(self, item) -> StepOperator:
        return self.get_item(item)

    def get_item(self, item) -> StepOperator:
        ins = self.inputs()
        if item in ins:
            return StepOperator(self, item)
        outs = self.outputs()
        if item in outs:
            return StepOperator(self, item)

        tags = ", ".join(
            [f"in.{i}" for i in ins.keys()] + [f"out.{o}" for o in outs.keys()]
        )

        raise KeyError(
            f"Step '{self.id()}' with tool '{self.tool.id()}' has no identifier '{item}' ({tags})"
        )

    # "always set attributes keys"
    always_set = {"tool", "sources", "node_type", "identifier"}

    def __setitem__(self, key, value):
        if key not in StepNode.always_set and key in self.inputs():
            if isinstance(value, list):
                return [self._add_edge(key, v) for v in value]
            return self._add_edge(key, value)

        self.__dict__[key] = value


class OutputNode(Node):
    def __init__(
        self,
        wf,
        identifier: str,
        datatype: DataType,
        source: Union[List[ConnectionSource], ConnectionSource],
        doc: str = None,
        output_folder: Union[
            str, InputSelector, List[Union[str, InputSelector]]
        ] = None,
        output_name: Union[str, InputSelector] = None,
        pick_value: PickValue = None,
    ):
        super().__init__(wf, NodeTypes.OUTPUT, identifier)
        self.datatype = datatype

        sources = source if isinstance(source, list) else [source]
        single_source = sources[0]

        invalid_nodetypes = list(
            s for s in sources if s.node.node_type != NodeTypes.STEP
        )
        if len(invalid_nodetypes) > 0:
            raise Exception(
                f"Unsupported connection type: {NodeTypes.OUTPUT} → {', '.join(str(s[0].node_type) for s in sources)}"
            )

        snode = single_source.node
        stype = snode.outputs()[single_source.tag].outtype

        # todo: verify how a scatter on multiple input nodes should be handled, likely an error if they're different
        if isinstance(snode, StepNode) and snode.scatter:
            stype = Array(stype)

        if not datatype.can_receive_from(stype):
            Logger.critical(
                f"Mismatch of types when joining to output node '{source.node.id()}.{source.tag}' to '{identifier}' "
                f"({stype.id()} -/→ {datatype.id()})"
            )

        self.source = verify_or_try_get_source(source)
        self.doc = doc
        self.output_folder = output_folder
        self.output_name = output_name
        self.pick_value = pick_value

    def inputs(self) -> Dict[str, TInput]:
        # Program will just grab first value anyway
        return {None: TInput(self.identifier, self.datatype)}

    def outputs(self):
        return None


class Workflow(Tool):
    def __init__(self, **connections):
        super().__init__(metadata_class=WorkflowMetadata)

        self.connections = connections

        Logger.log(f"Creating workflow with identifier: '{self.id()}'")

        if not Validators.validate_identifier(self.id()):
            raise Exception(
                f"The identifier '{self.id()}' was invalid because {Validators.reason_for_failure(self.id())}"
            )

        # The following variables allow us to quickly check data about the graph
        self.nodes: Dict[str, Node] = {}

        self.input_nodes: Dict[str, InputNode] = {}
        self.step_nodes: Dict[str, StepNode] = {}
        self.output_nodes: Dict[str, OutputNode] = {}

        # Flags for different requirements that a workflow might need
        self.has_scatter = False
        self.has_subworkflow = False
        self.has_multiple_inputs = False

        # Now that we've initialised everything, we can "construct" the workflows for that subclass this class
        # else, for the WorkflowBuilder it will do nothing and they'll add workflows later
        self.constructor()

    @abstractmethod
    def friendly_name(self):
        pass

    @abstractmethod
    def constructor(self):
        """
        A place to construct your workflows. This is called directly after initialisation.
        :return:
        """
        pass

    def verify_identifier(self, identifier: str, component: str):

        if identifier in self.__dict__:
            raise Exception(
                f"'{identifier}' is a protected keyword for a janis workflow"
            )

        if identifier in self.nodes:
            existing = self.nodes[identifier]
            raise Exception(
                f"There already exists a node (and component) with id '{identifier}'. The added "
                f"component ('{component}') clashes with '{repr(existing)}')."
            )

        if not Validators.validate_identifier(identifier):
            raise Exception(
                f"The identifier '{identifier}' was invalid because {Validators.reason_for_failure(identifier)}"
            )

    def input(
        self,
        identifier: str,
        datatype: ParseableType,
        default: any = None,
        value: any = None,
        doc: str = None,
    ):
        """
        Create an input node on a workflow
        :return:
        """

        self.verify_identifier(identifier, repr(datatype))

        datatype = get_instantiated_type(datatype)
        if default:
            datatype.optional = True

        inp = InputNode(
            self,
            identifier=identifier,
            datatype=datatype,
            default=default,
            doc=doc,
            value=value,
        )
        self.nodes[identifier] = inp
        self.input_nodes[identifier] = inp
        return inp

    def output(
        self,
        identifier: str,
        datatype: Optional[ParseableType] = None,
        source: Union[
            List[Union[StepNode, ConnectionSource]], Union[StepNode, ConnectionSource]
        ] = None,
        output_folder: Union[
            str,
            InputSelector,
            ConnectionSource,
            List[Union[str, InputSelector, ConnectionSource]],
        ] = None,
        output_name: Union[str, InputSelector, ConnectionSource] = None,
        pick_value: PickValue = None,
    ):
        """
        Create an output on a workflow

        :param identifier: The identifier for the output
        :param datatype: Optional data type of the output to check. This will be automatically inferred if not provided.
        :param source: The source of the output, must be an output to a step node
        :param output_folder: A janis annotation for grouping outputs by this value.  If a list is passed, it represents
        a structure of nested directories, the first element being the root directory.
        At most, one InputSelector can resolve to an array, and this behaviour is only defined if the output
        scattered source, and the number of elements is equal.
        :param output_name: Decides the prefix that an output will have, or acts as a map if the InputSelector
        resolves to an array with equal length to the number of shards (scatters). Any other behaviour is defined and
        may result in an unexpected termination.
        :param pick_value: A pick value describes how to pick which value (if source is an array) should be used
        :return:
        """
        self.verify_identifier(identifier, repr(datatype))

        if source is None:
            raise Exception("Output source must not be 'None'")

        stepoperators = verify_or_try_get_source(source)
        if isinstance(stepoperators, list):
            # better be a pick_value
            if not pick_value:
                pick_value = PickValue.first_non_null
                Logger.warn(
                    "janis.output had source=List, but didn't provide a pick_value, defaulting to first_non_null"
                )

            stepoperator = stepoperators[0]
        else:
            stepoperator = stepoperators

        node, tag = stepoperator.node, stepoperator.tag

        if not datatype:
            datatype: DataType = node.outputs()[tag].outtype.received_type()

            if isinstance(node, StepNode) and node.scatter:
                datatype = Array(datatype)

            if pick_value and pick_value == PickValue.all_non_null:
                datatype = Array(datatype)

        if output_name:
            if isinstance(output_name, list):
                raise Exception("An output_name cannot be of type 'list'")
            output_name = self.verify_output_source_type(
                identifier, output_name, "output_name"
            )
        if output_folder:
            ot = output_folder if isinstance(output_folder, list) else [output_folder]
            output_folder = self.verify_output_source_type(
                identifier, ot, "output_folder"
            )

        otp = OutputNode(
            self,
            identifier=identifier,
            datatype=get_instantiated_type(datatype),
            source=stepoperators,
            output_folder=output_folder,
            output_name=output_name,
            pick_value=pick_value,
        )
        self.nodes[identifier] = otp
        self.output_nodes[identifier] = otp
        return otp

    def all_input_keys(self):
        from janis_core.translations.translationbase import TranslatorBase

        return super().all_input_keys() + list(
            TranslatorBase.build_resources_input(workflow=self, hints={}).keys()
        )

    def verify_output_source_type(
        self,
        identifier,
        out: Union[
            str,
            InputSelector,
            ConnectionSource,
            List[Union[str, InputSelector, ConnectionSource]],
        ],
        outtype: str,
    ):
        if isinstance(out, list):
            return [self.verify_output_source_type(identifier, o, outtype) for o in out]

        if isinstance(out, str):
            return out

        if isinstance(out, tuple):
            # ConnectionSource tuple
            out = out[0]

        if isinstance(out, Node):
            if not isinstance(out, InputNode):
                raise Exception(
                    f"The source for the {outtype} '{identifier}' was a {out.__class__.__name__} and must be an Input"
                )

            return InputSelector(out.identifier)

        if isinstance(out, InputSelector):
            keys = set(self.input_nodes.keys())
            if out.input_to_select not in keys:
                raise Exception(
                    f"Couldn't find the input {out.input_to_select} in the workflow, expected one of: "
                    + ", ".join(keys)
                )
            return out

        raise Exception(f"Invalidate type for {outtype}: {out.__class__.__name__}")

    def step(
        self,
        identifier: str,
        tool: Tool,
        scatter: Union[str, List[str], ScatterDescription] = None,
        when: Optional[Operator] = None,
        ignore_missing=False,
    ):
        """
        Construct a step on this workflow.

        :param identifier: The identifier of the step, unique within the workflow.
        :param tool: The tool that should run for this step.
        :param scatter: Indicate whether a scatter should occur, on what, and how.
        :type scatter: Union[str, ScatterDescription]
        :param when: An operator / condition that determines whether the step should run
        : type when: Optional[Operator]
        :param ignore_missing: Don't throw an error if required params are missing from this function
        :return:
        """

        self.verify_identifier(identifier, tool.id())

        if scatter is not None and not isinstance(scatter, ScatterDescription):

            fields = None
            if isinstance(scatter, str):
                fields = [scatter]
            elif isinstance(scatter, list):
                fields = scatter
            else:
                raise Exception(
                    f"Couldn't scatter with field '{scatter}' ({type(scatter)}"
                )

            scatter = ScatterDescription(fields, method=ScatterMethods.dot)

        # verify scatter
        if scatter:
            ins = set(tool.inputs_map().keys())
            fields = set(scatter.fields)
            if any(f not in ins for f in fields):
                # if there is a field not in the input map, we have a problem
                extra_keys = ", ".join(f"'{f}'" for f in (fields - ins))
                raise Exception(
                    f"Couldn't scatter the field(s) for step '{identifier}' "
                    f"(tool: '{tool}') {extra_keys} as they were not found in the input map"
                )

        tool.workflow = self
        inputs = tool.inputs_map()

        connections = tool.connections

        provided_keys = set(connections.keys())
        all_keys = set(inputs.keys())
        required_keys = set(
            # The input is optional if it's optional or has default)
            i
            for i, v in inputs.items()
            if not (v.intype.optional or v.default is not None)
        )

        if not provided_keys.issubset(all_keys):
            unrecparams = ", ".join(provided_keys - all_keys)

            tags = ", ".join([f"in.{i}" for i in all_keys])

            raise Exception(
                f"Unrecognised parameters {unrecparams} when creating '{identifier}' ({tool.id()}). "
                f"Expected types: {tags}"
            )

        if not ignore_missing and not required_keys.issubset(provided_keys):
            missing = ", ".join(f"'{i}'" for i in (required_keys - provided_keys))
            raise Exception(
                f"Missing the parameters {missing} when creating '{identifier}' ({tool.id()})"
            )

        stp = StepNode(
            self, identifier=identifier, tool=tool, scatter=scatter, when=when
        )

        added_edges = []
        for (k, v) in connections.items():

            isfilename = isinstance(v, Filename)
            if is_python_primitive(v) or isfilename:
                inp_identifier = f"{identifier}_{k}"
                referencedtype = copy.copy(inputs[k].intype) if not isfilename else v
                parsed_type = get_instantiated_type(v)

                if parsed_type and not referencedtype.can_receive_from(parsed_type):
                    raise TypeError(
                        f"The type {parsed_type.id()} inferred from the value '{v}' is not "
                        f"compatible with the '{identifier}.{k}' type: {referencedtype.id()}"
                    )

                referencedtype.optional = True

                v = self.input(
                    inp_identifier,
                    referencedtype,
                    default=v.generated_filename() if isfilename else v,
                )
            if v is None:
                inp_identifier = f"{identifier}_{k}"
                v = self.input(inp_identifier, inputs[k].intype, default=v)

            verifiedsource = verify_or_try_get_source(v)
            if isinstance(verifiedsource, list):
                for vv in verifiedsource:
                    added_edges.append(stp._add_edge(k, vv))
            else:
                added_edges.append(stp._add_edge(k, verifiedsource))

        for e in added_edges:

            si = e.finish.sources[e.ftag] if e.ftag else first_value(e.finish.sources)
            self.has_multiple_inputs = self.has_multiple_inputs or si.multiple_inputs

        self.has_scatter = self.has_scatter or scatter is not None
        self.has_subworkflow = self.has_subworkflow or isinstance(tool, Workflow)
        self.nodes[identifier] = stp
        self.step_nodes[identifier] = stp

        return stp

    def switch(self, stepid: str, conditions: List[Union[Tuple[Operator, Tool], Tool]]):
        if len(conditions) <= 1:
            raise Exception("A switch statement must include at least 2 conditions")
        # validate tools
        tools = []
        for i in range(len(conditions)):
            con = conditions[i]
            if isinstance(con, tuple):
                tools.append(con[1])
            else:
                if i < (len(conditions) - 1):
                    raise Exception(
                        "A default statement (no condition) must be the last element in the switch"
                    )
                tools.append(con)

        # check output schema
        compare_schema = tools[0].outputs_map()
        in_keys = set(compare_schema.keys())
        non_matching_tools = []
        for i in range(1, len(tools)):
            tool = tools[i]
            outs = tool.outputs_map()
            extra_params = set(outs.keys()) - in_keys
            non_matching_els = list(
                k
                for k, v in compare_schema.items()
                if k not in outs or not v.outtype.can_receive_from(outs[k].outtype)
            )
            if len(non_matching_els) > 0:
                non_matching_tools.append(
                    tool.id() + ": " + ", ".join(non_matching_els + list(extra_params))
                )

        if non_matching_tools:
            raise Exception(
                "Not all tools in the switch statement shared the output schema: "
                + " || ".join(non_matching_tools)
            )

        # should be good to build the workflow:
        w = wrap_steps_in_workflow(stepid, conditions)
        self.step(stepid, w)

    def __getattr__(self, item):
        if item in self.__dict__ or item == "nodes":
            return self.__dict__.get(item)

        if self.nodes and item in self.nodes:
            node = self.nodes[item]
            if isinstance(node, InputNode):
                return node.as_operator()
            return node

        raise AttributeError(
            f"AttributeError: '{type(self).__name__}' object has no attribute '{item}'"
        )

    def __getitem__(self, item):

        if item in self.nodes:
            return self.nodes[item]

        raise KeyError(f"KeyError: '{type(self).__name__}' object has no node '{item}'")

    @classmethod
    def type(cls) -> ToolType:
        return ToolTypes.Workflow

    def tool_inputs(self) -> List[TInput]:
        """
        List of ToolInputs of the workflow, we can toss out most of the metadata
        about positioning, prefixes, etc that the ToolInput class uses
        """
        return [
            TInput(i.id(), i.datatype, default=i.default, doc=i.doc)
            for i in self.input_nodes.values()
        ]

    def tool_outputs(self) -> List[TOutput]:
        """
        Similar to inputs, return a list of ToolOutputs of the workflow
        """
        return [
            TOutput(o.id(), o.datatype, doc=o.doc) for o in self.output_nodes.values()
        ]

    def translate(
        self,
        translation: SupportedTranslation,
        to_console=True,
        tool_to_console=False,
        to_disk=False,
        write_inputs_file=True,
        with_docker=True,
        with_hints=False,
        with_resource_overrides=False,
        validate=False,
        should_zip=True,
        export_path=ExportPathKeywords.default,
        merge_resources=False,
        hints=None,
        allow_null_if_not_optional=True,
        additional_inputs: Dict = None,
        max_cores=None,
        max_mem=None,
    ):
        from janis_core.translations import translate_workflow

        return translate_workflow(
            self,
            translation=translation,
            to_console=to_console,
            tool_to_console=tool_to_console,
            to_disk=to_disk,
            with_docker=with_docker,
            with_resource_overrides=with_resource_overrides,
            should_zip=should_zip,
            export_path=export_path,
            write_inputs_file=write_inputs_file,
            should_validate=validate,
            merge_resources=merge_resources,
            hints=hints,
            allow_null_if_not_optional=allow_null_if_not_optional,
            additional_inputs=additional_inputs,
            max_cores=max_cores,
            max_mem=max_mem,
        )

    def generate_inputs_override(
        self, additional_inputs=None, with_resource_overrides=False, hints=None
    ):
        """
        Generate the overrides to be used with Janis. Although it may work with
        other
        :return:
        """
        ad = additional_inputs or {}

        d = {
            i.id(): ad.get(i.id(), i.value or i.default)
            for i in self.input_nodes.values()
            if i.id() in ad or i.default or i.value or not i.datatype.optional
        }

        if with_resource_overrides:
            from janis_core.translations import CwlTranslator

            d.update(CwlTranslator().build_resources_input(self, hints))

        return d

    def generate_resources_file(
        self,
        translation: SupportedTranslation,
        hints: Dict[str, Any] = None,
        to_console=True,
    ):
        from janis_core.translations import build_resources_input

        tr = build_resources_input(self, translation, hints)
        if to_console:
            print(tr)
        return tr

    def get_tools(self) -> Dict[str, CommandTool]:
        tools: Dict[str, CommandTool] = {}
        for t in self._steps:
            tl = t.step.tool()
            if isinstance(tl, Workflow):
                tools.update(tl.get_tools())
            elif t.id() not in tools:
                tools[tl.id()] = tl
        return tools

    def report(self, to_console=True, tabulate_tablefmt=None):
        import tabulate

        tools = self.get_tools()
        keys = sorted(tools.keys(), key=lambda a: a[0].lower())

        header = ["tool", "version", "container"]
        data = []
        for t in keys:
            tool = tools[t]
            data.append(
                [
                    f"{tool.friendly_name()} ({tool.id()})",
                    tool.version(),
                    tool.container(),
                ]
            )

        retval = tabulate.tabulate(data, headers=header, tablefmt=tabulate_tablefmt)
        if to_console:
            print(retval)

        return retval

    def generate_resources_table(
        self,
        hints: Dict[str, Any],
        to_console=True,
        to_disk=False,
        output_type: str = "tsv",
    ):
        delim = "\t" if output_type == "tsv" else ","

        tools = self.get_tools()
        header = ["name", "cpu", "memory (GB)"]
        data = []

        for t in sorted(tools.keys()):
            tool = tools[t]
            data.append([tool.id(), tool.cpus(hints), tool.memory(hints)])

        data.sort(key=lambda a: a[0].lower())

        data.insert(0, header)

        if to_console:
            import tabulate

            print(tabulate.tabulate(data, headers="firstrow"))
        if to_disk:
            import csv

            d = ExportPathKeywords.resolve(
                ExportPathKeywords.default_no_spec, None, self.id()
            )
            path = d + f"resources.{output_type}"

            if not os.path.isdir(d):
                os.makedirs(d)

            Logger.info(f"Writing resources {output_type} to '{path}'")
            with open(path, "w+") as mf:
                writer = csv.writer(mf, delimiter=delim)
                for row in data:
                    writer.writerow(row)

        return data

    def version(self):
        meta: WorkflowMetadata = self.bind_metadata() or self.metadata
        if meta:
            return meta.version


class WorkflowBuilder(Workflow):
    def __init__(
        self, identifier: str = None, friendly_name: str = None, version: str = None
    ):
        self._identifier = identifier
        self._name = friendly_name
        self._version = version

        super().__init__()

    def id(self):
        return self._identifier

    def friendly_name(self):
        return self._name

    def __call__(self, **connections):
        self.connections = connections
        return self

    def constructor(self):
        """
        Empty placeholder as users will construct their workflow manually, and not as part of this class
        :return:
        """
        return self

    def version(self):
        return self.version


def wrap_steps_in_workflow(
    stepId: str, steps: List[Union[Tuple[Operator, Tool], Tool]]
):
    # skip validation of steps

    # need to resolve connections and try to map them to the same

    w = WorkflowBuilder(stepId)
    workflow_connection_map = {}

    def rebuild_condition(operator: Operator):
        if operator is None:
            return None

        if not isinstance(operator, Operator):
            return operator

        # traverse through the operator, rebuilding and swapping out InputOperator / StepOperator
        # ensure traversal through StringFormatters to looking for the same thing

        if isinstance(operator, StepOperator):
            # pipe in the input of the step_operator
            identifier = f"cond_{operator.node.id()}_{operator.tag}"
            if identifier in w.input_nodes:
                return w.input_nodes[identifier]
            else:
                workflow_connection_map[identifier] = operator
                return w.input(
                    identifier, operator.node.outputs()[operator.tag].outtype
                )
        if isinstance(operator, InputOperator):
            identifier = f"cond_{operator.input_node.id()}"
            if identifier in w.input_nodes:
                return w.input_nodes[identifier]
            else:
                innode: InputNode = operator.input_node
                workflow_connection_map[identifier] = innode
                return w.input(
                    identifier, first_value(innode.outputs()).outtype
                ).as_operator()

        if isinstance(operator, StringFormatter):
            return StringFormatter(
                operator._format,
                **{k: rebuild_condition(v) for k, v in operator.kwargs.items()},
            )

        if isinstance(operator, SingleValueOperator):
            return operator.__class__(rebuild_condition(operator.internal))

        if isinstance(operator, TwoValueOperator):
            return operator.__class__(
                rebuild_condition(operator.lhs), rebuild_condition(operator.rhs)
            )

        return operator

    prevconds: List[Operator] = []

    for i in range(len(steps)):
        step = steps[i]
        stepid = "switch_case_" + str(i + 1)

        if isinstance(step, tuple):
            newcond = rebuild_condition(step[0])
            tool = step[1]
            prevcond = or_prev_conds(prevconds)
            cond = AndOperator(newcond, NotOperator(prevcond)) if prevcond else newcond
            prevconds.append(newcond)
        else:
            tool = step
            cond = NotOperator(or_prev_conds(prevconds))

        toolinputs = tool.inputs_map()
        connection_map = {}
        for (k, v) in tool.connections.items():

            # unique
            identifier = stepid + "_" + k

            if is_python_primitive(v):
                # the add step will create the literal for us
                connection_map[k] = v
            else:
                # generate an input for this step, of whatever type our tool wants
                toolin = toolinputs[k]
                workflow_connection_map[identifier] = v
                connection_map[k] = w.input(identifier, toolin.intype, doc=toolin.doc)

        w.step(stepid, tool(**connection_map), when=cond)

    steps = list(w.step_nodes.values())
    # They should all have exact same contract
    outputs = steps[0].tool.outputs_map().values()
    for o in outputs:
        w.output(
            o.id(),
            source=[stp[o.id()] for stp in steps],
            pick_value=PickValue.first_non_null,
        )

    return w(**workflow_connection_map)
