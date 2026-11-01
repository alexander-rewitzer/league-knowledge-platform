"""Immutable transient Gold operation intents; no execution or durable state."""

from dataclasses import dataclass, field, replace
from enum import Enum

from .gold_models import (
    GoldContractError, GoldKnowledgeUnit, GoldProvenance, GoldRelation,
    GoldRelationKind, GoldStatus, _identity, _text,
)

OPERATION_SCHEMA_VERSION = 1
GOVERNANCE_CONTRACT_VERSION = 1


class GoldGovernanceError(GoldContractError):
    """Sanitized structural error, never a semantic rejection or event outcome."""

    def __init__(self, code, location="operation"):
        self.location = location
        super().__init__(code)


def _require(condition, code, location):
    if not condition:
        raise GoldGovernanceError(code, location)


def _checked(value, kind, location):
    _require(type(value) is kind, "TYPE", location)
    try:
        replace(value)
    except GoldGovernanceError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise GoldGovernanceError("VALUE", location) from None


def _string(value, location, prefix=None):
    try:
        if prefix is None:
            _text(value)
        else:
            _identity(value, prefix)
    except (ValueError, TypeError):
        raise GoldGovernanceError("IDENTITY" if prefix else "TEXT", location) from None


def _values(value, kind, location, *, nonempty=False, key=None):
    _require(type(value) is tuple and (not nonempty or bool(value)), "COLLECTION", location)
    for member in value:
        _checked(member, kind, location)
    _require(len(set(value)) == len(value), "DUPLICATE", location)
    return tuple(sorted(value, key=key)) if key else value


def _ids(value, location, *, minimum=1):
    _require(type(value) is tuple and len(value) >= minimum, "CARDINALITY", location)
    for identity in value:
        _string(identity, location, "gku")
    _require(len(set(value)) == len(value), "DUPLICATE", location)
    return tuple(sorted(value))


class GoldOperationKind(str, Enum):
    NO_CHANGE = "NO_CHANGE"
    CREATE = "CREATE"
    ENRICH = "ENRICH"
    REWRITE = "REWRITE"
    MERGE = "MERGE"
    LINK = "LINK"
    FLAG_CONFLICT = "FLAG_CONFLICT"


@dataclass(frozen=True, slots=True)
class GoldPlacementIntent:
    document_id: str
    topic_id: str
    position: int

    def __post_init__(self):
        _string(self.document_id, "placement.document_id", "gdoc")
        _string(self.topic_id, "placement.topic_id", "topic")
        _require(type(self.position) is int and self.position >= 0, "POSITION", "placement.position")


@dataclass(frozen=True, slots=True)
class GoldProvenanceAddition:
    knowledge_id: str
    provenance: GoldProvenance = field(repr=False)

    def __post_init__(self):
        _string(self.knowledge_id, "support.knowledge_id", "gku")
        _checked(self.provenance, GoldProvenance, "support.provenance")

    def _sort_key(self):
        return self.knowledge_id, self.provenance._sort_key()


@dataclass(frozen=True, slots=True)
class GoldRelationIntent:
    source_knowledge_id: str
    relation: GoldRelation

    def __post_init__(self):
        _string(self.source_knowledge_id, "relation.source_knowledge_id", "gku")
        _checked(self.relation, GoldRelation, "relation")
        _require(self.source_knowledge_id != self.relation.target_knowledge_id, "SELF_RELATION", "relation")

    def _sort_key(self):
        return self.source_knowledge_id, self.relation._sort_key()


@dataclass(frozen=True, slots=True)
class GoldNewUnitIntent:
    unit: GoldKnowledgeUnit = field(repr=False)
    placement: GoldPlacementIntent

    def __post_init__(self):
        _checked(self.unit, GoldKnowledgeUnit, "new_unit")
        _checked(self.placement, GoldPlacementIntent, "placement")
        _require(self.unit.status is GoldStatus.ACTIVE, "NEW_UNIT_STATUS", "new_unit.status")


def _support(value):
    return _values(value, GoldProvenanceAddition, "provenance_additions", key=GoldProvenanceAddition._sort_key)


@dataclass(frozen=True, slots=True)
class GoldNoChangePayload:
    target_knowledge_ids: tuple[str, ...]
    provenance_additions: tuple[GoldProvenanceAddition, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "target_knowledge_ids", _ids(self.target_knowledge_ids, "targets"))
        object.__setattr__(self, "provenance_additions", _support(self.provenance_additions))
        _require(all(p.knowledge_id in self.target_knowledge_ids for p in self.provenance_additions),
                 "SUPPORT_TARGET", "provenance_additions")


@dataclass(frozen=True, slots=True)
class GoldCreatePayload:
    new_unit: GoldNewUnitIntent

    def __post_init__(self):
        _checked(self.new_unit, GoldNewUnitIntent, "new_unit")


@dataclass(frozen=True, slots=True)
class GoldEnrichPayload:
    target_knowledge_id: str
    resulting_unit: GoldKnowledgeUnit = field(repr=False)

    def __post_init__(self):
        _replacement(self.target_knowledge_id, self.resulting_unit)


def _replacement(identity, unit):
    _string(identity, "target_knowledge_id", "gku")
    _checked(unit, GoldKnowledgeUnit, "resulting_unit")
    _require(unit.knowledge_id == identity and unit.status is GoldStatus.ACTIVE,
             "RESULT_IDENTITY", "resulting_unit")


@dataclass(frozen=True, slots=True)
class GoldRewritePayload:
    target_knowledge_id: str
    resulting_unit: GoldKnowledgeUnit = field(repr=False)

    def __post_init__(self):
        _replacement(self.target_knowledge_id, self.resulting_unit)


@dataclass(frozen=True, slots=True)
class GoldMergePayload:
    target_knowledge_ids: tuple[str, ...]
    survivor_unit: GoldKnowledgeUnit = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "target_knowledge_ids", _ids(self.target_knowledge_ids, "targets", minimum=2))
        _checked(self.survivor_unit, GoldKnowledgeUnit, "survivor_unit")
        _require(self.survivor_unit.status is GoldStatus.ACTIVE
                 and self.survivor_unit.knowledge_id in self.target_knowledge_ids,
                 "SURVIVOR", "survivor_unit")


@dataclass(frozen=True, slots=True)
class GoldLinkPayload:
    relation: GoldRelationIntent

    def __post_init__(self):
        _checked(self.relation, GoldRelationIntent, "relation")


@dataclass(frozen=True, slots=True)
class GoldFlagConflictPayload:
    claim_knowledge_ids: tuple[str, ...]
    new_claims: tuple[GoldNewUnitIntent, ...]
    contradictions: tuple[GoldRelationIntent, ...]
    provenance_additions: tuple[GoldProvenanceAddition, ...]
    conflict_reason: str = field(repr=False)

    def __post_init__(self):
        claims = _ids(self.claim_knowledge_ids, "claims", minimum=2)
        object.__setattr__(self, "claim_knowledge_ids", claims)
        new = _values(self.new_claims, GoldNewUnitIntent, "new_claims", key=lambda n: n.unit.knowledge_id)
        object.__setattr__(self, "new_claims", new)
        _require(len({n.unit.knowledge_id for n in new}) == len(new)
                 and all(n.unit.knowledge_id in claims for n in new), "CLAIM_IDENTITY", "new_claims")
        edges = _values(self.contradictions, GoldRelationIntent, "contradictions", nonempty=True,
                        key=GoldRelationIntent._sort_key)
        object.__setattr__(self, "contradictions", edges)
        covered = set()
        for edge in edges:
            ends = {edge.source_knowledge_id, edge.relation.target_knowledge_id}
            _require(edge.relation.kind is GoldRelationKind.CONTRADICTS and ends <= set(claims),
                     "CONFLICT_RELATION", "contradictions")
            covered.update(ends)
        _require(covered == set(claims), "UNCOVERED_CLAIM", "claims")
        object.__setattr__(self, "provenance_additions", _support(self.provenance_additions))
        _require(all(p.knowledge_id in claims for p in self.provenance_additions),
                 "SUPPORT_TARGET", "provenance_additions")
        _string(self.conflict_reason, "conflict_reason")


_PAYLOADS = {
    GoldOperationKind.NO_CHANGE: GoldNoChangePayload,
    GoldOperationKind.CREATE: GoldCreatePayload,
    GoldOperationKind.ENRICH: GoldEnrichPayload,
    GoldOperationKind.REWRITE: GoldRewritePayload,
    GoldOperationKind.MERGE: GoldMergePayload,
    GoldOperationKind.LINK: GoldLinkPayload,
    GoldOperationKind.FLAG_CONFLICT: GoldFlagConflictPayload,
}


@dataclass(frozen=True, slots=True)
class GoldIntegrationOperation:
    operation_id: str
    kind: GoldOperationKind
    source_refs: tuple[GoldProvenance, ...] = field(repr=False)
    rationale: str = field(repr=False)
    payload: (GoldNoChangePayload | GoldCreatePayload | GoldEnrichPayload | GoldRewritePayload
              | GoldMergePayload | GoldLinkPayload | GoldFlagConflictPayload) = field(repr=False)

    def __post_init__(self):
        _string(self.operation_id, "operation_id")
        _string(self.rationale, "rationale")
        _require(type(self.kind) is GoldOperationKind, "OPERATION_KIND", "kind")
        object.__setattr__(self, "source_refs", _values(self.source_refs, GoldProvenance,
            "source_refs", nonempty=True, key=GoldProvenance._sort_key))
        _checked(self.payload, _PAYLOADS[self.kind], "payload")
