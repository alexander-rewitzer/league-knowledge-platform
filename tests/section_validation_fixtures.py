"""Synthetic section-local semantic decisions, extraction and filesystem fixtures."""

from dataclasses import replace
from pathlib import Path

from league_knowledge_platform import (
    BronzeConfig, EvidenceSpan, GateEvaluation, GateVerdict, JudgeIssue, SectionJudgeEvaluation,
    SectionValidationConfig, SemanticGate, SegmentationConfig, SilverConfig, SemanticCallMetadata,
    canonical_segmentation_path, canonical_silver_path, extract_knowledge, load_extraction_section,
)
from league_knowledge_platform.extraction_prompts import EXTRACTION_REFINEMENT_PROMPT_VERSION
from league_knowledge_platform.section_validation_prompts import SECTION_JUDGE_PROMPT_VERSION
from extraction_http_fixtures import input_bytes, response, section, document, SelectionHTTPAdapter, envelope, requests

JUDGE_METADATA = SemanticCallMetadata("fixture", "judge-model", "reported-judge", SECTION_JUDGE_PROMPT_VERSION)


def evaluation(*failed, item_ids=("item-000001",), spans=((0, 3),), reason="The proposed statement loses the source condition."):
    issue = JudgeIssue(reason, tuple(item_ids), tuple(EvidenceSpan(*s) for s in spans),
                       "Preserve the condition in the statement and cite the supporting source spans.")
    return SectionJudgeEvaluation(tuple(GateEvaluation(g, GateVerdict.FAIL if g.value in failed else GateVerdict.PASS,
                                                       (issue,) if g.value in failed else ()) for g in SemanticGate), JUDGE_METADATA)


def judge_document(*failed, **kwargs):
    from league_knowledge_platform.section_knowledge import _gates_document
    return {"gates": _gates_document(evaluation(*failed, **kwargs))}


class FakeJudge:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def evaluate(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("Unexpected Judge call")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeExtractor:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def extract(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("Unexpected extraction call")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if request.refinement is not None:
            entry = replace(entry, metadata=replace(entry.metadata, prompt_version=EXTRACTION_REFINEMENT_PROMPT_VERSION))
        return entry


def workspace(directory, *, texts=()):
    root = Path(directory)
    configs = {"bronze_config": BronzeConfig(root / "bronze"), "silver_config": SilverConfig(root / "silver"),
               "segmentation_config": SegmentationConfig(root / "sections"), "config": SectionValidationConfig(root / "validated")}
    segmented, silver = input_bytes(*texts)
    for path, content in ((canonical_silver_path("A", configs["silver_config"]), silver),
                          (canonical_segmentation_path("A", configs["segmentation_config"]), segmented)):
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    unit = load_extraction_section("A", "section-000001", silver_config=configs["silver_config"],
                                   segmentation_config=configs["segmentation_config"])
    return configs, unit


def initial_proposal(unit=None, *statements):
    return extract_knowledge(unit or section(), capability=FakeExtractor([response(*(statements or ("Usually wait for the interrupt to be unavailable.",)))]))
