"""Immutable durable integration/history contracts, separate from preparation."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import re

from .gold_application import DocumentEffect, IntegrationEffects
from .gold_governance_models import GoldOperationKind, GoldPlacementIntent, GoldProvenanceAddition, GoldRelationIntent
from .gold_models import GoldSourceArtifactReference, _identity, _text
from .gold_store import GoldSnapshotReference
from .gold_document_models import GoldStorageError
from .selection_models import SemanticCallMetadata

HISTORY_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
DURABLE_INTEGRATION_CONTRACT_VERSION = 1


class GoldHistoryError(GoldStorageError):
    """Sanitized persistent-state or request error; never echoes stored content."""


class GoldHistoryCompatibilityError(GoldHistoryError):
    pass


class GoldCommitError(GoldHistoryError):
    pass


def check(condition, code):
    if not condition:
        raise GoldHistoryError(code)


def version(value):
    if type(value) is not int or value != 1:
        raise GoldHistoryCompatibilityError("HISTORY_VERSION")


def digest(value):
    check(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "HASH")


def date(value):
    check(isinstance(value, str), "TIMESTAMP")
    try:
        result = datetime.fromisoformat(value)
        check(result.tzinfo is not None and result.utcoffset() == timezone.utc.utcoffset(result), "TIMESTAMP")
        return result
    except (ValueError, OverflowError):
        raise GoldHistoryError("TIMESTAMP") from None


def strings(values):
    check(type(values) is tuple and all(type(v) is str for v in values), "COLLECTION")
    check(len(set(values)) == len(values), "DUPLICATE")
    for value in values:
        _text(value)


def members(values, kind):
    check(type(values) is tuple and all(type(v) is kind for v in values), "COLLECTION")
    for value in values:
        replace(value)


@dataclass(frozen=True, slots=True)
class GoldHistoryReference:
    commit_id: str
    sha256: str

    def __post_init__(self):
        check(isinstance(self.commit_id, str) and re.fullmatch(r"[0-9a-f]{32}", self.commit_id) is not None, "HISTORY_ID")
        digest(self.sha256)


class GoldEventType(str, Enum):
    KNOWLEDGE_CREATED = "KNOWLEDGE_CREATED"
    KNOWLEDGE_ENRICHED = "KNOWLEDGE_ENRICHED"
    KNOWLEDGE_REWRITTEN = "KNOWLEDGE_REWRITTEN"
    KNOWLEDGE_MERGED = "KNOWLEDGE_MERGED"
    RELATION_ADDED = "RELATION_ADDED"
    CONFLICT_FLAGGED = "CONFLICT_FLAGGED"
    PROVENANCE_ADDED = "PROVENANCE_ADDED"
    NO_CHANGE_RECORDED = "NO_CHANGE_RECORDED"


@dataclass(frozen=True, slots=True)
class GoldCreatedPlacement:
    knowledge_id: str
    placement: GoldPlacementIntent

    def __post_init__(self):
        _identity(self.knowledge_id)
        check(type(self.placement) is GoldPlacementIntent, "PLACEMENT")
        replace(self.placement)


@dataclass(frozen=True, slots=True)
class GoldOperationAudit:
    operation_id: str
    kind: GoldOperationKind
    source_item_ids: tuple[str, ...]
    affected_knowledge_ids: tuple[str, ...]
    new_placements: tuple[GoldCreatedPlacement, ...]
    support: tuple[GoldProvenanceAddition, ...]
    relations: tuple[GoldRelationIntent, ...]
    survivor_id: str | None
    merged_ids: tuple[str, ...]
    conflict_reason: str | None = field(repr=False)
    rationale: str = field(repr=False)

    def __post_init__(self):
        _text(self.operation_id)
        _text(self.rationale)
        check(type(self.kind) is GoldOperationKind, "OPERATION_KIND")
        for values in (self.source_item_ids, self.affected_knowledge_ids, self.merged_ids):
            strings(values)
        check(bool(self.source_item_ids), "SOURCE_ACCOUNTING")
        for value in self.affected_knowledge_ids + self.merged_ids:
            _identity(value)
        members(self.new_placements, GoldCreatedPlacement)
        members(self.support, GoldProvenanceAddition)
        members(self.relations, GoldRelationIntent)
        for values in (self.new_placements, self.support, self.relations):
            check(len(set(values)) == len(values), "DUPLICATE")
        check(self.kind in (GoldOperationKind.CREATE, GoldOperationKind.FLAG_CONFLICT)
              or not self.new_placements, "PLACEMENT_AUTHORITY")
        if self.kind is GoldOperationKind.MERGE:
            _identity(self.survivor_id)
            check(bool(self.merged_ids) and self.survivor_id not in self.merged_ids, "MERGE")
        else:
            check(self.survivor_id is None and not self.merged_ids, "MERGE")
        if self.kind is GoldOperationKind.FLAG_CONFLICT:
            _text(self.conflict_reason)
        else:
            check(self.conflict_reason is None, "CONFLICT")


@dataclass(frozen=True, slots=True)
class GoldRoundAudit:
    number: int
    agent: SemanticCallMetadata
    validator: SemanticCallMetadata
    decision: str

    def __post_init__(self):
        check(type(self.number) is int and 1 <= self.number <= 3, "ROUND")
        members((self.agent, self.validator), SemanticCallMetadata)
        check(self.decision in ("PASS", "REJECT"), "ROUND")


@dataclass(frozen=True, slots=True)
class GoldChangeEvent:
    schema_version: int
    event_id: str
    integration_id: str
    event_type: GoldEventType
    timestamp: str
    source_reference: GoldSourceArtifactReference
    source_item_ids: tuple[str, ...]
    operation_ids: tuple[str, ...]
    affected_knowledge_ids: tuple[str, ...]
    unit_change_ids: tuple[str, ...]
    document_changes: tuple[DocumentEffect, ...]
    effect_index: int | None
    survivor_id: str | None
    merged_ids: tuple[str, ...]
    reason: str | None
    rationales: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        version(self.schema_version)
        _identity(self.integration_id, "integration")
        check(isinstance(self.event_id, str) and re.fullmatch(r"event-[0-9a-f]{32}-[0-9]{6,}", self.event_id) is not None, "EVENT_ID")
        check(type(self.event_type) is GoldEventType, "EVENT_TYPE")
        date(self.timestamp)
        check(type(self.source_reference) is GoldSourceArtifactReference, "SOURCE_REFERENCE")
        replace(self.source_reference)
        for values in (self.source_item_ids, self.operation_ids, self.affected_knowledge_ids,
                       self.unit_change_ids, self.merged_ids, self.rationales):
            strings(values)
        members(self.document_changes, DocumentEffect)
        for identity in self.affected_knowledge_ids + self.unit_change_ids + self.merged_ids:
            _identity(identity)
        if self.survivor_id is not None:
            _identity(self.survivor_id)
        check(self.effect_index is None or type(self.effect_index) is int and self.effect_index >= 0, "EFFECT_INDEX")
        if self.reason is not None:
            _text(self.reason)


@dataclass(frozen=True, slots=True)
class GoldIntegrationRecord:
    schema_version: int
    integration_contract_version: int
    preparation_schema_version: int
    preparation_contract_version: int
    integration_id: str
    timestamp: str
    source_reference: GoldSourceArtifactReference
    source_item_ids: tuple[str, ...]
    base_reference: GoldSnapshotReference
    result_reference: GoldSnapshotReference
    base_sha256: str
    candidate_sha256: str
    outcome: str
    operations: tuple[GoldOperationAudit, ...] = field(repr=False)
    rounds: tuple[GoldRoundAudit, ...]
    context: tuple[tuple[str, str], ...]
    effects: IntegrationEffects = field(repr=False)
    events: tuple[GoldChangeEvent, ...]

    def __post_init__(self):
        for v in (self.schema_version, self.integration_contract_version,
                  self.preparation_schema_version, self.preparation_contract_version):
            version(v)
        _identity(self.integration_id, "integration")
        date(self.timestamp)
        for value, kind in ((self.source_reference, GoldSourceArtifactReference),
                            (self.base_reference, GoldSnapshotReference), (self.result_reference, GoldSnapshotReference)):
            check(type(value) is kind, "REFERENCE")
            replace(value)
        version(self.source_reference.schema_version)
        version(self.source_reference.audit_contract_version)
        digest(self.base_sha256)
        digest(self.candidate_sha256)
        strings(self.source_item_ids)
        members(self.operations, GoldOperationAudit)
        members(self.rounds, GoldRoundAudit)
        members(self.events, GoldChangeEvent)
        check(type(self.effects) is IntegrationEffects, "EFFECTS")
        check(self.outcome in ("PREPARED_CHANGE", "PREPARED_NO_CHANGE", "NO_SOURCE_KNOWLEDGE"), "OUTCOME")
        check(type(self.context) is tuple and all(type(p) is tuple and len(p) == 2 for p in self.context), "CONTEXT")
        check(len({p[0] for p in self.context}) == len(self.context), "CONTEXT")
        for key, value in self.context:
            check(key in ("logical_run_id", "target_id", "selected_video_id"), "CONTEXT")
            _text(value)
        selected = dict(self.context).get("selected_video_id")
        check(selected is None or selected == self.source_reference.source_id, "SOURCE_CONTEXT")


@dataclass(frozen=True, slots=True)
class GoldCommitResult:
    integration: GoldIntegrationRecord
    gold_reference: GoldSnapshotReference
    history_reference: GoldHistoryReference
    reused: bool
