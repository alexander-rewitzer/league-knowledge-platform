"""Bounded Gold document structure; semantic units remain owned by gold_models."""

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
from uuid import uuid4

from . import defaults
from .gold_models import (
    GoldContractError, GoldDocumentVersion, GoldKnowledgeUnit, GoldStatus,
    _identity, _text,
)

DOCUMENT_SCHEMA_VERSION = 1
MARKER_FORMAT_VERSION = 1


class GoldDocumentError(GoldContractError):
    """Invalid document structure or inconsistent canonical pair."""


class GoldStorageError(GoldDocumentError):
    """Local storage is invalid or publication failed."""


class GoldStaleBaseError(GoldStorageError):
    """The current snapshot differs from the explicit publication base."""


class GoldBusyError(GoldStorageError):
    """Another local publisher owns the execution guard."""


def _title(value):
    _text(value)
    if any(ord(c) < 32 or ord(c) == 127 or c in "\u2028\u2029" for c in value):
        raise GoldDocumentError("TITLE")


def _key(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value) is None:
        raise GoldDocumentError("ROUTING_KEY")
    return value


def _members(value, kind):
    if type(value) is not tuple or any(type(v) is not kind for v in value):
        raise GoldDocumentError("COLLECTION")
    for member in value:
        replace(member)  # Revalidate nested frozen values at public boundaries.


def _ordered(value, kind, identity):
    _members(value, kind)
    if len({getattr(v, identity) for v in value}) != len(value):
        raise GoldDocumentError("DUPLICATE_LAYOUT_ID")
    if len({v.position for v in value}) != len(value):
        raise GoldDocumentError("DUPLICATE_POSITION")
    return tuple(sorted(value, key=lambda v: v.position))


def _position(value):
    if type(value) is not int or value < 0:
        raise GoldDocumentError("POSITION")


def new_topic_id() -> str:
    return "topic-" + uuid4().hex


@dataclass(frozen=True, slots=True)
class GoldEntity:
    namespace: str
    entity_key: str
    display_name: str

    def __post_init__(self):
        _key(self.namespace)
        _key(self.entity_key)
        _title(self.display_name)


@dataclass(frozen=True, slots=True)
class GoldUnitPlacement:
    knowledge_id: str
    position: int

    def __post_init__(self):
        _identity(self.knowledge_id)
        _position(self.position)


@dataclass(frozen=True, slots=True)
class GoldTopic:
    """May be empty in a proposed layout, never in a canonical document."""

    topic_id: str
    title: str
    position: int
    units: tuple[GoldUnitPlacement, ...] = ()

    def __post_init__(self):
        _identity(self.topic_id, "topic")
        _title(self.title)
        _position(self.position)
        object.__setattr__(self, "units", _ordered(self.units, GoldUnitPlacement, "knowledge_id"))


@dataclass(frozen=True, slots=True)
class GoldDocumentLayout:
    """Caller-proposed organization, before canonical empty-container pruning."""

    version: GoldDocumentVersion
    title: str
    entity: GoldEntity
    domain_key: str
    topics: tuple[GoldTopic, ...] = ()

    def __post_init__(self):
        if type(self.version) is not GoldDocumentVersion or type(self.entity) is not GoldEntity:
            raise GoldDocumentError("DOCUMENT_CONTEXT")
        self.version._validate()
        replace(self.entity)
        _title(self.title)
        _key(self.domain_key)
        object.__setattr__(self, "topics", _ordered(self.topics, GoldTopic, "topic_id"))
        ids = [p.knowledge_id for t in self.topics for p in t.units]
        if len(ids) != len(set(ids)):
            raise GoldDocumentError("DUPLICATE_PLACEMENT")

    @property
    def document_id(self):
        return self.version.document_id

    @property
    def document_version(self):
        return self.version.document_version


@dataclass(frozen=True, slots=True)
class GoldDocument(GoldDocumentLayout):
    units: tuple[GoldKnowledgeUnit, ...] = field(default=(), repr=False)
    historical_units: tuple[GoldKnowledgeUnit, ...] = field(default=(), repr=False)

    def __post_init__(self):
        GoldDocumentLayout.__post_init__(self)
        _members(self.units, GoldKnowledgeUnit)
        _members(self.historical_units, GoldKnowledgeUnit)
        if not self.topics or any(not t.units for t in self.topics):
            raise GoldDocumentError("EMPTY_CONTAINER")
        placed = [p.knowledge_id for t in self.topics for p in t.units]
        if (len(placed) != len(self.units) or set(placed) != {u.knowledge_id for u in self.units}
                or any(u.status is not GoldStatus.ACTIVE for u in self.units)
                or any(u.status is not GoldStatus.MERGED for u in self.historical_units)):
            raise GoldDocumentError("UNIT_PLACEMENT")
        ids = [u.knowledge_id for u in self.units + self.historical_units]
        if len(ids) != len(set(ids)):
            raise GoldDocumentError("DUPLICATE_IDENTITY")
        for name in ("units", "historical_units"):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name), key=lambda u: u.knowledge_id)))


@dataclass(frozen=True, slots=True)
class GoldSnapshot:
    documents: tuple[GoldDocument, ...] = ()

    def __post_init__(self):
        _members(self.documents, GoldDocument)
        if len({d.document_id for d in self.documents}) != len(self.documents):
            raise GoldDocumentError("DUPLICATE_DOCUMENT")
        object.__setattr__(self, "documents", tuple(sorted(self.documents, key=lambda d: d.document_id)))


@dataclass(frozen=True, slots=True)
class GoldConfig:
    root: Path = Path(defaults.GOLD_ROOT)

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).absolute())
