"""Pure Gold validation and strict in-memory codec; no writer or integration agent."""

from collections.abc import Mapping
import json

from ._deepseek_json import strict_json
from .gold_models import (
    GoldContractError, GoldContractState, GoldDocumentVersion, GoldKnowledgeUnit,
    GoldProvenance, GoldReferenceError, GoldRelation, GoldRelationKind,
    GoldSourceArtifactReference, GoldStatus, _identity, _text,
)


def validate_gold_unit(unit: GoldKnowledgeUnit) -> GoldKnowledgeUnit:
    if type(unit) is not GoldKnowledgeUnit:
        raise GoldContractError("UNIT")
    unit._validate()
    return unit


def validate_gold_state(state: GoldContractState) -> GoldContractState:
    """Check all loaded identities; this does not certify semantic truth or lineage."""
    if type(state) is not GoldContractState:
        raise GoldContractError("STATE")
    state._validate()
    units = {unit.knowledge_id: unit for unit in state.units}
    for unit in state.units:
        if unit.merged_into is not None and unit.merged_into not in units:
            raise GoldReferenceError("UNKNOWN_SURVIVOR")
        if any(r.target_knowledge_id not in units for r in unit.relations):
            raise GoldReferenceError("UNKNOWN_RELATION_TARGET")
    # Memoize completed chains so a long history is checked in linear time.
    completed = set()
    for identity in units:
        pending = set()
        current = identity
        while current not in completed:
            if current in pending:
                raise GoldReferenceError("MERGE_CYCLE")
            pending.add(current)
            unit = units[current]
            if unit.status is GoldStatus.ACTIVE:
                break
            current = unit.merged_into
        completed.update(pending)
    return state


def validate_gold_provenance(state: GoldContractState, *,
                             source_items_by_artifact: Mapping[GoldSourceArtifactReference, frozenset[str]]) -> None:
    """Check references against caller-verified source context, without loading files.

    The caller must obtain this complete context from authoritative source readers
    and exact bytes/hashes. A supplied mapping is not itself proof of source validity.
    """
    validate_gold_state(state)
    if not isinstance(source_items_by_artifact, Mapping):
        raise GoldContractError("SOURCE_CONTEXT")
    for reference, items in source_items_by_artifact.items():
        if type(reference) is not GoldSourceArtifactReference or type(items) is not frozenset:
            raise GoldContractError("SOURCE_CONTEXT")
        reference._validate()
        for identity in items:
            _text(identity)
    for unit in state.units:
        for entry in unit.provenance:
            items = source_items_by_artifact.get(entry.source_knowledge_artifact)
            if items is None:
                raise GoldReferenceError("UNKNOWN_SOURCE_ARTIFACT")
            if entry.source_item_id not in items:
                raise GoldReferenceError("UNKNOWN_SOURCE_ITEM")


def _get_unit(state, knowledge_id):
    _identity(knowledge_id)
    for unit in state.units:
        if unit.knowledge_id == knowledge_id:
            return unit
    raise GoldReferenceError("UNKNOWN_IDENTITY")


def get_gold_unit(state: GoldContractState, knowledge_id: str) -> GoldKnowledgeUnit:
    validate_gold_state(state)
    return _get_unit(state, knowledge_id)


def resolve_knowledge_id(state: GoldContractState, knowledge_id: str) -> str:
    validate_gold_state(state)
    unit = _get_unit(state, knowledge_id)
    units = {u.knowledge_id: u for u in state.units}
    while unit.status is GoldStatus.MERGED:
        unit = units[unit.merged_into]
    return unit.knowledge_id


def gold_unit_document(unit: GoldKnowledgeUnit) -> dict:
    validate_gold_unit(unit)
    return {"knowledge_id": unit.knowledge_id, "content": unit.content,
            "provenance": [{"source_knowledge_artifact": {
                "source_type": p.source_knowledge_artifact.source_type,
                "source_id": p.source_knowledge_artifact.source_id,
                "relative_path": p.source_knowledge_artifact.relative_path,
                "sha256": p.source_knowledge_artifact.sha256,
                "schema_version": p.source_knowledge_artifact.schema_version,
                "audit_contract_version": p.source_knowledge_artifact.audit_contract_version},
                "source_item_id": p.source_item_id}
                for p in sorted(unit.provenance, key=GoldProvenance._sort_key)],
            "status": unit.status.value,
            "relations": [{"kind": r.kind.value, "target_knowledge_id": r.target_knowledge_id}
                          for r in sorted(unit.relations, key=GoldRelation._sort_key)],
            "merged_into": unit.merged_into}


def gold_state_document(state: GoldContractState) -> dict:
    validate_gold_state(state)
    return {"schema_version": state.schema_version, "contract_version": state.contract_version,
            "units": [gold_unit_document(u) for u in sorted(state.units, key=lambda u: u.knowledge_id)]}


def encode_gold_state(state: GoldContractState) -> bytes:
    return (json.dumps(gold_state_document(state), ensure_ascii=False, sort_keys=True,
                       allow_nan=False, indent=2) + "\n").encode("utf-8")


def _keys(document, names):
    if type(document) is not dict or set(document) != set(names.split()):
        raise GoldContractError("FIELDS")
    return document


def _array(value):
    if type(value) is not list:
        raise GoldContractError("ARRAY")
    return value


def decode_gold_unit_document(document: dict) -> GoldKnowledgeUnit:
    _keys(document, "knowledge_id content provenance status relations merged_into")
    provenance, relations = [], []
    for entry in _array(document["provenance"]):
        _keys(entry, "source_knowledge_artifact source_item_id")
        reference = _keys(entry["source_knowledge_artifact"],
                          "source_type source_id relative_path sha256 schema_version audit_contract_version")
        provenance.append(GoldProvenance(GoldSourceArtifactReference(**reference), entry["source_item_id"]))
    for relation in _array(document["relations"]):
        _keys(relation, "kind target_knowledge_id")
        try:
            kind = GoldRelationKind(relation["kind"])
        except (ValueError, TypeError):
            raise GoldContractError("RELATION_KIND") from None
        relations.append(GoldRelation(kind, relation["target_knowledge_id"]))
    try:
        status = GoldStatus(document["status"])
    except (ValueError, TypeError):
        raise GoldContractError("STATUS") from None
    return GoldKnowledgeUnit(document["knowledge_id"], document["content"], tuple(provenance),
                             status, tuple(relations), document["merged_into"])


def decode_gold_state(content: bytes) -> GoldContractState:
    if type(content) is not bytes:
        raise GoldContractError("JSON_BYTES")
    try:
        document = strict_json(content.decode("utf-8"))
        _keys(document, "schema_version contract_version units")
        # Reject unsupported versions before interpreting a possibly different unit schema.
        GoldContractState(document["schema_version"], document["contract_version"])
        units = tuple(decode_gold_unit_document(u) for u in _array(document["units"]))
        state = GoldContractState(document["schema_version"], document["contract_version"], units)
    except GoldContractError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GoldContractError("JSON") from None
    return validate_gold_state(state)


def gold_document_version_document(value: GoldDocumentVersion) -> dict:
    if type(value) is not GoldDocumentVersion:
        raise GoldContractError("DOCUMENT_VERSION")
    value._validate()
    return {"document_id": value.document_id, "document_version": value.document_version}


def decode_gold_document_version(document: dict) -> GoldDocumentVersion:
    return GoldDocumentVersion(**_keys(document, "document_id document_version"))
