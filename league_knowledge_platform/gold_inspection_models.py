"""Immutable inspection projections; no new canonical schema or durable state."""

from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar

from .gold_document_models import GoldDocument, GoldEntity, GoldTopic, _key, _title
from .gold_models import (
    GoldContractError, GoldKnowledgeUnit, GoldProvenance, GoldStatus,
    _identity, _positive, _text,
)
from .gold_store import GoldSnapshotReference


class GoldInspectionError(GoldContractError):
    """Sanitized inspection contract error without knowledge or request text."""


class GoldInspectionRequestError(GoldInspectionError):
    """Malformed lookup, filter or pagination request."""


class GoldInspectionNotFoundError(GoldInspectionError):
    """Well-formed identity absent from the pinned snapshot."""


def _check(value, kind):
    if type(value) is not kind:
        raise GoldInspectionError("RESULT_TYPE")
    replace(value)


def _tuple(value, kind):
    if type(value) is not tuple:
        raise GoldInspectionError("RESULT_COLLECTION")
    for member in value:
        _check(member, kind)


def _ids(value):
    if type(value) is not tuple:
        raise GoldInspectionError("RESULT_IDENTITIES")
    for identity in value:
        _identity(identity)
    if len(set(value)) != len(value):
        raise GoldInspectionError("RESULT_IDENTITIES")


def _document_context(identity, version, title, entity, domain):
    _identity(identity, "gdoc")
    _positive(version)
    _title(title)
    _check(entity, GoldEntity)
    _key(domain)


@dataclass(frozen=True, slots=True)
class GoldTopicContext:
    topic_id: str
    title: str
    position: int
    unit_position: int

    def __post_init__(self):
        _identity(self.topic_id, "topic")
        _title(self.title)
        if any(type(v) is not int or v < 0 for v in (self.position, self.unit_position)):
            raise GoldInspectionError("RESULT_POSITION")


@dataclass(frozen=True, slots=True)
class GoldUnitView:
    unit: GoldKnowledgeUnit = field(repr=False)
    document_id: str
    document_version: int
    document_title: str
    entity: GoldEntity
    domain_key: str
    topic: GoldTopicContext | None

    def __post_init__(self):
        _check(self.unit, GoldKnowledgeUnit)
        _document_context(self.document_id, self.document_version, self.document_title, self.entity, self.domain_key)
        if self.unit.status is GoldStatus.ACTIVE:
            _check(self.topic, GoldTopicContext)
        elif self.topic is not None:
            raise GoldInspectionError("HISTORICAL_PLACEMENT")


@dataclass(frozen=True, slots=True)
class GoldTopicView:
    document_id: str
    document_version: int
    document_title: str
    entity: GoldEntity
    domain_key: str
    topic: GoldTopic
    units: tuple[GoldKnowledgeUnit, ...] = field(repr=False)

    def __post_init__(self):
        _document_context(self.document_id, self.document_version, self.document_title, self.entity, self.domain_key)
        _check(self.topic, GoldTopic)
        _tuple(self.units, GoldKnowledgeUnit)
        if (tuple(u.knowledge_id for u in self.units) != tuple(p.knowledge_id for p in self.topic.units)
                or any(u.status is not GoldStatus.ACTIVE for u in self.units)):
            raise GoldInspectionError("TOPIC_MEMBERSHIP")


@dataclass(frozen=True, slots=True)
class GoldProvenanceView:
    knowledge_id: str
    provenance: tuple[GoldProvenance, ...] = field(repr=False)

    def __post_init__(self):
        _identity(self.knowledge_id)
        _tuple(self.provenance, GoldProvenance)
        if not self.provenance or len(set(self.provenance)) != len(self.provenance):
            raise GoldInspectionError("RESULT_PROVENANCE")


@dataclass(frozen=True, slots=True)
class GoldTopicStructure:
    topic_id: str
    title: str
    position: int
    knowledge_ids: tuple[str, ...]

    def __post_init__(self):
        _identity(self.topic_id, "topic")
        _title(self.title)
        if type(self.position) is not int or self.position < 0:
            raise GoldInspectionError("RESULT_POSITION")
        _ids(self.knowledge_ids)


@dataclass(frozen=True, slots=True)
class GoldDocumentStructure:
    document_id: str
    document_version: int
    title: str
    domain_key: str
    topics: tuple[GoldTopicStructure, ...]
    historical_knowledge_ids: tuple[str, ...]

    def __post_init__(self):
        _identity(self.document_id, "gdoc")
        _positive(self.document_version)
        _title(self.title)
        _key(self.domain_key)
        _tuple(self.topics, GoldTopicStructure)
        _ids(self.historical_knowledge_ids)


@dataclass(frozen=True, slots=True)
class GoldEntityStructure:
    entity: GoldEntity
    documents: tuple[GoldDocumentStructure, ...]

    def __post_init__(self):
        _check(self.entity, GoldEntity)
        _tuple(self.documents, GoldDocumentStructure)


@dataclass(frozen=True, slots=True)
class GoldStructureView:
    entities: tuple[GoldEntityStructure, ...]

    def __post_init__(self):
        _tuple(self.entities, GoldEntityStructure)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class GoldInspectionResult(Generic[T]):
    snapshot_reference: GoldSnapshotReference
    value: T = field(repr=False)

    def __post_init__(self):
        _check(self.snapshot_reference, GoldSnapshotReference)
        kinds = (GoldUnitView, GoldDocument, GoldTopicView, GoldProvenanceView, GoldStructureView)
        if type(self.value) not in kinds:
            raise GoldInspectionError("RESULT_VALUE")
        _check(self.value, type(self.value))


def _tokens(value):
    if type(value) is not tuple or not value:
        raise GoldInspectionError("MATCH_TOKENS")
    for token in value:
        _text(token)
    if tuple(sorted(set(value))) != value:
        raise GoldInspectionError("MATCH_TOKENS")


@dataclass(frozen=True, slots=True)
class GoldSearchMatch:
    unit: GoldUnitView
    matched_tokens: tuple[str, ...]
    matched_fields: tuple[str, ...]

    def __post_init__(self):
        _check(self.unit, GoldUnitView)
        _tokens(self.matched_tokens)
        _tokens(self.matched_fields)

    @property
    def matched_token_count(self):
        return len(self.matched_tokens)


@dataclass(frozen=True, slots=True)
class GoldSearchPage:
    snapshot_reference: GoldSnapshotReference
    query_tokens: tuple[str, ...]
    matches: tuple[GoldSearchMatch, ...]
    total_matches: int
    limit: int
    offset: int
    has_more: bool

    def __post_init__(self):
        _check(self.snapshot_reference, GoldSnapshotReference)
        if self.query_tokens:
            _tokens(self.query_tokens)
        elif self.query_tokens != ():
            raise GoldInspectionError("MATCH_TOKENS")
        _tuple(self.matches, GoldSearchMatch)
        if (type(self.total_matches) is not int or self.total_matches < 0
                or not self.query_tokens and self.total_matches != 0
                or type(self.limit) is not int or self.limit < 1
                or type(self.offset) is not int or self.offset < 0
                or len(self.matches) != min(self.limit, max(0, self.total_matches - self.offset))
                or type(self.has_more) is not bool
                or self.has_more != (self.offset + len(self.matches) < self.total_matches)):
            raise GoldInspectionError("SEARCH_PAGE")
        identities = tuple(m.unit.unit.knowledge_id for m in self.matches)
        if (len(set(identities)) != len(identities)
                or any(not set(m.matched_tokens) <= set(self.query_tokens) for m in self.matches)):
            raise GoldInspectionError("SEARCH_MATCHES")
