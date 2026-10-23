"""Section-local semantic gates, completed rounds and durable success contracts."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Protocol

from . import defaults
from .extraction_models import (
    EvidenceSpan, ExtractionOutcome, ExtractionProposal, ExtractionProvenance,
    ExtractionSection, _section_id, _text, _tuple,
)
from .selection_models import SemanticCallMetadata

SECTION_KNOWLEDGE_SCHEMA_VERSION = 1
SECTION_VALIDATION_CONTRACT_VERSION = 1


class SemanticGate(str, Enum):
    FAITHFULNESS = "FAITHFULNESS"
    COMPLETENESS = "COMPLETENESS"
    ATOMICITY = "ATOMICITY"
    CONTEXT_PRESERVATION = "CONTEXT_PRESERVATION"
    DUPLICATION = "DUPLICATION"


class GateVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class SectionDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class JudgeIssue:
    reason: str = field(repr=False)
    affected_item_ids: tuple[str, ...]
    relevant_source_spans: tuple[EvidenceSpan, ...]
    actionable_feedback: str = field(repr=False)

    def __post_init__(self):
        _text(self.reason)
        _text(self.actionable_feedback)
        if (not isinstance(self.affected_item_ids, tuple) or any(not isinstance(v, str)
                or re.fullmatch(r"item-[0-9]{6,}", v) is None for v in self.affected_item_ids)
                or len(set(self.affected_item_ids)) != len(self.affected_item_ids)):
            raise ValueError("Expected unique local item references")
        _tuple(self.relevant_source_spans, EvidenceSpan)


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    gate: SemanticGate
    verdict: GateVerdict
    issues: tuple[JudgeIssue, ...]

    def __post_init__(self):
        if not isinstance(self.gate, SemanticGate) or not isinstance(self.verdict, GateVerdict):
            raise ValueError("Expected supported binary semantic gate")
        _tuple(self.issues, JudgeIssue)
        if bool(self.issues) != (self.verdict is GateVerdict.FAIL):
            raise ValueError("Failed gates require issues; passed gates cannot contain issues")
        if self.gate is SemanticGate.COMPLETENESS and any(not i.relevant_source_spans for i in self.issues):
            raise ValueError("Missing-knowledge feedback requires source references")


@dataclass(frozen=True, slots=True)
class SectionJudgeEvaluation:
    gates: tuple[GateEvaluation, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        _tuple(self.gates, GateEvaluation, nonempty=True)
        if len(self.gates) != 5 or {g.gate for g in self.gates} != set(SemanticGate):
            raise ValueError("Expected exactly the five semantic gates")
        if not isinstance(self.metadata, SemanticCallMetadata):
            raise ValueError("Expected sanitized Judge metadata")
        self.metadata.__post_init__()


@dataclass(frozen=True, slots=True)
class SectionJudgeResult:
    evaluation: SectionJudgeEvaluation = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.evaluation, SectionJudgeEvaluation):
            raise ValueError("Expected complete Judge evaluation")
        self.evaluation.__post_init__()

    @property
    def decision(self):
        return SectionDecision.PASS if all(g.verdict is GateVerdict.PASS for g in self.evaluation.gates) else SectionDecision.REJECT


@dataclass(frozen=True, slots=True)
class SectionJudgeRequest:
    section: ExtractionSection = field(repr=False)
    proposal: ExtractionProposal = field(repr=False)
    output_limit: int = defaults.SECTION_JUDGE_OUTPUT_LIMIT

    def __post_init__(self):
        if not isinstance(self.section, ExtractionSection) or not isinstance(self.proposal, ExtractionProposal):
            raise ValueError("Expected full section and proposal")
        self.section.__post_init__()
        self.proposal.__post_init__()
        if type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("Expected positive Judge output allowance")


class SectionJudgeCapability(Protocol):
    def evaluate(self, request: SectionJudgeRequest) -> SectionJudgeEvaluation: ...


@dataclass(frozen=True, slots=True)
class SemanticRoundRecord:
    number: int
    proposal_outcome: ExtractionOutcome
    proposal_item_count: int
    proposal_sha256: str
    extraction_metadata: SemanticCallMetadata
    evaluation: SectionJudgeEvaluation = field(repr=False)

    def __post_init__(self):
        if (type(self.number) is not int or not 1 <= self.number <= 3
                or not isinstance(self.proposal_outcome, ExtractionOutcome)
                or type(self.proposal_item_count) is not int or self.proposal_item_count < 0
                or bool(self.proposal_item_count) != (self.proposal_outcome is ExtractionOutcome.ITEMS_PROPOSED)
                or not isinstance(self.proposal_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.proposal_sha256) is None
                or not isinstance(self.extraction_metadata, SemanticCallMetadata)
                or not isinstance(self.evaluation, SectionJudgeEvaluation)):
            raise ValueError("Invalid completed semantic round")
        self.extraction_metadata.__post_init__()
        self.evaluation.__post_init__()


@dataclass(frozen=True, slots=True)
class ValidatedSectionKnowledge:
    schema_version: int
    validation_contract_version: int
    created_at_utc: str
    section_id: str
    provenance: ExtractionProvenance
    accepted_proposal: ExtractionProposal = field(repr=False)
    rounds: tuple[SemanticRoundRecord, ...] = field(repr=False)

    def __post_init__(self):
        if any(type(v) is not int or v != 1 for v in (self.schema_version, self.validation_contract_version)):
            raise ValueError("Unsupported validated-section version")
        _section_id(self.section_id)
        if not isinstance(self.provenance, ExtractionProvenance) or not isinstance(self.accepted_proposal, ExtractionProposal):
            raise ValueError("Expected accepted proposal and provenance")
        self.provenance.__post_init__()
        self.accepted_proposal.__post_init__()
        _tuple(self.rounds, SemanticRoundRecord, nonempty=True)
        if len(self.rounds) > 3:
            raise ValueError("Exceeded semantic-round budget")

    @property
    def semantic_rounds_used(self):
        return len(self.rounds)


@dataclass(frozen=True, slots=True)
class SectionKnowledgeResult:
    path: Path
    created: bool
    checkpoint: ValidatedSectionKnowledge = field(repr=False)
    operation_rounds_used: int


class SectionValidationError(RuntimeError):
    def __init__(self, kind: str, *, phase: str = "INPUT", rounds=()):
        self.kind, self.phase, self.rounds = kind, phase, tuple(rounds)
        self.semantic_rounds_used = len(self.rounds)
        super().__init__(f"Section validation failed ({phase}, {kind}, completed_rounds={len(self.rounds)})")


class SectionValidationInputError(SectionValidationError):
    pass


class SectionValidationIntegrityError(SectionValidationError):
    pass


class SectionValidationOperationalError(SectionValidationError):
    pass


class SectionValidationExhaustedError(SectionValidationError):
    pass


class SectionKnowledgeContractError(SectionValidationError):
    pass


class SectionKnowledgeFilesystemError(SectionValidationError):
    pass


class SectionJudgeCapabilityError(RuntimeError):
    def __init__(self, kind: str, *, retryable: bool):
        from .extraction_models import ExtractionCapabilityError
        sanitized = ExtractionCapabilityError(kind, retryable=retryable)
        self.kind, self.retryable = sanitized.kind, sanitized.retryable
        super().__init__(f"Section Judge failed ({kind})")


@dataclass(frozen=True, slots=True)
class SectionValidationConfig:
    root: str | Path = defaults.SECTION_KNOWLEDGE_ROOT
    max_semantic_rounds: int = defaults.SECTION_VALIDATION_MAX_ROUNDS
    judge_output_limit: int = defaults.SECTION_JUDGE_OUTPUT_LIMIT

    def __post_init__(self):
        if (not isinstance(self.root, (str, Path)) or not str(self.root).strip()
                or type(self.max_semantic_rounds) is not int or not 1 <= self.max_semantic_rounds <= 3
                or type(self.judge_output_limit) is not int or self.judge_output_limit <= 0):
            raise ValueError("Invalid section validation configuration")


DEFAULT_SECTION_VALIDATION_CONFIG = SectionValidationConfig()


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError("Unexpected Judge fields")
    return value


def decode_judge_document(document, metadata):
    _keys(document, "gates")
    gates = document["gates"]
    _keys(gates, " ".join(g.value for g in SemanticGate))
    results = []
    for gate in SemanticGate:
        evaluation = _keys(gates[gate.value], "verdict issues")
        if type(evaluation["issues"]) is not list:
            raise ValueError("Expected issues array")
        issues = []
        for issue in evaluation["issues"]:
            _keys(issue, "reason affected_item_ids relevant_source_spans actionable_feedback")
            if type(issue["affected_item_ids"]) is not list or type(issue["relevant_source_spans"]) is not list:
                raise ValueError("Expected issue reference arrays")
            spans = tuple(EvidenceSpan(**_keys(s, "first_source_index last_source_index")) for s in issue["relevant_source_spans"])
            issues.append(JudgeIssue(issue["reason"], tuple(issue["affected_item_ids"]), spans, issue["actionable_feedback"]))
        results.append(GateEvaluation(gate, GateVerdict(evaluation["verdict"]), tuple(issues)))
    return SectionJudgeEvaluation(tuple(results), metadata)
