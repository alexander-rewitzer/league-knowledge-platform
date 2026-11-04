"""Read-only pinned Gold inspection and lexical candidate search, not integration."""

from dataclasses import dataclass, field
import re
import unicodedata

from . import defaults
from .gold_document_models import GoldConfig, GoldDocument, _key
from .gold_models import GoldStatus, _identity
from .gold_store import LoadedGoldSnapshot, GoldSnapshotReference, load_gold_snapshot
from .gold_inspection_models import (
    GoldDocumentStructure, GoldEntityStructure, GoldInspectionNotFoundError,
    GoldInspectionRequestError, GoldInspectionResult, GoldProvenanceView,
    GoldSearchMatch, GoldSearchPage, GoldStructureView, GoldTopicContext,
    GoldTopicStructure, GoldTopicView, GoldUnitView,
)

_TOKEN_PATTERN = re.compile(r"(?:gku|gdoc|topic)-[0-9a-f]{32}\b|[^\W_]+")


def _tokenize(text):
    return frozenset(_TOKEN_PATTERN.findall(unicodedata.normalize("NFC", text).casefold()))


def _request_identity(value, prefix):
    try:
        _identity(value, prefix)
    except (ValueError, TypeError):
        raise GoldInspectionRequestError("IDENTITY") from None


def _filters(entity_namespace, entity_key, domain_key, document_id, topic_id=None, status=None):
    for value in (entity_namespace, entity_key, domain_key):
        if value is not None:
            try:
                _key(value)
            except (ValueError, TypeError):
                raise GoldInspectionRequestError("FILTER") from None
    if document_id is not None:
        _request_identity(document_id, "gdoc")
    if topic_id is not None:
        _request_identity(topic_id, "topic")
    if status is not None and type(status) is not GoldStatus:
        raise GoldInspectionRequestError("FILTER_STATUS")


def _document_matches(document, namespace, key, domain, identity):
    return ((namespace is None or document.entity.namespace == namespace)
            and (key is None or document.entity.entity_key == key)
            and (domain is None or document.domain_key == domain)
            and (identity is None or document.document_id == identity))


def _unit_views(snapshot):
    rows = []
    for document in snapshot.documents:
        units = {u.knowledge_id: u for u in document.units}
        context = (document.document_id, document.document_version, document.title,
                   document.entity, document.domain_key)
        for topic in document.topics:
            for placement in topic.units:
                rows.append(GoldUnitView(units[placement.knowledge_id], *context,
                    GoldTopicContext(topic.topic_id, topic.title, topic.position, placement.position)))
        for unit in document.historical_units:
            rows.append(GoldUnitView(unit, *context, None))
    return tuple(sorted(rows, key=lambda row: row.unit.knowledge_id))


def _search_fields(row):
    unit = row.unit
    return {
        "content": unit.content,
        "knowledge_id": unit.knowledge_id,
        "entity": " ".join((row.entity.namespace, row.entity.entity_key, row.entity.display_name)),
        "document": " ".join((row.document_id, row.document_title, row.domain_key)),
        "topic": "" if row.topic is None else row.topic.topic_id + " " + row.topic.title,
        "status": unit.status.value,
        "relations": " ".join(r.kind.value + " " + r.target_knowledge_id for r in unit.relations),
        "provenance": " ".join(" ".join((p.source_knowledge_artifact.source_type,
            p.source_knowledge_artifact.source_id, p.source_item_id)) for p in unit.provenance),
    }


@dataclass(frozen=True, slots=True, init=False)
class GoldInspection:
    """One validated, immutable selection; methods never refresh or write files.

    Construction always uses the canonical loader. Future callers bind plans to
    reference and own stale-base checks. This is not a filesystem monitoring API.
    """

    _loaded: LoadedGoldSnapshot = field(repr=False)
    _rows: tuple[GoldUnitView, ...] = field(repr=False)

    def __init__(self, config=GoldConfig()):
        loaded = load_gold_snapshot(config)
        object.__setattr__(self, "_loaded", loaded)
        object.__setattr__(self, "_rows", _unit_views(loaded.snapshot))

    @property
    def reference(self) -> GoldSnapshotReference:
        return self._loaded.reference

    def get_gold_unit(self, knowledge_id: str) -> GoldInspectionResult[GoldUnitView]:
        _request_identity(knowledge_id, "gku")
        for row in self._rows:
            if row.unit.knowledge_id == knowledge_id:
                return GoldInspectionResult(self.reference, row)
        raise GoldInspectionNotFoundError("UNIT_NOT_FOUND")

    def get_gold_document(self, document_id: str) -> GoldInspectionResult[GoldDocument]:
        _request_identity(document_id, "gdoc")
        for document in self._loaded.snapshot.documents:
            if document.document_id == document_id:
                return GoldInspectionResult(self.reference, document)
        raise GoldInspectionNotFoundError("DOCUMENT_NOT_FOUND")

    def get_gold_topic(self, document_id: str, topic_id: str) -> GoldInspectionResult[GoldTopicView]:
        _request_identity(document_id, "gdoc")
        _request_identity(topic_id, "topic")
        document = self.get_gold_document(document_id).value
        for topic in document.topics:
            if topic.topic_id == topic_id:
                units = {u.knowledge_id: u for u in document.units}
                view = GoldTopicView(document.document_id, document.document_version, document.title,
                    document.entity, document.domain_key, topic,
                    tuple(units[p.knowledge_id] for p in topic.units))
                return GoldInspectionResult(self.reference, view)
        raise GoldInspectionNotFoundError("TOPIC_NOT_FOUND")

    def get_gold_provenance(self, knowledge_id: str) -> GoldInspectionResult[GoldProvenanceView]:
        unit = self.get_gold_unit(knowledge_id).value.unit
        return GoldInspectionResult(self.reference, GoldProvenanceView(unit.knowledge_id, unit.provenance))

    def inspect_gold_structure(self, *, entity_namespace=None, entity_key=None, domain_key=None,
                               document_id=None) -> GoldInspectionResult[GoldStructureView]:
        _filters(entity_namespace, entity_key, domain_key, document_id)
        groups = {}
        for document in self._loaded.snapshot.documents:
            if not _document_matches(document, entity_namespace, entity_key, domain_key, document_id):
                continue
            topics = tuple(GoldTopicStructure(t.topic_id, t.title, t.position,
                tuple(p.knowledge_id for p in t.units)) for t in document.topics)
            view = GoldDocumentStructure(document.document_id, document.document_version, document.title,
                document.domain_key, topics, tuple(u.knowledge_id for u in document.historical_units))
            # Keep differing stored display labels rather than silently choosing
            # one label for all documents with the same namespace/key.
            entity_id = (document.entity.namespace, document.entity.entity_key, document.entity.display_name)
            if entity_id not in groups:
                groups[entity_id] = (document.entity, [])
            groups[entity_id][1].append(view)
        entities = tuple(GoldEntityStructure(entity, tuple(documents))
                         for _, (entity, documents) in sorted(groups.items()))
        return GoldInspectionResult(self.reference, GoldStructureView(entities))

    def search_gold(self, query: str, *, limit=defaults.GOLD_INSPECTION_SEARCH_LIMIT, offset=0,
                    entity_namespace=None, entity_key=None, domain_key=None, document_id=None,
                    topic_id=None, status=None) -> GoldSearchPage:
        _filters(entity_namespace, entity_key, domain_key, document_id, topic_id, status)
        if type(limit) is not int or not 1 <= limit <= defaults.GOLD_INSPECTION_MAX_PAGE_LIMIT:
            raise GoldInspectionRequestError("LIMIT")
        if type(offset) is not int or offset < 0:
            raise GoldInspectionRequestError("OFFSET")
        if not isinstance(query, str):
            raise GoldInspectionRequestError("QUERY")
        try:
            query.encode("utf-8")
        except UnicodeError:
            raise GoldInspectionRequestError("QUERY") from None
        tokens = _tokenize(query)
        matches = []
        if tokens:
            for row in self._rows:
                if (not _document_matches(row, entity_namespace, entity_key, domain_key, document_id)
                        or topic_id is not None and (row.topic is None or row.topic.topic_id != topic_id)
                        or status is not None and row.unit.status is not status):
                    continue
                found, matched_fields = set(), []
                for name, text in _search_fields(row).items():
                    overlap = tokens & _tokenize(text)
                    if overlap:
                        found.update(overlap)
                        matched_fields.append(name)
                if found:
                    matches.append(GoldSearchMatch(row, tuple(sorted(found)), tuple(sorted(matched_fields))))
        matches.sort(key=lambda match: (-match.matched_token_count, match.unit.unit.knowledge_id))
        page = tuple(matches[offset:offset + limit])
        return GoldSearchPage(self.reference, tuple(sorted(tokens)), page, len(matches), limit, offset,
                              offset + len(page) < len(matches))


def open_gold_inspection(config=GoldConfig()) -> GoldInspection:
    """Open one canonical selection; missing stores fail instead of being initialized."""
    return GoldInspection(config)
