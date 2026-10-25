"""Synthetic multi-section source, durable upstream checkpoints and scripted semantics."""

from pathlib import Path

from league_knowledge_platform import (
    EvidenceSpan, GateVerdict, KnowledgeEvidence, SectionItemReference, SegmentationConfig,
    SectionValidationConfig, SilverConfig, SourceAuditConfig, SourceAuditGate, SourceAuditIssue,
    SourceAuditResponse, SourceConflictDraft, SourceGateEvaluation, SourceIssueKind, SourceItemDraft,
    SourceValidatorEvaluation, SemanticCallMetadata, canonical_segmentation_path, canonical_silver_path,
    construct_segmentation_artifact, load_source_audit_input, process_section_knowledge,
)
from league_knowledge_platform.source_audit_prompts import SOURCE_AUDIT_PROMPT_VERSION, SOURCE_VALIDATOR_PROMPT_VERSION
from league_knowledge_platform.source_knowledge import evaluation_document
from section_validation_fixtures import FakeExtractor, FakeJudge, evaluation as section_evaluation, response
from segmentation_http_fixtures import bronze_bytes, proposal, source
from test_segmentation_artifacts import artifact
from selection_http_fixtures import SelectionHTTPAdapter, envelope, requests

AUDIT_METADATA = SemanticCallMetadata("fixture", "audit-model", "reported-audit", SOURCE_AUDIT_PROMPT_VERSION)
VALIDATOR_METADATA = SemanticCallMetadata("fixture", "judge-model", "reported-validator", SOURCE_VALIDATOR_PROMPT_VERSION)


def audit_response(*groups, conflicts=()):
    if not groups:
        groups = (("Preserve Charm against Zed because missing it gives an engage window.", ("input-000001", "input-000002")),)
    return SourceAuditResponse(tuple(SourceItemDraft(statement, tuple(refs)) for statement, refs in groups),
        tuple(SourceConflictDraft(tuple(positions), reason) for positions, reason in conflicts), AUDIT_METADATA)


def audit_document(*groups, conflicts=()):
    value = audit_response(*groups, conflicts=conflicts)
    return {"items": [{"statement": i.statement, "input_refs": list(i.input_refs)} for i in value.items],
            "conflicts": [{"item_positions": list(c.item_positions), "reason": c.reason} for c in value.conflicts]}


def validator_evaluation(*failed, kind=SourceIssueKind.CANDIDATE_DEFECT,
                         source_ids=("source-item-000001",), input_refs=(("section-000001", "item-000001"),),
                         evidence=(("section-000001", ((0, 3),)),), reason="Missing the stated condition."):
    issue = SourceAuditIssue(kind, reason, tuple(source_ids), tuple(SectionItemReference(*r) for r in input_refs),
        tuple(KnowledgeEvidence(s, tuple(EvidenceSpan(*span) for span in spans)) for s, spans in evidence),
        "Restore the source-backed condition, retaining all validated inputs.")
    return SourceValidatorEvaluation(tuple(SourceGateEvaluation(g, GateVerdict.FAIL if g.value in failed else GateVerdict.PASS,
        (issue,) if g.value in failed else ()) for g in SourceAuditGate), VALIDATOR_METADATA)


def validator_document(*failed, **kwargs):
    # Exercise the actual JSON array representation, not Python tuples.
    import json
    return json.loads(json.dumps(evaluation_document(validator_evaluation(*failed, **kwargs))))


class FakeAudit:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def propose(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("Unexpected audit invocation")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry(request) if callable(entry) else entry


class FakeValidator:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def evaluate(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("Unexpected validator invocation")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry(request) if callable(entry) else entry


def workspace(directory, *, empty=False, upstream="content", texts=None):
    from hashlib import sha256
    root = Path(directory)
    configs = {"silver_config": SilverConfig(root / "silver"), "segmentation_config": SegmentationConfig(root / "sections"),
               "section_validation_config": SectionValidationConfig(root / "validated"), "config": SourceAuditConfig(root / "source-knowledge")}
    silver = source(*(texts or ("Preserve Charm against Zed.", "Usually avoid wasting it.",
                                "Missing Charm creates an engage window.", "Retain this causal explanation.", "AD_ONLY sponsor")))
    if upstream == "empty":
        silver = source("", "")
        segmented = construct_segmentation_artifact(silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest())
    else:
        spans = proposal((0, 12, "ADVERTISEMENT")) if upstream == "ads" else proposal((0, 3, "CONTENT"), (6, 9, "CONTENT"), (12, 12, "ADVERTISEMENT"))
        segmented = artifact(silver, spans)
    for path, content in ((canonical_silver_path("A", configs["silver_config"]), bronze_bytes(silver)),
                          (canonical_segmentation_path("A", configs["segmentation_config"]), bronze_bytes(segmented))):
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    if upstream == "content":
        for number, statement, spans in ((1, "Preserve Charm against Zed.", ((0, 3),)),
                                         (2, "Missing Charm creates an engage window.", ((6, 9),))):
            process_section_knowledge("A", f"section-{number:06d}", silver_config=configs["silver_config"],
                segmentation_config=configs["segmentation_config"], config=configs["section_validation_config"],
                extraction_capability=FakeExtractor([response(*(() if empty else (statement,)), spans=spans)]),
                judge_capability=FakeJudge([section_evaluation()]))
    inputs = load_source_audit_input("A", **{k: v for k, v in configs.items() if k != "config"})
    return configs, inputs
