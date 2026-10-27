"""Pure structural governance and transient interchange, never semantic acceptance."""

from dataclasses import asdict, dataclass, fields
from enum import Enum
import json

from ._deepseek_json import strict_json
from .gold_contract import decode_gold_unit_document, validate_gold_state
from .gold_document_models import GoldDocumentLayout
from .gold_documents import validate_gold_snapshot
from .gold_models import (
    GoldContractState, GoldKnowledgeUnit, GoldProvenance, GoldRelation, GoldRelationKind,
    GoldSourceArtifactReference, GoldStatus,
)
from .gold_governance_models import (
    GOVERNANCE_CONTRACT_VERSION, OPERATION_SCHEMA_VERSION, GoldCreatePayload,
    GoldEnrichPayload, GoldFlagConflictPayload, GoldGovernanceError,
    GoldIntegrationOperation, GoldLinkPayload, GoldMergePayload, GoldNewUnitIntent,
    GoldNoChangePayload, GoldOperationKind, GoldPlacementIntent, GoldProvenanceAddition,
    GoldRelationIntent, GoldRewritePayload, _PAYLOADS, _checked, _require, _string, _values,
)


@dataclass(frozen=True, slots=True)
class GoldGovernanceRule:
    kind: GoldOperationKind
    applies_when: str
    preserve: str
    prohibited: str
    ownership: str

    def __post_init__(self):
        _require(type(self.kind) is GoldOperationKind, "OPERATION_KIND", "rule.kind")
        for name in ("applies_when", "preserve", "prohibited", "ownership"):
            _string(getattr(self, name), "rule." + name)


GOLD_GOVERNANCE_RULES = (
    GoldGovernanceRule(GoldOperationKind.NO_CHANGE,
        "Incoming knowledge is already substantively represented in existing Gold.",
        "Existing identity and knowledge; supporting provenance may be proposed explicitly.",
        "Duplicate CREATE merely for another source; fabricated content or version changes.",
        "Semantic appropriateness and effects belong to integration; events belong to audit history."),
    GoldGovernanceRule(GoldOperationKind.CREATE,
        "Genuinely new source-grounded knowledge needs a new unit.",
        "New stable global identity, supporting provenance and bounded placement.",
        "Semantic duplicates of represented knowledge; replacing an existing identity.",
        "Application code supplies identities; future integration executes accepted intent."),
    GoldGovernanceRule(GoldOperationKind.ENRICH,
        "Substantive conditions, exceptions, qualifiers, causal explanation or useful detail extend the same valid unit.",
        "Same knowledge_id, valid existing knowledge, conditions, provenance and relations.",
        "Knowledge loss, cosmetic-only change or hidden conflict resolution.",
        "Semantic preservation and appropriateness require the future integration Validator."),
    GoldGovernanceRule(GoldOperationKind.REWRITE,
        "Accumulated source-grounded knowledge of the same unit needs substantive integrated reformulation.",
        "Same knowledge_id and valid knowledge, conditions, provenance and relations.",
        "Cosmetic-only rewriting, unsupported inference and source-fact correction.",
        "Complete resulting content is proposed; structural validity is not semantic PASS."),
    GoldGovernanceRule(GoldOperationKind.MERGE,
        "Unnecessarily fragmented existing units should form one coherent unit.",
        "One existing survivor, combined valid meaning/provenance and all historical IDs/links.",
        "Lossful consolidation, invented survivor, erased identities or silent chain flattening.",
        "Future Writer retains non-survivors as MERGED with original records and merged_into."),
    GoldGovernanceRule(GoldOperationKind.LINK,
        "Distinct units have a meaningful supported relation and remain distinct.",
        "Stable endpoints and existing directional related_to/contradicts semantics.",
        "Self-links, new relation ontology or inferred reciprocal edges.",
        "Existing edges may be recognized; integration and audit own actual effects."),
    GoldGovernanceRule(GoldOperationKind.FLAG_CONFLICT,
        "Material incompatibility under equivalent relevant conditions cannot safely be reconciled.",
        "Incoming/existing claims, stable IDs, provenance and explicit conflict relationships/details.",
        "Winner selection, majority voting, silent harmonization, correction or global-truth claims.",
        "Future semantic validation judges incompatibility; governance preserves its representation."),
)


def validate_gold_operation_shape(operation):
    """Revalidate nested immutable values; this cannot establish semantic correctness."""
    _checked(operation, GoldIntegrationOperation, "operation")
    return operation


def _new_intents(payload):
    if type(payload) is GoldCreatePayload:
        return (payload.new_unit,)
    if type(payload) is GoldFlagConflictPayload:
        return payload.new_claims
    return ()


def validate_gold_operation(operation, *, base_snapshot, incoming_artifact, incoming_item_ids,
                            declared_new_units=(), proposed_layout=()):
    """Validate one intent against caller-verified context, without loading or writing.

    Callers verify canonical source input and its exact reference/hash beforehand.
    Declarations are transient context, not existing identities or accepted knowledge.
    Complete-plan accounting, semantic review, staleness and execution remain external.
    Existing support/edges produce no derived mutation or Change Event behavior.
    """
    validate_gold_operation_shape(operation)
    try:
        return _validate_context(operation, base_snapshot, incoming_artifact, incoming_item_ids,
                                 declared_new_units, proposed_layout)
    except GoldGovernanceError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise GoldGovernanceError("CONTEXT", "context") from None


def _validate_context(operation, snapshot, artifact, item_ids, declarations, layouts):
    base = {u.knowledge_id: u for u in validate_gold_snapshot(snapshot).units}
    _checked(artifact, GoldSourceArtifactReference, "incoming_artifact")
    _require(type(item_ids) is frozenset, "SOURCE_CONTEXT", "incoming_item_ids")
    for identity in item_ids:
        _string(identity, "incoming_item_ids")
    incoming = {GoldProvenance(artifact, identity) for identity in item_ids}
    _require(set(operation.source_refs) <= incoming, "SOURCE_MEMBERSHIP", "source_refs")
    _values(declarations, GoldKnowledgeUnit, "declared_new_units")
    _require(len({u.knowledge_id for u in declarations}) == len(declarations),
             "DUPLICATE_IDENTITY", "declared_new_units")
    allowed_support = incoming | {p for u in base.values() for p in u.provenance}
    known = dict(base)
    for unit in declarations:
        _require(unit.knowledge_id not in base and unit.status is GoldStatus.ACTIVE,
                 "NEW_IDENTITY", "declared_new_units")
        _require(set(unit.provenance) <= allowed_support, "PROVENANCE_REFERENCE", "declared_new_units")
        known[unit.knowledge_id] = unit
    new_intents = _new_intents(operation.payload)
    for intent in new_intents:
        unit = intent.unit
        _require(unit.knowledge_id not in base, "NEW_IDENTITY", "new_unit")
        _require(unit.knowledge_id not in known or known[unit.knowledge_id] == unit,
                 "DECLARATION_MISMATCH", "new_unit")
        known[unit.knowledge_id] = unit
    validate_gold_state(GoldContractState(1, 1, tuple(known.values())))
    _values(layouts, GoldDocumentLayout, "proposed_layout")
    _require(len({d.document_id for d in layouts}) == len(layouts), "DUPLICATE_DOCUMENT", "proposed_layout")
    documents = {d.document_id: d for d in snapshot.documents}
    documents.update({d.document_id: d for d in layouts})
    for layout in layouts:
        _require(all(p.knowledge_id in known for t in layout.topics for p in t.units),
                 "UNKNOWN_PLACEMENT", "proposed_layout")

    def existing(identity):
        _require(identity in base and base[identity].status is GoldStatus.ACTIVE,
                 "ACTIVE_TARGET", "targets")
        return base[identity]

    def result(unit, preserved=()):
        _require(set(unit.provenance) <= allowed_support, "PROVENANCE_REFERENCE", "resulting_unit.provenance")
        required = set(operation.source_refs) | {p for old in preserved for p in old.provenance}
        _require(required <= set(unit.provenance), "PROVENANCE_PRESERVATION", "resulting_unit.provenance")
        _require(all(r.target_knowledge_id in known for r in unit.relations),
                 "UNKNOWN_RELATION_TARGET", "resulting_unit.relations")

    def additions(values, targets):
        for addition in values:
            _require(addition.knowledge_id in targets and addition.knowledge_id in known
                     and known[addition.knowledge_id].status is GoldStatus.ACTIVE,
                     "SUPPORT_TARGET", "provenance_additions")
            _require(addition.provenance in operation.source_refs, "SOURCE_MEMBERSHIP", "provenance_additions")
            # Existing support is valid context, not a mutation/event instruction.

    def relation(edge):
        _require(edge.source_knowledge_id in known and edge.relation.target_knowledge_id in known,
                 "UNKNOWN_RELATION_TARGET", "relation")
        # Preserve exact historical addressing and direction; do not derive effects.

    placements = set()
    for intent in new_intents:
        result(intent.unit)
        p = intent.placement
        _require(p.document_id in documents, "UNKNOWN_DOCUMENT", "placement.document_id")
        topics = {t.topic_id: t for t in documents[p.document_id].topics}
        _require(p.topic_id in topics, "UNKNOWN_TOPIC", "placement.topic_id")
        for placed in topics[p.topic_id].units:
            _require((placed.knowledge_id != intent.unit.knowledge_id or placed.position == p.position)
                     and (placed.position != p.position or placed.knowledge_id == intent.unit.knowledge_id),
                     "PLACEMENT_COLLISION", "placement.position")
        location = (p.document_id, p.topic_id, p.position)
        _require(location not in placements, "PLACEMENT_COLLISION", "placement.position")
        placements.add(location)

    payload = operation.payload
    if type(payload) is GoldNoChangePayload:
        for identity in payload.target_knowledge_ids:
            existing(identity)
        additions(payload.provenance_additions, payload.target_knowledge_ids)
    elif type(payload) in (GoldEnrichPayload, GoldRewritePayload):
        old = existing(payload.target_knowledge_id)
        result(payload.resulting_unit, (old,))
        _require(set(old.relations) <= set(payload.resulting_unit.relations),
                 "RELATION_PRESERVATION", "resulting_unit.relations")
    elif type(payload) is GoldMergePayload:
        originals = tuple(existing(identity) for identity in payload.target_knowledge_ids)
        result(payload.survivor_unit, originals)
        survivor = base[payload.survivor_unit.knowledge_id]
        _require(set(survivor.relations) <= set(payload.survivor_unit.relations),
                 "RELATION_PRESERVATION", "survivor_unit.relations")
        # Execution must retain every non-survivor's original record as MERGED;
        # this validator neither materializes that state nor rewrites merge history.
    elif type(payload) is GoldLinkPayload:
        relation(payload.relation)
    elif type(payload) is GoldFlagConflictPayload:
        _require(all(identity in known for identity in payload.claim_knowledge_ids), "UNKNOWN_CLAIM", "claims")
        for edge in payload.contradictions:
            relation(edge)
        additions(payload.provenance_additions, payload.claim_knowledge_ids)
    return operation


def gold_operation_document(operation):
    """Return transient interchange data, never a canonical operation artifact."""
    validate_gold_operation_shape(operation)
    return {"operation_schema_version": OPERATION_SCHEMA_VERSION,
            "governance_contract_version": GOVERNANCE_CONTRACT_VERSION, **_document_value(asdict(operation))}


def _document_value(value):
    if isinstance(value, Enum):
        return value.value
    if type(value) is dict:
        return {key: _document_value(member) for key, member in value.items()}
    if type(value) is tuple:
        return [_document_value(member) for member in value]
    return value


def encode_gold_operation(operation):
    return (json.dumps(gold_operation_document(operation), ensure_ascii=False, allow_nan=False,
                       sort_keys=True, indent=2) + "\n").encode("utf-8")


def _keys(value, names):
    _require(type(value) is dict and set(value) == set(names), "FIELDS", "json")
    return value


def _array(value):
    _require(type(value) is list, "ARRAY", "json")
    return value


def _provenance(value):
    _keys(value, ("source_knowledge_artifact", "source_item_id"))
    ref = _keys(value["source_knowledge_artifact"], (f.name for f in fields(GoldSourceArtifactReference)))
    return GoldProvenance(GoldSourceArtifactReference(**ref), value["source_item_id"])


def _new(value):
    _keys(value, ("unit", "placement"))
    placement = _keys(value["placement"], (f.name for f in fields(GoldPlacementIntent)))
    return GoldNewUnitIntent(decode_gold_unit_document(value["unit"]), GoldPlacementIntent(**placement))


def _edge(value):
    _keys(value, ("source_knowledge_id", "relation"))
    edge = _keys(value["relation"], ("kind", "target_knowledge_id"))
    return GoldRelationIntent(value["source_knowledge_id"],
                              GoldRelation(GoldRelationKind(edge["kind"]), edge["target_knowledge_id"]))


def _addition(value):
    _keys(value, ("knowledge_id", "provenance"))
    return GoldProvenanceAddition(value["knowledge_id"], _provenance(value["provenance"]))


def decode_gold_operation(content):
    """Decode shape only; callers must still validate trusted contextual references."""
    _require(type(content) is bytes, "JSON_BYTES", "json")
    try:
        document = strict_json(content.decode("utf-8"))
        _keys(document, ("operation_schema_version", "governance_contract_version",
                         "operation_id", "kind", "source_refs", "rationale", "payload"))
        for name, supported in (("operation_schema_version", OPERATION_SCHEMA_VERSION),
                                ("governance_contract_version", GOVERNANCE_CONTRACT_VERSION)):
            _require(type(document[name]) is int and document[name] == supported, "VERSION", name)
        kind = GoldOperationKind(document["kind"])
        payload_type = _PAYLOADS[kind]
        value = dict(_keys(document["payload"], (f.name for f in fields(payload_type))))
        for name in ("target_knowledge_ids", "claim_knowledge_ids"):
            if name in value:
                value[name] = tuple(_array(value[name]))
        if "new_unit" in value:
            value["new_unit"] = _new(value["new_unit"])
        for name in ("resulting_unit", "survivor_unit"):
            if name in value:
                value[name] = decode_gold_unit_document(value[name])
        if "relation" in value:
            value["relation"] = _edge(value["relation"])
        for name, decode in (("provenance_additions", _addition), ("new_claims", _new), ("contradictions", _edge)):
            if name in value:
                value[name] = tuple(decode(v) for v in _array(value[name]))
        return GoldIntegrationOperation(document["operation_id"], kind,
            tuple(_provenance(v) for v in _array(document["source_refs"])), document["rationale"], payload_type(**value))
    except GoldGovernanceError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, RecursionError):
        raise GoldGovernanceError("JSON", "json") from None
