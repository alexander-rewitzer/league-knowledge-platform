"""One-source audit contracts; model diagnostics never author upstream provenance."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Protocol

from . import defaults
from .extraction_models import (
    ExtractionProvenance, ExtractionSection, KnowledgeEvidence, KnowledgeItem,
    _section_id, _text, _tuple,
)
from .section_validation_models import GateVerdict
from .selection_models import SemanticCallMetadata

SOURCE_KNOWLEDGE_SCHEMA_VERSION = 1
SOURCE_AUDIT_CONTRACT_VERSION = 1


def _local_id(value, prefix):
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + r"-[0-9]{6,}", value) is None:
        raise ValueError("Expected local identity")


def _unique(values):
    if len(set(values)) != len(values):
        raise ValueError("Expected unique references")


def _metadata(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, SemanticCallMetadata):
        raise ValueError("Expected sanitized semantic metadata")
    value.__post_init__()


class SourceKnowledgeOutcome(str, Enum):
    KNOWLEDGE_AVAILABLE = "KNOWLEDGE_AVAILABLE"
    NO_SOURCE_KNOWLEDGE = "NO_SOURCE_KNOWLEDGE"


class SourceAuditGate(str, Enum):
    SOURCE_FAITHFULNESS = "SOURCE_FAITHFULNESS"
    FULL_INPUT_COVERAGE = "FULL_INPUT_COVERAGE"
    NO_KNOWLEDGE_LOSS = "NO_KNOWLEDGE_LOSS"
    CROSS_SECTION_DEDUPLICATION = "CROSS_SECTION_DEDUPLICATION"
    COHERENCE_ATOMICITY = "COHERENCE_ATOMICITY"
    CONFLICT_PRESERVATION = "CONFLICT_PRESERVATION"
    PROVENANCE_INTEGRITY = "PROVENANCE_INTEGRITY"


class SourceAuditDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


class SourceIssueKind(str, Enum):
    CANDIDATE_DEFECT = "CANDIDATE_DEFECT"
    UPSTREAM_COVERAGE_DEFECT = "UPSTREAM_COVERAGE_DEFECT"


@dataclass(frozen=True, slots=True)
class SectionItemReference:
    section_id: str
    item_id: str

    def __post_init__(self):
        _section_id(self.section_id)
        _local_id(self.item_id, "item")


@dataclass(frozen=True, slots=True)
class SectionCheckpointReference:
    section_id: str
    relative_path: str
    sha256: str
    schema_version: int
    validation_contract_version: int

    def __post_init__(self):
        _section_id(self.section_id)
        if (not isinstance(self.relative_path, str)
                or re.fullmatch(r"youtube/id-[0-9a-f]+/" + re.escape(self.section_id) + r"\.json", self.relative_path) is None
                or not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
                or any(type(v) is not int or v != 1 for v in (self.schema_version, self.validation_contract_version))):
            raise ValueError("Unsupported section checkpoint reference")


@dataclass(frozen=True, slots=True)
class SourceInputItem:
    token: str
    reference: SectionItemReference
    knowledge: KnowledgeItem = field(repr=False)

    def __post_init__(self):
        _local_id(self.token, "input")
        if not isinstance(self.reference, SectionItemReference) or not isinstance(self.knowledge, KnowledgeItem):
            raise ValueError("Expected validated input item")
        self.reference.__post_init__()
        self.knowledge.__post_init__()
        if self.reference.item_id != self.knowledge.item_id:
            raise ValueError("Input identity mismatch")


@dataclass(frozen=True, slots=True)
class SourceFileSnapshot:
    path: Path
    root: Path
    sha256: str

    def __post_init__(self):
        if (not isinstance(self.path, Path) or not isinstance(self.root, Path)
                or not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None):
            raise ValueError("Invalid immutable file snapshot")


@dataclass(frozen=True, slots=True)
class SourceAuditInput:
    provenance: ExtractionProvenance
    upstream_segmentation_outcome: str
    sections: tuple[ExtractionSection, ...] = field(repr=False)
    validated_sections: tuple[SectionCheckpointReference, ...]
    items: tuple[SourceInputItem, ...] = field(repr=False)
    snapshots: tuple[SourceFileSnapshot, ...] = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.provenance, ExtractionProvenance):
            raise ValueError("Expected trusted source provenance")
        self.provenance.__post_init__()
        if self.upstream_segmentation_outcome not in ("SEGMENTED", "NO_SEGMENTABLE_CONTENT"):
            raise ValueError("Unsupported upstream outcome")
        _tuple(self.sections, ExtractionSection)
        _tuple(self.validated_sections, SectionCheckpointReference)
        _tuple(self.items, SourceInputItem)
        _tuple(self.snapshots, SourceFileSnapshot, nonempty=True)
        if (tuple(s.section_id for s in self.sections) != tuple(s.section_id for s in self.validated_sections)
                or len({s.section_id for s in self.sections}) != len(self.sections)
                or any(s.provenance != self.provenance for s in self.sections)
                or self.upstream_segmentation_outcome == "NO_SEGMENTABLE_CONTENT" and self.sections):
            raise ValueError("Incomplete section context")
        _unique(tuple(i.reference for i in self.items))
        for position, item in enumerate(self.items, 1):
            if item.token != f"input-{position:06d}" or item.reference.section_id not in {s.section_id for s in self.sections}:
                raise ValueError("Input ordering or section mismatch")


@dataclass(frozen=True, slots=True)
class SourceKnowledgeItem:
    item_id: str
    statement: str = field(repr=False)
    evidence: tuple[KnowledgeEvidence, ...]
    derived_from: tuple[SectionItemReference, ...]

    def __post_init__(self):
        _local_id(self.item_id, "source-item")
        _text(self.statement)
        _tuple(self.evidence, KnowledgeEvidence, nonempty=True)
        _tuple(self.derived_from, SectionItemReference, nonempty=True)
        _unique(self.derived_from)


@dataclass(frozen=True, slots=True)
class SourceConflict:
    conflict_id: str
    item_ids: tuple[str, ...]
    reason: str = field(repr=False)

    def __post_init__(self):
        _local_id(self.conflict_id, "conflict")
        _text(self.reason)
        if not isinstance(self.item_ids, tuple) or len(self.item_ids) < 2:
            raise ValueError("Conflict needs distinct items")
        for identity in self.item_ids:
            _local_id(identity, "source-item")
        _unique(self.item_ids)


@dataclass(frozen=True, slots=True)
class SourceKnowledgeCandidate:
    outcome: SourceKnowledgeOutcome
    items: tuple[SourceKnowledgeItem, ...] = field(repr=False)
    conflicts: tuple[SourceConflict, ...] = field(repr=False)
    audit_metadata: SemanticCallMetadata | None

    def __post_init__(self):
        _tuple(self.items, SourceKnowledgeItem)
        _tuple(self.conflicts, SourceConflict)
        _metadata(self.audit_metadata, optional=True)
        if (not isinstance(self.outcome, SourceKnowledgeOutcome)
                or bool(self.items) != (self.outcome is SourceKnowledgeOutcome.KNOWLEDGE_AVAILABLE)
                or not self.items and self.conflicts):
            raise ValueError("Candidate outcome mismatch")


@dataclass(frozen=True, slots=True)
class SourceItemDraft:
    statement: str = field(repr=False)
    input_refs: tuple[str, ...]

    def __post_init__(self):
        _text(self.statement)
        if not isinstance(self.input_refs, tuple) or not self.input_refs:
            raise ValueError("Missing contributors")
        for identity in self.input_refs:
            _local_id(identity, "input")
        _unique(self.input_refs)


@dataclass(frozen=True, slots=True)
class SourceConflictDraft:
    item_positions: tuple[int, ...]
    reason: str = field(repr=False)

    def __post_init__(self):
        _text(self.reason)
        if (not isinstance(self.item_positions, tuple) or len(self.item_positions) < 2
                or any(type(v) is not int or v < 1 for v in self.item_positions)):
            raise ValueError("Invalid conflict positions")
        _unique(self.item_positions)


@dataclass(frozen=True, slots=True)
class SourceAuditResponse:
    items: tuple[SourceItemDraft, ...] = field(repr=False)
    conflicts: tuple[SourceConflictDraft, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        _tuple(self.items, SourceItemDraft)
        _tuple(self.conflicts, SourceConflictDraft)
        _metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class SourceAuditIssue:
    kind: SourceIssueKind
    reason: str = field(repr=False)
    affected_source_item_ids: tuple[str, ...]
    affected_section_item_refs: tuple[SectionItemReference, ...]
    relevant_evidence: tuple[KnowledgeEvidence, ...]
    actionable_feedback: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.kind, SourceIssueKind) or not isinstance(self.affected_source_item_ids, tuple):
            raise ValueError("Invalid diagnostic issue")
        _text(self.reason)
        _text(self.actionable_feedback)
        for identity in self.affected_source_item_ids:
            _local_id(identity, "source-item")
        _unique(self.affected_source_item_ids)
        _tuple(self.affected_section_item_refs, SectionItemReference)
        _unique(self.affected_section_item_refs)
        _tuple(self.relevant_evidence, KnowledgeEvidence)
        if self.kind is SourceIssueKind.UPSTREAM_COVERAGE_DEFECT and not self.relevant_evidence:
            raise ValueError("Upstream coverage diagnosis needs source evidence")


@dataclass(frozen=True, slots=True)
class SourceGateEvaluation:
    gate: SourceAuditGate
    verdict: GateVerdict
    issues: tuple[SourceAuditIssue, ...]

    def __post_init__(self):
        if not isinstance(self.gate, SourceAuditGate) or not isinstance(self.verdict, GateVerdict):
            raise ValueError("Unsupported binary source gate")
        _tuple(self.issues, SourceAuditIssue)
        if bool(self.issues) != (self.verdict is GateVerdict.FAIL):
            raise ValueError("Only failed gates require issues")
        if self.gate is not SourceAuditGate.NO_KNOWLEDGE_LOSS and any(
                i.kind is SourceIssueKind.UPSTREAM_COVERAGE_DEFECT for i in self.issues):
            raise ValueError("Upstream coverage diagnostic belongs to NO_KNOWLEDGE_LOSS")


@dataclass(frozen=True, slots=True)
class SourceValidatorEvaluation:
    gates: tuple[SourceGateEvaluation, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        _tuple(self.gates, SourceGateEvaluation, nonempty=True)
        _metadata(self.metadata)
        if len(self.gates) != 7 or {g.gate for g in self.gates} != set(SourceAuditGate):
            raise ValueError("Expected exactly seven source gates")

    @property
    def decision(self):
        return SourceAuditDecision.PASS if all(g.verdict is GateVerdict.PASS for g in self.gates) else SourceAuditDecision.REJECT


@dataclass(frozen=True, slots=True)
class SourceRefinementContext:
    previous_candidate: SourceKnowledgeCandidate = field(repr=False)
    evaluation: SourceValidatorEvaluation = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.previous_candidate, SourceKnowledgeCandidate) or not isinstance(self.evaluation, SourceValidatorEvaluation):
            raise ValueError("Expected complete candidate and feedback")
        self.previous_candidate.__post_init__()
        self.evaluation.__post_init__()
        if self.evaluation.decision is not SourceAuditDecision.REJECT or any(
                i.kind is SourceIssueKind.UPSTREAM_COVERAGE_DEFECT for g in self.evaluation.gates for i in g.issues):
            raise ValueError("Only candidate REJECT permits refinement")


@dataclass(frozen=True, slots=True)
class SourceAuditRequest:
    inputs: SourceAuditInput = field(repr=False)
    output_limit: int
    refinement: SourceRefinementContext | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.inputs, SourceAuditInput) or type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("Invalid complete audit request")
        self.inputs.__post_init__()
        if self.refinement is not None:
            if not isinstance(self.refinement, SourceRefinementContext):
                raise ValueError("Expected complete refinement context")
            self.refinement.__post_init__()


@dataclass(frozen=True, slots=True)
class SourceValidatorRequest:
    inputs: SourceAuditInput = field(repr=False)
    candidate: SourceKnowledgeCandidate = field(repr=False)
    output_limit: int

    def __post_init__(self):
        if (not isinstance(self.inputs, SourceAuditInput) or not isinstance(self.candidate, SourceKnowledgeCandidate)
                or type(self.output_limit) is not int or self.output_limit <= 0):
            raise ValueError("Invalid complete validation request")
        self.inputs.__post_init__()
        self.candidate.__post_init__()


class SourceAuditCapability(Protocol):
    def propose(self, request: SourceAuditRequest) -> SourceAuditResponse: ...


class SourceValidatorCapability(Protocol):
    def evaluate(self, request: SourceValidatorRequest) -> SourceValidatorEvaluation: ...


@dataclass(frozen=True, slots=True)
class SourceAuditRound:
    number: int
    candidate_sha256: str
    item_count: int
    conflict_count: int
    audit_metadata: SemanticCallMetadata | None
    evaluation: SourceValidatorEvaluation = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceKnowledgeArtifact:
    schema_version: int
    source_audit_contract_version: int
    created_at_utc: str
    provenance: ExtractionProvenance
    upstream_segmentation_outcome: str
    validated_sections: tuple[SectionCheckpointReference, ...]
    candidate: SourceKnowledgeCandidate = field(repr=False)
    rounds: tuple[SourceAuditRound, ...] = field(repr=False)

    @property
    def rounds_used(self):
        return len(self.rounds)

    @property
    def outcome(self):
        return self.candidate.outcome


@dataclass(frozen=True, slots=True)
class SourceKnowledgeResult:
    path: Path
    created: bool
    artifact: SourceKnowledgeArtifact = field(repr=False)
    operation_rounds_used: int


class SourceAuditError(RuntimeError):
    def __init__(self, kind, *, phase="INPUT", rounds=()):
        self.kind, self.phase, self.rounds = kind, phase, tuple(rounds)
        self.semantic_rounds_used = len(self.rounds)
        super().__init__(f"Source audit failed ({phase}, {kind}, completed_rounds={len(self.rounds)})")


class SourceAuditInputError(SourceAuditError):
    pass


class SourceAuditIntegrityError(SourceAuditError):
    pass


class SourceAuditOperationalError(SourceAuditError):
    pass


class SourceAuditExhaustedError(SourceAuditError):
    pass


class SourceAuditUpstreamCoverageError(SourceAuditError):
    pass


class SourceKnowledgeContractError(SourceAuditError):
    pass


class SourceKnowledgeFilesystemError(SourceAuditError):
    pass


class SourceAuditCapabilityError(RuntimeError):
    def __init__(self, kind, *, retryable):
        from .extraction_models import ExtractionCapabilityError
        sanitized = ExtractionCapabilityError(kind, retryable=retryable)
        self.kind, self.retryable = sanitized.kind, sanitized.retryable
        super().__init__(f"Source semantic provider failed ({sanitized.kind})")


@dataclass(frozen=True, slots=True)
class SourceAuditConfig:
    root: str | Path = defaults.SOURCE_KNOWLEDGE_ROOT
    max_semantic_rounds: int = defaults.SOURCE_AUDIT_MAX_ROUNDS
    audit_output_limit: int = defaults.SOURCE_AUDIT_OUTPUT_LIMIT
    validator_output_limit: int = defaults.SOURCE_VALIDATOR_OUTPUT_LIMIT

    def __post_init__(self):
        if (not isinstance(self.root, (str, Path)) or not str(self.root).strip()
                or type(self.max_semantic_rounds) is not int or not 1 <= self.max_semantic_rounds <= 3
                or any(type(v) is not int or v <= 0 for v in (self.audit_output_limit, self.validator_output_limit))):
            raise ValueError("Invalid source audit configuration")


DEFAULT_SOURCE_AUDIT_CONFIG = SourceAuditConfig()
