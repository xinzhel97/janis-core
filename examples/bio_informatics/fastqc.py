from pipeline_definition.types.step_type import StepFactory
from pipeline_definition.types.step_type import Step

class FastQCFactory(StepFactory):
    @classmethod
    def type(cls):
        return 'fastqc'

    @classmethod
    def label(cls):
        return 'fastqc'

    @classmethod
    def description(cls):
        return cls.label()

    @classmethod
    def describe(cls):
        return {
            'schema': {},
            'nullable': True
        }

    @classmethod
    def build(cls, meta):
        step = FastQCStep( meta )
        return step

class FastQCStep(Step):

    def provides(self):
        return [
            {
                Step.STR_ID : "reports",
                Step.STR_TYPE: "Text"
            }
        ]



    def requires(self):
        return [
            {
                Step.STR_ID: "read",
                Step.STR_TYPE: "SequenceReadArchivePaired"
            }
        ]

