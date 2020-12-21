import re
import inspect
from datetime import datetime
from typing import Union, List, Tuple

from janis_core.utils.logger import Logger

from janis_core.tool.commandtool import (
    ToolInput,
    ToolArgument,
    ToolOutput,
    InputDocumentation,
)
from janis_core.types import (
    InputSelector,
    WildcardSelector,
    CpuSelector,
    MemorySelector,
)
from janis_core.types.data_types import DataType
from janis_core.utils.metadata import Metadata, ToolMetadata

from toolbuilder.templates import (
    ToolTemplateType,
    generate_tool_version_template,
    generate_gatk4_tooltemplatebase,
    generate_regular_tooltemplatebase,
)

generic_convertible = [
    DataType,
    ToolInput,
    ToolOutput,
    ToolArgument,
    InputSelector,
    WildcardSelector,
    MemorySelector,
    CpuSelector,
    Metadata,
    InputDocumentation,
]


def get_string_repr(obj):
    if isinstance(obj, list):
        inner = ", ".join(map(get_string_repr, obj))
        return f"[{inner}]"
    if isinstance(obj, str):
        nlreplaced = obj.replace("\n", "\\n").replace('"', "'")
        return f'"{nlreplaced}"'
    elif isinstance(obj, (int, float, bool, type(None))):
        return str(obj)
    elif isinstance(obj, datetime):
        return f"datetime({obj.year}, {obj.month}, {obj.day})"
    elif obj is None:
        return "none"
    elif isinstance(
        obj, object
    ):  # any(isinstance(obj, T) for T in generic_convertible):
        return convert_generic_class(obj)

    return str(obj)


def convert_generic_class(t, ignore_fields=None):
    options = []

    ignore_fields = set(
        (ignore_fields if ignore_fields else []) + ["self", "args", "kwargs"]
    )

    params = inspect.signature(type(t).__init__).parameters
    param_map = {}
    if hasattr(t, "init_key_map"):
        param_map = t.init_key_map
    # fields = fields_to_check if fields_to_check \
    #     else [f for f in dict(params).keys() if f not in ignore_fields]

    for fkey in params:
        if fkey in ignore_fields:
            continue

        opts = params[fkey]

        t_key = param_map.get(fkey, fkey)
        if t_key is None:
            continue

        if hasattr(t, t_key):
            v = t.__getattribute__(t_key)
        else:
            Logger.warn(
                f"Object {t.__class__.__name__} didn't have attribute {t_key}, setting to None and it might get skipped"
            )
            v = None
        if (v is None and opts.default is None) or v == opts.default:
            continue

        options.append(fkey + "=" + get_string_repr(v))

    return f"{t.__class__.__name__}({', '.join(options)})"


def convert_commandtool(type: ToolTemplateType, commandtool):

    convert_command_tool_fragments(
        type,
        commandtool.id(),
        commandtool.base_command(),
        commandtool.friendly_name(),
        commandtool.tool_provider(),
        commandtool.inputs(),
        commandtool.outputs(),
        commandtool.metadata(),
        commandtool.version(),
        commandtool.docker(),
    )


def convert_command_tool_fragments(
    type: ToolTemplateType,
    toolid: str,
    basecommand: Union[str, List[str]],
    friendly_name: str,
    toolprov: str,
    ins: [ToolInput],
    outs: [ToolOutput],
    metadata: ToolMetadata,
    version: str,
    container: str,
) -> Tuple[str, str]:

    if not metadata.dateCreated:
        metadata.dateCreated = datetime.now()
    metadata.dateUpdated = datetime.now()

    if isinstance(toolid, list):
        toolid = "".join(s.title() for s in toolid)

    bc = f'"{basecommand}"' if isinstance(basecommand, str) else str(basecommand)

    ins = [get_string_repr(i) for i in ins]
    outs = [get_string_repr(o) for o in outs]

    if type == ToolTemplateType.base:
        base = generate_regular_tooltemplatebase(
            toolname=toolid,
            name=toolid,
            friendly_name=friendly_name.title(),
            tool_provider=toolprov,
            base_command=bc,
            inputs=ins,
            outputs=outs,
            metadata=get_string_repr(metadata),
        )
    elif type == ToolTemplateType.gatk4:
        base = generate_gatk4_tooltemplatebase(
            gatk_command=basecommand[1],
            inputs=ins,
            outputs=outs,
            metadata=get_string_repr(metadata),
        )
    else:
        raise NotImplementedError(f"Couldn't convert tool type {type.value}")

    return (
        base,
        generate_tool_version_template(
            name=toolid, version=version, container=container
        ),
    )
