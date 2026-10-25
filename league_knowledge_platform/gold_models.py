"""Immutable Gold contract values, independent of document placement and retrieval."""

from dataclasses import dataclass, field
from enum import Enum
import re
from uuid import uuid4

from .models import _require_video_id

GOLD_SCHEMA_VERSION = 1
GOLD_CONTRACT_VERSION = 1


class GoldContractError(ValueError):
    """Invalid structural data; diagnostics never echo knowledge content."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"Invalid Gold contract ({code})")


class GoldVersionError(GoldContractError):
    """Unsupported Gold serialization or validation contract."""


class GoldReferenceError(GoldContractError):
    """A stable identity cannot be resolved in the supplied complete context."""


def _identity(value, prefix="gku"):
    if not isinstance(value, str) or re.fullmatch(prefix + r"-[0-9a-f]{32}", value) is None:
        raise GoldContractError("IDENTITY")


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise GoldContractError("TEXT")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise GoldContractError("TEXT_ENCODING") from None


def _positive(value):
    if type(value) is not int or value < 1:
        raise GoldContractError("POSITIVE_INTEGER")


def _collection(value, kind, *, nonempty=False):
    if type(value) is not tuple or nonempty and not value:
        raise GoldContractError("IMMUTABLE_COLLECTION")
    for entry in value:
        if type(entry) is not kind:
            raise GoldContractError("COLLECTION_MEMBER")
        entry._validate()
    if len(set(value)) != len(value):
        raise GoldContractError("DUPLICATE_ENTRY")


def new_knowledge_id() -> str:
    return "gku-" + uuid4().hex


def new_document_id() -> str:
    return "gdoc-" + uuid4().hex


class GoldStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MERGED = "MERGED"


class GoldRelationKind(str, Enum):
    RELATED_TO = "related_to"
    CONTRADICTS = "contradicts"


@dataclass(frozen=True, slots=True)
class GoldSourceArtifactReference:
    source_type: str
    source_id: str
    relative_path: str
    sha256: str
    schema_version: int
    audit_contract_version: int

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if not isinstance(self.source_type, str) or self.source_type != "youtube":
            raise GoldContractError("SOURCE_TYPE")
        try:
            _require_video_id(self.source_id)
        except ValueError:
            raise GoldContractError("SOURCE_IDENTITY") from None
        _text(self.relative_path)
        if (any(c in self.relative_path for c in ("\\", ":", "\0"))
                or any(p in ("", ".", "..") for p in self.relative_path.split("/"))):
            raise GoldContractError("SOURCE_PATH")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise GoldContractError("SOURCE_HASH")
        _positive(self.schema_version)
        _positive(self.audit_contract_version)

    def _sort_key(self):
        return (self.source_type, self.source_id, self.relative_path, self.sha256,
                self.schema_version, self.audit_contract_version)


@dataclass(frozen=True, slots=True)
class GoldProvenance:
    source_knowledge_artifact: GoldSourceArtifactReference
    source_item_id: str

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if type(self.source_knowledge_artifact) is not GoldSourceArtifactReference:
            raise GoldContractError("SOURCE_REFERENCE")
        self.source_knowledge_artifact._validate()
        _text(self.source_item_id)

    def _sort_key(self):
        return (*self.source_knowledge_artifact._sort_key(), self.source_item_id)


@dataclass(frozen=True, slots=True)
class GoldRelation:
    kind: GoldRelationKind
    target_knowledge_id: str

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if type(self.kind) is not GoldRelationKind:
            raise GoldContractError("RELATION_KIND")
        _identity(self.target_knowledge_id)

    def _sort_key(self):
        return self.kind.value, self.target_knowledge_id


@dataclass(frozen=True, slots=True)
class GoldKnowledgeUnit:
    knowledge_id: str
    content: str = field(repr=False)
    provenance: tuple[GoldProvenance, ...] = field(repr=False)
    status: GoldStatus
    relations: tuple[GoldRelation, ...] = ()
    merged_into: str | None = None

    def __post_init__(self):
        self._validate()
        object.__setattr__(self, "provenance", tuple(sorted(self.provenance, key=GoldProvenance._sort_key)))
        object.__setattr__(self, "relations", tuple(sorted(self.relations, key=GoldRelation._sort_key)))

    def _validate(self):
        _identity(self.knowledge_id)
        _text(self.content)
        _collection(self.provenance, GoldProvenance, nonempty=True)
        _collection(self.relations, GoldRelation)
        if type(self.status) is not GoldStatus:
            raise GoldContractError("STATUS")
        if self.status is GoldStatus.ACTIVE:
            if self.merged_into is not None:
                raise GoldContractError("ACTIVE_SURVIVOR")
        else:
            _identity(self.merged_into)
            if self.merged_into == self.knowledge_id:
                raise GoldReferenceError("SELF_MERGE")
        if any(r.target_knowledge_id == self.knowledge_id for r in self.relations):
            raise GoldReferenceError("SELF_RELATION")


@dataclass(frozen=True, slots=True)
class GoldContractState:
    """Complete in-memory identity scope, never an additional Gold file store."""

    schema_version: int
    contract_version: int
    units: tuple[GoldKnowledgeUnit, ...] = field(default=(), repr=False)

    def __post_init__(self):
        self._validate()
        object.__setattr__(self, "units", tuple(sorted(self.units, key=lambda u: u.knowledge_id)))

    def _validate(self):
        if (type(self.schema_version) is not int or self.schema_version != GOLD_SCHEMA_VERSION
                or type(self.contract_version) is not int or self.contract_version != GOLD_CONTRACT_VERSION):
            raise GoldVersionError("VERSION")
        _collection(self.units, GoldKnowledgeUnit)
        if len({u.knowledge_id for u in self.units}) != len(self.units):
            raise GoldContractError("DUPLICATE_IDENTITY")


@dataclass(frozen=True, slots=True)
class GoldDocumentVersion:
    document_id: str
    document_version: int

    def __post_init__(self):
        self._validate()

    def _validate(self):
        _identity(self.document_id, "gdoc")
        _positive(self.document_version)
