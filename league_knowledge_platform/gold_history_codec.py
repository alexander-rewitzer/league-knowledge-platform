"""Strict deterministic history interchange; no model responses or operation store."""

from dataclasses import asdict, fields

from .gold_documents import _json_bytes, _decode_json
from .gold_contract import decode_gold_unit_document
from .gold_governance import _document_value, _addition, _edge
from .gold_governance_models import GoldOperationKind, GoldPlacementIntent
from .gold_models import GoldSourceArtifactReference
from .gold_store import GoldSnapshotReference
from .gold_application import UnitEffect, DocumentEffect, IntegrationEffects
from .gold_history_models import (
    GoldHistoryError, GoldChangeEvent, GoldEventType, GoldIntegrationRecord,
    GoldOperationAudit, GoldCreatedPlacement, GoldRoundAudit, check,
)
from .selection_models import SemanticCallMetadata


def keys(value, kind):
    names = {f.name for f in fields(kind)} if isinstance(kind, type) else set(kind.split())
    check(type(value) is dict and set(value) == names, "HISTORY_FIELDS")
    return value


def array(value):
    check(type(value) is list, "HISTORY_ARRAY")
    return value


def document(record):
    return _document_value(asdict(record))


def encode_history(value):
    return _json_bytes(value)


def _doc_effect(value):
    value = keys(value, DocumentEffect)
    from .gold_models import _identity
    from .gold_history_models import digest
    _identity(value["document_id"], "gdoc")
    for prefix in ("previous", "proposed"):
        version = value[prefix + "_version"]
        check(version is None or type(version) is int and version > 0, "DOCUMENT_VERSION")
    for name in ("before_sha256", "after_sha256"):
        if value[name] is not None:
            digest(value[name])
    check((value["previous_version"] is None) == (value["before_sha256"] is None)
          and (value["proposed_version"] is None) == (value["after_sha256"] is None), "DOCUMENT_EFFECT")
    return DocumentEffect(**value)


def _placement(value):
    keys(value, GoldCreatedPlacement)
    return GoldCreatedPlacement(value["knowledge_id"], GoldPlacementIntent(**keys(value["placement"], GoldPlacementIntent)))


def _audit(value):
    value = dict(keys(value, GoldOperationAudit))
    value["kind"] = GoldOperationKind(value["kind"])
    for name in ("source_item_ids", "affected_knowledge_ids", "merged_ids"):
        value[name] = tuple(array(value[name]))
    value["new_placements"] = tuple(_placement(v) for v in array(value["new_placements"]))
    value["support"] = tuple(_addition(v) for v in array(value["support"]))
    value["relations"] = tuple(_edge(v) for v in array(value["relations"]))
    return GoldOperationAudit(**value)


def _effects(value):
    keys(value, IntegrationEffects)
    units = []
    for u in array(value["units"]):
        keys(u, UnitEffect)
        units.append(UnitEffect(u["knowledge_id"], None if u["before"] is None else decode_gold_unit_document(u["before"]),
                                decode_gold_unit_document(u["after"])))
    return IntegrationEffects(tuple(units), tuple(_doc_effect(v) for v in array(value["documents"])),
        tuple(_addition(v) for v in array(value["provenance_additions"])),
        tuple(_edge(v) for v in array(value["relation_additions"])),
        tuple((v[0], v[1], tuple(array(v[2]))) for v in array(value["merges"])),
        tuple((v[0], tuple(array(v[1])), v[2]) for v in array(value["conflicts"])),
        tuple((v[0], tuple(array(v[1]))) for v in array(value["source_item_accounting"])))


def _event(value):
    value = dict(keys(value, GoldChangeEvent))
    value["event_type"] = GoldEventType(value["event_type"])
    value["source_reference"] = GoldSourceArtifactReference(**keys(value["source_reference"], GoldSourceArtifactReference))
    for name in ("source_item_ids", "operation_ids", "affected_knowledge_ids", "unit_change_ids", "merged_ids", "rationales"):
        value[name] = tuple(array(value[name]))
    value["document_changes"] = tuple(_doc_effect(v) for v in array(value["document_changes"]))
    return GoldChangeEvent(**value)


def decode_integration_record(value):
    try:
        original = value
        value = dict(keys(value, GoldIntegrationRecord))
        value["source_reference"] = GoldSourceArtifactReference(**keys(value["source_reference"], GoldSourceArtifactReference))
        for name in ("base_reference", "result_reference"):
            value[name] = GoldSnapshotReference(**keys(value[name], GoldSnapshotReference))
        value["source_item_ids"] = tuple(array(value["source_item_ids"]))
        value["operations"] = tuple(_audit(v) for v in array(value["operations"]))
        rounds = []
        for r in array(value["rounds"]):
            r = dict(keys(r, GoldRoundAudit))
            for name in ("agent", "validator"):
                r[name] = SemanticCallMetadata(**keys(r[name], SemanticCallMetadata))
            rounds.append(GoldRoundAudit(**r))
        value["rounds"] = tuple(rounds)
        value["context"] = tuple(tuple(array(v)) for v in array(value["context"]))
        value["effects"] = _effects(value["effects"])
        value["events"] = tuple(_event(v) for v in array(value["events"]))
        result = GoldIntegrationRecord(**value)
        # Reject extra tuple members/changed collection shapes, not just known fields.
        check(document(result) == original, "HISTORY_CANONICAL_VALUE")
        return result
    except GoldHistoryError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise GoldHistoryError("HISTORY_RECORD") from None


def decode_history_bytes(content):
    try:
        return _decode_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GoldHistoryError("HISTORY_JSON") from None
