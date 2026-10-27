"""Deterministic Gold pairs and pure structural pruning; no semantic writer."""

from dataclasses import replace
from hashlib import sha256
import json
import re

from ._deepseek_json import strict_json
from .gold_contract import (
    decode_gold_unit_document, gold_unit_document, resolve_knowledge_id, validate_gold_state,
)
from .gold_models import GoldContractState, GoldDocumentVersion, GoldStatus, GoldVersionError
from .gold_document_models import (
    DOCUMENT_SCHEMA_VERSION, MARKER_FORMAT_VERSION, GoldDocument, GoldDocumentError,
    GoldDocumentLayout, GoldEntity, GoldSnapshot, GoldTopic, GoldUnitPlacement,
)


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise GoldDocumentError("FIELDS")
    return value


def _array(value):
    if type(value) is not list:
        raise GoldDocumentError("ARRAY")
    return value


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _decode_json(content):
    if type(content) is not bytes:
        raise GoldDocumentError("JSON_BYTES")
    try:
        return strict_json(content.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise GoldDocumentError("JSON") from None


def _literal(text):
    """Escape literal prose reversibly, including marker-like content and CR/LF."""
    lines = []
    special = set("&<>\\`*_{}[]()#+-!|~.=:")
    for line in text.split("\n"):
        first = len(line) - len(line.lstrip(" "))
        last = len(line.rstrip(" "))
        lines.append("".join(
            f"&#{ord(c)};" if c in special or ord(c) < 32 or ord(c) == 127
            or c == " " and (n < first or n >= last) else c
            for n, c in enumerate(line)))
    return "\n".join(lines)


def _unliteral(text):
    try:
        value = re.sub(r"&#([0-9]+);", lambda m: chr(int(m[1])), text)
    except (ValueError, OverflowError):
        raise GoldDocumentError("LITERAL_TEXT") from None
    if _literal(value) != text:
        raise GoldDocumentError("NONCANONICAL_TEXT")
    return value


def validate_gold_snapshot(snapshot: GoldSnapshot) -> GoldContractState:
    if type(snapshot) is not GoldSnapshot:
        raise GoldDocumentError("SNAPSHOT")
    snapshot = replace(snapshot)
    state = validate_gold_state(GoldContractState(1, 1,
        tuple(u for d in snapshot.documents for u in d.units + d.historical_units)))
    owners = {u.knowledge_id: d.document_id for d in snapshot.documents for u in d.units}
    for document in snapshot.documents:
        # Reconstruct to check local structure as well as the complete identity closure.
        replace(document)
        for historical in document.historical_units:
            if owners[resolve_knowledge_id(state, historical.knowledge_id)] != document.document_id:
                raise GoldDocumentError("HISTORICAL_OWNER")
    return state


def normalize_gold_structure(state, proposed_layout) -> GoldSnapshot:
    """Prune containers only; never create/drop a unit or decide a merge survivor."""
    validate_gold_state(state)
    if type(proposed_layout) is not tuple or any(type(d) is not GoldDocumentLayout for d in proposed_layout):
        raise GoldDocumentError("LAYOUT")
    if len({d.document_id for d in proposed_layout}) != len(proposed_layout):
        raise GoldDocumentError("DUPLICATE_DOCUMENT")
    units = {u.knowledge_id: u for u in state.units}
    seen, documents = set(), []
    for layout in proposed_layout:
        replace(layout)
        topics, active = [], []
        for topic in layout.topics:
            retained = []
            for placement in topic.units:
                identity = placement.knowledge_id
                if identity in seen or identity not in units:
                    raise GoldDocumentError("LAYOUT_REFERENCE")
                seen.add(identity)
                if units[identity].status is GoldStatus.ACTIVE:
                    retained.append(placement)
                    active.append(units[identity])
            if retained:
                topics.append(replace(topic, units=tuple(retained)))
        if topics:
            documents.append(GoldDocument(layout.version, layout.title, layout.entity,
                layout.domain_key, tuple(topics), tuple(active)))
    if {u.knowledge_id for u in state.units if u.status is GoldStatus.ACTIVE} - seen:
        raise GoldDocumentError("UNPLACED_ACTIVE")
    owners = {u.knowledge_id: d.document_id for d in documents for u in d.units}
    histories = {d.document_id: [] for d in documents}
    for unit in state.units:
        if unit.status is GoldStatus.MERGED:
            histories[owners[resolve_knowledge_id(state, unit.knowledge_id)]].append(unit)
    snapshot = GoldSnapshot(tuple(replace(d, historical_units=tuple(histories[d.document_id])) for d in documents))
    validate_gold_snapshot(snapshot)
    return snapshot


def render_gold_document(document: GoldDocument) -> bytes:
    if type(document) is not GoldDocument:
        raise GoldDocumentError("DOCUMENT")
    replace(document)
    units = {u.knowledge_id: u for u in document.units}
    lines = [f"<!-- gold-document:v1 id={document.document_id} version={document.document_version} -->",
             "# " + _literal(document.title), ""]
    for topic in document.topics:
        lines.extend([f"<!-- gold-topic:v1 id={topic.topic_id} -->", "## " + _literal(topic.title), ""])
        for placement in topic.units:
            lines.extend([f"<!-- knowledge:{placement.knowledge_id} -->",
                          _literal(units[placement.knowledge_id].content), "<!-- /knowledge -->", ""])
        lines.extend(["<!-- /gold-topic -->", ""])
    lines.append("<!-- /gold-document -->")
    return ("\n".join(lines) + "\n").encode("utf-8")


def gold_document_metadata(document: GoldDocument) -> dict:
    render_gold_document(document)
    units = []
    for unit in document.units:
        value = gold_unit_document(unit)
        value["content_sha256"] = sha256(value.pop("content").encode("utf-8")).hexdigest()
        units.append(value)
    return {
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "gold_schema_version": 1, "gold_contract_version": 1,
        "marker_format_version": MARKER_FORMAT_VERSION,
        "document_id": document.document_id, "document_version": document.document_version,
        "title": document.title,
        "entity": {"namespace": document.entity.namespace, "entity_key": document.entity.entity_key,
                   "display_name": document.entity.display_name},
        "domain_key": document.domain_key,
        "topics": [{"topic_id": t.topic_id, "title": t.title, "position": t.position,
                    "units": [{"knowledge_id": p.knowledge_id, "position": p.position} for p in t.units]}
                   for t in document.topics],
        "units": units,
        "historical_units": [gold_unit_document(u) for u in document.historical_units],
    }


def encode_gold_document_metadata(document: GoldDocument) -> bytes:
    return _json_bytes(gold_document_metadata(document))


def decode_gold_document_pair(markdown: bytes, metadata: bytes) -> GoldDocument:
    """Check a local pair; validate_gold_snapshot supplies global reference closure."""
    value = _keys(_decode_json(metadata), "document_schema_version gold_schema_version gold_contract_version "
        "marker_format_version document_id document_version title entity domain_key topics units historical_units")
    for key in ("document_schema_version", "gold_schema_version", "gold_contract_version", "marker_format_version"):
        if type(value[key]) is not int or value[key] != 1:
            raise GoldVersionError("DOCUMENT_VERSION")
    if type(markdown) is not bytes:
        raise GoldDocumentError("MARKDOWN_BYTES")
    try:
        text = markdown.decode("utf-8")
    except UnicodeError:
        raise GoldDocumentError("MARKDOWN_ENCODING") from None
    # The complete canonical re-render below checks every wrapper, heading and ordering.
    matches = re.findall(r"<!-- knowledge:(gku-[0-9a-f]{32}) -->\n(.*?)\n<!-- /knowledge -->", text, flags=re.S)
    if len({identity for identity, _ in matches}) != len(matches):
        raise GoldDocumentError("DUPLICATE_MARKER")
    content = {identity: _unliteral(body) for identity, body in matches}
    units = []
    for entry in _array(value["units"]):
        _keys(entry, "knowledge_id content_sha256 provenance status relations merged_into")
        identity = entry["knowledge_id"]
        if type(identity) is not str or identity not in content:
            raise GoldDocumentError("MISSING_MARKER")
        if entry["content_sha256"] != sha256(content[identity].encode("utf-8")).hexdigest():
            raise GoldDocumentError("CONTENT_HASH")
        unit = {k: v for k, v in entry.items() if k != "content_sha256"}
        units.append(decode_gold_unit_document({**unit, "content": content[identity]}))
    topics = []
    for topic in _array(value["topics"]):
        _keys(topic, "topic_id title position units")
        placements = tuple(GoldUnitPlacement(**_keys(p, "knowledge_id position")) for p in _array(topic["units"]))
        topics.append(GoldTopic(topic["topic_id"], topic["title"], topic["position"], placements))
    document = GoldDocument(GoldDocumentVersion(value["document_id"], value["document_version"]), value["title"],
        GoldEntity(**_keys(value["entity"], "namespace entity_key display_name")), value["domain_key"], tuple(topics),
        tuple(units), tuple(decode_gold_unit_document(u) for u in _array(value["historical_units"])))
    if set(content) != {u.knowledge_id for u in document.units} or render_gold_document(document) != markdown:
        raise GoldDocumentError("PAIR_MISMATCH")
    return document


def validate_document_transition(previous: GoldSnapshot, proposed: GoldSnapshot) -> None:
    """Version/identity safety only; semantic approval remains the future Writer's job."""
    old = validate_gold_snapshot(previous)
    new = validate_gold_snapshot(proposed)
    if {u.knowledge_id for u in old.units} - {u.knowledge_id for u in new.units}:
        raise GoldDocumentError("KNOWLEDGE_REMOVAL")
    before = {d.document_id: d for d in previous.documents}
    for document in proposed.documents:
        prior = before.get(document.document_id)
        if prior is None:
            continue
        a, b = gold_document_metadata(prior), gold_document_metadata(document)
        a.pop("document_version")
        b.pop("document_version")
        changed = a != b
        if (changed and document.document_version <= prior.document_version
                or not changed and document.document_version != prior.document_version):
            raise GoldDocumentError("VERSION_TRANSITION")
