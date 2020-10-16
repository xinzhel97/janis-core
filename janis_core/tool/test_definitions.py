# import docker
from typing import Dict, Union, List, Set
from pkg_resources import parse_version

from janis_core import ToolType, Tool, CommandTool, CodeTool, Workflow
from janis_core.translationdeps.supportedtranslations import SupportedTranslation
from janis_core.translations.cwl import CwlTranslator
from janis_core.translations.wdl import WdlTranslator
from janis_core.utils.metadata import ToolMetadata

from janis_core.tool import test_helpers


class ToolEvaluator:
    @classmethod
    def evaluate(cls, tool: Tool) -> Union[str, bool]:
        """
        Evaluate a Janis tool whether they satisfy certain criteria for them to be publishable
        """
        if tool.type() == ToolType.Workflow:
            return cls.evaluate_workflow(tool)
        elif tool.type() == ToolType.CommandTool:
            return cls.evaluate_command_tool(tool)
        elif tool.type() == ToolType.CodeTool:
            return cls.evaluate_code_tool(tool)
        raise Exception("Unrecognised tool type: " + str(tool.type()))

    @classmethod
    def evaluate_command_tool(cls, tool: CommandTool) -> Union[str, bool]:
        evaluation = cls.evaluate_generic(tool)
        return cls._read_evaluation(evaluation)

    @classmethod
    def evaluate_code_tool(cls, tool: CodeTool) -> Union[str, bool]:
        evaluation = cls.evaluate_generic(tool)
        return cls._read_evaluation(evaluation)

    @classmethod
    def evaluate_workflow(cls, wf: Workflow) -> Union[str, bool]:
        return True

    @classmethod
    def evaluate_generic(cls, tool) -> Dict[str, str]:
        evaluation = {}

        evaluation["friendly_name"] = cls.evaluate_friendly_name(tool)
        evaluation["metadata"] = cls.evaluate_metadata(tool)
        evaluation["unit_tests_exists"] = cls.evaluate_unit_test_exists(tool)
        evaluation['container'] = cls.evaluate_container(tool)
        evaluation["translation"] = cls.evaluate_translation(tool)

        return evaluation

    @staticmethod
    def evaluate_unit_test_exists(tool: Tool) -> Union[str, bool]:
        if tool.tests():
            return True

        return "Mising unit tests"

    @staticmethod
    def evaluate_friendly_name(tool: Tool) -> Union[str, bool]:
        if tool.friendly_name() is None:
            return "Missing friendly name"

        return True

    @staticmethod
    def evaluate_metadata(tool: Tool) -> Union[str, bool]:
        if isinstance(tool.metadata, ToolMetadata):
            required = {
                "contributors": tool.metadata.contributors,
                "created date": tool.metadata.dateCreated,
                "institution": tool.metadata.institution,
            }

            missing = []
            for key, field in required.items():
                if field is None or not field:
                    missing.append(key)

            if missing:
                return f"Missing metadata: {', '.join(missing)}"
        # elif isinstance(self.metadata, ...):
        else:
            return "Incorrect metadata class"

        return True

    @staticmethod
    def evaluate_container(tool: Tool) -> Union[str, bool]:
        """
        Evaluate if the image specified for this tool exists in the remote registry
        """
        # If there is no container, we don't need to check if the container exists in the registry
        if tool.container() is None:
            return True

        min_version_required = test_helpers.janis_assistant_version_required_min

        try:
            import janis_assistant
            if parse_version(janis_assistant.__version__) < parse_version(min_version_required):
                raise Exception()

            from janis_assistant.data.container.registries import ContainerRegistryBase, ContainerRegistry
            from janis_assistant.data.container.info import ContainerInfo

        except Exception as e:
            raise Exception(f"to run this test, janis_asisstant >= {min_version_required}"
                            f" must be installed")

        # Call this outside the try-except so that we can still throw
        # different exceptions relevant to the actual logic of this function
        ci = ContainerInfo.parse(tool.container())
        registry = ContainerRegistry.from_host(ci.host).to_registry()
        digest = registry.get_digest(ci)

        if digest:
            return True
        else:
            return f"image {tool.container()} not found"

    @staticmethod
    def evaluate_translation(tool: Tool) -> Union[str, bool]:
        cwl_file_path = f"/tmp/janis/tests/{tool.id()}/cwl"
        wdl_file_path = f"/tmp/janis/tests/{tool.id()}/wdl"

        tool.translate(
            SupportedTranslation.CWL,
            to_console=False,
            to_disk=True,
            export_path=cwl_file_path,
        )
        tool.translate(
            SupportedTranslation.WDL,
            to_console=False,
            to_disk=True,
            export_path=wdl_file_path,
        )

        # TODO: translate and validate
        CwlTranslator.validate_command_for(cwl_file_path, "", "", "")
        WdlTranslator.validate_command_for(wdl_file_path, "", "", "")

        return True

    @staticmethod
    def _read_evaluation(evaluation: Dict[str, str]) -> Union[str, bool]:
        """
        Translate evaluation results into reportable format

        Returns:
            - True if no error is found in each of the evaluation criteria
            - A string of error messages if at least one of the evaluation fails
        """
        errors = []
        for field in evaluation:
            if evaluation[field] is not True:
                errors.append(evaluation[field])

        if not errors:
            return True

        return "; ".join(errors)