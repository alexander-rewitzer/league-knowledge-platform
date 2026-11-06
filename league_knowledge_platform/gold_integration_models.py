"""Transient Gold preparation contracts. None represents a durable integration."""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol, TYPE_CHECKING

from . import defaults
from .gold_document_models import GoldDocumentLayout, GoldSnapshot
from .gold_governance_models import GoldIntegrationOperation
from .gold_models import GoldContractError, GoldSourceArtifactReference, _text
from .gold_store import GoldSnapshotReference
from .selection_models import SemanticCallMetadata

if TYPE_CHECKING:
    from .gold_application import IntegrationEffects
    from .gold_integration_input import IntegrationSource, IntegrationInspection

INTEGRATION_SCHEMA_VERSION = 1
INTEGRATION_CONTRACT_VERSION = 1


class IntegrationError(GoldContractError):
    """Content-free diagnostic carrying only completed semantic rounds."""

    def __init__(self, code, *, phase="PREPARATION", rounds=()):
        self.phase, self.rounds = phase, tuple(rounds)
        super().__init__(code)


class IntegrationOperationalError(IntegrationError):
    pass


class IntegrationValidationFailed(IntegrationError):
    pass


def require(condition, code):
    if not condition:
        raise IntegrationError(code)


def members(values, kind):
    require(type(values) is tuple and all(type(v) is kind for v in values), "COLLECTION")
    for value in values:
        replace(value)


def strings(values):
    require(type(values) is tuple and len(set(values)) == len(values), "REFERENCES")
    for value in values:
        _text(value)


class IntegrationGate(str, Enum):
    SOURCE_GROUNDING = "SOURCE_GROUNDING"
    SOURCE_ITEM_COVERAGE = "SOURCE_ITEM_COVERAGE"
    TARGET_CORRECTNESS = "TARGET_CORRECTNESS"
    KNOWLEDGE_PRESERVATION = "KNOWLEDGE_PRESERVATION"
    CONFLICT_HANDLING = "CONFLICT_HANDLING"
    PROVENANCE_COMPLETENESS = "PROVENANCE_COMPLETENESS"
    STRUCTURAL_VALIDITY = "STRUCTURAL_VALIDITY"
    OPERATION_APPROPRIATENESS = "OPERATION_APPROPRIATENESS"


class IntegrationVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class IntegrationIssue:
    reason: str = field(repr=False)
    actionable_feedback: str = field(repr=False)
    affected_operation_ids: tuple[str, ...] = ()
    affected_source_item_ids: tuple[str, ...] = ()
    affected_gold_ids: tuple[str, ...] = ()

    def __post_init__(self):
        _text(self.reason)
        _text(self.actionable_feedback)
        for value in (self.affected_operation_ids, self.affected_source_item_ids, self.affected_gold_ids):
            strings(value)


@dataclass(frozen=True, slots=True)
class IntegrationGateEvaluation:
    gate: IntegrationGate
    verdict: IntegrationVerdict
    issues: tuple[IntegrationIssue, ...] = field(default=(), repr=False)

    def __post_init__(self):
        require(type(self.gate) is IntegrationGate and type(self.verdict) is IntegrationVerdict, "GATE")
        members(self.issues, IntegrationIssue)
        require(bool(self.issues) == (self.verdict is IntegrationVerdict.FAIL), "GATE_ISSUES")


@dataclass(frozen=True, slots=True)
class IntegrationEvaluation:
    gates: tuple[IntegrationGateEvaluation, ...]
    metadata: SemanticCallMetadata

    def __post_init__(self):
        members(self.gates, IntegrationGateEvaluation)
        require(len(self.gates) == 8 and {v.gate for v in self.gates} == set(IntegrationGate), "GATES")
        require(type(self.metadata) is SemanticCallMetadata, "METADATA")
        replace(self.metadata)

    @property
    def decision(self):
        return "PASS" if all(v.verdict is IntegrationVerdict.PASS for v in self.gates) else "REJECT"


@dataclass(frozen=True, slots=True)
class IntegrationPlan:
    schema_version: int
    integration_contract_version: int
    source_reference: GoldSourceArtifactReference
    base_reference: GoldSnapshotReference
    operations: tuple[GoldIntegrationOperation, ...] = field(repr=False)
    proposed_layout: tuple[GoldDocumentLayout, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        require(all(type(v) is int and v == 1 for v in
                    (self.schema_version, self.integration_contract_version)), "PLAN_VERSION")
        for value, kind in ((self.source_reference, GoldSourceArtifactReference),
                            (self.base_reference, GoldSnapshotReference), (self.metadata, SemanticCallMetadata)):
            require(type(value) is kind, "PLAN_CONTEXT")
            replace(value)
        members(self.operations, GoldIntegrationOperation)
        members(self.proposed_layout, GoldDocumentLayout)
        require(bool(self.operations) and len({o.operation_id for o in self.operations}) == len(self.operations),
                "OPERATIONS")
        require(len({d.document_id for d in self.proposed_layout}) == len(self.proposed_layout), "LAYOUT")


@dataclass(frozen=True, slots=True)
class IntegrationRound:
    number: int
    plan: IntegrationPlan = field(repr=False)
    evaluation: IntegrationEvaluation

    def __post_init__(self):
        require(type(self.number) is int and 1 <= self.number <= 3, "ROUND")
        members((self.plan,), IntegrationPlan)
        members((self.evaluation,), IntegrationEvaluation)


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    max_semantic_rounds: int = 3
    max_inspection_calls: int = defaults.INTEGRATION_MAX_INSPECTION_CALLS
    max_inspection_bytes: int = defaults.INTEGRATION_MAX_INSPECTION_BYTES
    max_agent_exchanges: int = defaults.INTEGRATION_MAX_AGENT_EXCHANGES
    agent_output_limit: int = defaults.INTEGRATION_AGENT_OUTPUT_LIMIT
    validator_output_limit: int = defaults.INTEGRATION_VALIDATOR_OUTPUT_LIMIT

    def __post_init__(self):
        require(type(self.max_semantic_rounds) is int and 1 <= self.max_semantic_rounds <= 3, "ROUND_LIMIT")
        for name in ("max_inspection_calls", "max_inspection_bytes", "max_agent_exchanges",
                     "agent_output_limit", "validator_output_limit"):
            require(type(getattr(self, name)) is int and getattr(self, name) > 0, "LIMIT")


@dataclass(frozen=True, slots=True)
class IntegrationAgentRequest:
    source: "IntegrationSource" = field(repr=False)
    inspection: "IntegrationInspection" = field(repr=False)
    config: IntegrationConfig
    previous_plan: IntegrationPlan | None = field(default=None, repr=False)
    feedback: IntegrationEvaluation | None = None


@dataclass(frozen=True, slots=True)
class IntegrationValidatorRequest:
    source: "IntegrationSource" = field(repr=False)
    plan: IntegrationPlan = field(repr=False)
    inspected_context: tuple[str, ...] = field(repr=False)
    output_limit: int


class IntegrationAgent(Protocol):
    def propose(self, request: IntegrationAgentRequest) -> IntegrationPlan: ...


class IntegrationValidator(Protocol):
    def evaluate(self, request: IntegrationValidatorRequest) -> IntegrationEvaluation: ...


@dataclass(frozen=True, slots=True)
class PreparedIntegrationResult:
    schema_version: int
    integration_contract_version: int
    source_reference: GoldSourceArtifactReference
    source_item_ids: tuple[str, ...]
    base_reference: GoldSnapshotReference
    base_snapshot: GoldSnapshot = field(repr=False)
    candidate_snapshot: GoldSnapshot = field(repr=False)
    plan: IntegrationPlan | None = field(repr=False)
    rounds: tuple[IntegrationRound, ...]
    outcome: str
    effects: "IntegrationEffects" = field(repr=False)
    base_sha256: str
    candidate_sha256: str
    context: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        require(all(type(v) is int and v == 1 for v in
                    (self.schema_version, self.integration_contract_version)), "RESULT_VERSION")
        for value, kind in ((self.source_reference, GoldSourceArtifactReference),
                            (self.base_reference, GoldSnapshotReference), (self.base_snapshot, GoldSnapshot),
                            (self.candidate_snapshot, GoldSnapshot)):
            require(type(value) is kind, "RESULT_CONTEXT")
            replace(value)
        strings(self.source_item_ids)
        members(self.rounds, IntegrationRound)
        require(self.outcome in ("PREPARED_CHANGE", "PREPARED_NO_CHANGE", "NO_SOURCE_KNOWLEDGE"), "OUTCOME")
        require(type(self.context) is tuple and all(type(p) is tuple and len(p) == 2 for p in self.context), "CONTEXT")
        require(len({p[0] for p in self.context}) == len(self.context), "CONTEXT")
        for pair in self.context:
            for value in pair:
                _text(value)

    @property
    def would_change_gold(self):
        return self.base_snapshot != self.candidate_snapshot
