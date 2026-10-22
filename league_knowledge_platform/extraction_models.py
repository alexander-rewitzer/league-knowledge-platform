"""Immutable transient extraction contracts; no semantic acceptance or persistence."""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Protocol

from . import defaults
from .models import _require_video_id
from .segmentation_models import SectionKind
from .selection_models import SemanticCallMetadata
from .silver import _timing

EXTRACTION_CONTRACT_VERSION = 1


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text")


def _tuple(value, kind, *, nonempty=False):
    if not isinstance(value, tuple) or nonempty and not value:
        raise ValueError("Expected immutable collection")
    for entry in value:
        if not isinstance(entry, kind):
            raise ValueError("Unexpected collection member")
        entry.__post_init__()


def _section_id(value):
    if not isinstance(value, str) or re.fullmatch(r"section-[0-9]{6,}", value) is None:
        raise ValueError("Expected an upstream section identity")


class ExtractionOutcome(str, Enum):
    ITEMS_PROPOSED = "ITEMS_PROPOSED"
    NO_KNOWLEDGE_PROPOSED = "NO_KNOWLEDGE_PROPOSED"


def _outcome(outcome, items):
    if not isinstance(outcome, ExtractionOutcome) or bool(items) != (outcome is ExtractionOutcome.ITEMS_PROPOSED):
        raise ValueError("Outcome and item count disagree")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    relative_path: str
    sha256: str
    schema_version: int

    def __post_init__(self):
        if (not isinstance(self.relative_path, str)
                or re.fullmatch(r"youtube/id-[0-9a-f]+\.json", self.relative_path) is None
                or not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
                or type(self.schema_version) is not int or self.schema_version != 1):
            raise ValueError("Unsupported upstream artifact reference")


@dataclass(frozen=True, slots=True)
class ExtractionProvenance:
    video_id: str
    segmentation: ArtifactReference
    silver: ArtifactReference
    bronze: ArtifactReference
    segmentation_contract_version: int
    cleaning_rule_version: int

    def __post_init__(self):
        _require_video_id(self.video_id)
        for reference in (self.segmentation, self.silver, self.bronze):
            if not isinstance(reference, ArtifactReference):
                raise ValueError("Expected upstream provenance")
            reference.__post_init__()
            if reference.relative_path != f"youtube/id-{self.video_id.encode('ascii').hex()}.json":
                raise ValueError("Upstream source identities disagree")
        if any(type(v) is not int or v != 1 for v in
               (self.segmentation_contract_version, self.cleaning_rule_version)):
            raise ValueError("Unsupported upstream behavior version")


@dataclass(frozen=True, slots=True)
class ExtractionSegment:
    source_index: int
    text: str = field(repr=False)
    start_seconds: str
    duration_seconds: str

    def __post_init__(self):
        if type(self.source_index) is not int or self.source_index < 0 or not isinstance(self.text, str):
            raise ValueError("Expected an existing Silver segment")
        _timing(self.start_seconds)
        _timing(self.duration_seconds)


@dataclass(frozen=True, slots=True)
class ExtractionSection:
    section_id: str
    kind: SectionKind
    language_code: str
    text: str = field(repr=False)
    segments: tuple[ExtractionSegment, ...] = field(repr=False)
    provenance: ExtractionProvenance

    def __post_init__(self):
        _section_id(self.section_id)
        if self.kind is not SectionKind.CONTENT:
            raise ValueError("Extraction requires CONTENT")
        _text(self.language_code)
        _tuple(self.segments, ExtractionSegment, nonempty=True)
        indices = tuple(s.source_index for s in self.segments)
        if indices != tuple(sorted(set(indices))) or self.text != " ".join(s.text for s in self.segments if s.text):
            raise ValueError("Section text and ordered members disagree")
        if not isinstance(self.provenance, ExtractionProvenance):
            raise ValueError("Expected trusted input provenance")
        self.provenance.__post_init__()


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    first_source_index: int
    last_source_index: int

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.first_source_index, self.last_source_index)):
            raise ValueError("Expected existing source-index endpoints")


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    section_id: str
    spans: tuple[EvidenceSpan, ...]

    def __post_init__(self):
        _section_id(self.section_id)
        _tuple(self.spans, EvidenceSpan, nonempty=True)


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    item_id: str
    statement: str = field(repr=False)
    evidence: tuple[KnowledgeEvidence, ...]

    def __post_init__(self):
        if not isinstance(self.item_id, str) or re.fullmatch(r"item-[0-9]{6,}", self.item_id) is None:
            raise ValueError("Expected a proposal-local item identity")
        _text(self.statement)
        _tuple(self.evidence, KnowledgeEvidence, nonempty=True)


@dataclass(frozen=True, slots=True)
class ExtractionItemDraft:
    statement: str = field(repr=False)
    spans: tuple[EvidenceSpan, ...]

    def __post_init__(self):
        _text(self.statement)
        _tuple(self.spans, EvidenceSpan, nonempty=True)


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    language_code: str
    segments: tuple[ExtractionSegment, ...] = field(repr=False)
    output_limit: int

    def __post_init__(self):
        _text(self.language_code)
        _tuple(self.segments, ExtractionSegment, nonempty=True)
        indices = tuple(s.source_index for s in self.segments)
        if indices != tuple(sorted(set(indices))) or type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("Invalid complete extraction request")


@dataclass(frozen=True, slots=True)
class ExtractionResponse:
    outcome: ExtractionOutcome
    items: tuple[ExtractionItemDraft, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        _tuple(self.items, ExtractionItemDraft)
        _outcome(self.outcome, self.items)
        if not isinstance(self.metadata, SemanticCallMetadata):
            raise ValueError("Expected sanitized call metadata")
        self.metadata.__post_init__()


@dataclass(frozen=True, slots=True)
class ExtractionProposal:
    contract_version: int
    section_id: str
    provenance: ExtractionProvenance
    outcome: ExtractionOutcome
    items: tuple[KnowledgeItem, ...] = field(repr=False)
    metadata: SemanticCallMetadata

    def __post_init__(self):
        if type(self.contract_version) is not int or self.contract_version != EXTRACTION_CONTRACT_VERSION:
            raise ValueError("Unsupported transient extraction contract")
        _section_id(self.section_id)
        if not isinstance(self.provenance, ExtractionProvenance) or not isinstance(self.metadata, SemanticCallMetadata):
            raise ValueError("Expected application provenance and call metadata")
        self.provenance.__post_init__()
        self.metadata.__post_init__()
        _tuple(self.items, KnowledgeItem)
        _outcome(self.outcome, self.items)


class ExtractionError(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"Extraction failed ({kind})")


class ExtractionInputError(ExtractionError):
    """Missing, invalid or non-CONTENT upstream input."""


class ExtractionIntegrityError(ExtractionError):
    """Upstream source, snapshot or section membership disagrees."""


class ExtractionValidationError(ExtractionError):
    """An operation did not produce a structurally valid proposal."""


class ExtractionCapabilityError(ExtractionError):
    def __init__(self, kind: str, *, retryable: bool):
        allowed = {"TIMEOUT", "NETWORK", "RATE_LIMIT", "SERVICE", "INVALID_OUTPUT", "OUTPUT_TRUNCATED",
                   "CONTEXT_LIMIT", "AUTHENTICATION", "AUTHORIZATION", "BALANCE", "CONFIGURATION",
                   "INVALID_REQUEST", "PROVIDER_CONTRACT"}
        if kind not in allowed or type(retryable) is not bool:
            raise ValueError("Invalid sanitized provider failure")
        self.retryable = retryable
        super().__init__(kind)


class ExtractionCapability(Protocol):
    def extract(self, request: ExtractionRequest) -> ExtractionResponse: ...


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    output_limit: int = defaults.EXTRACTION_OUTPUT_LIMIT

    def __post_init__(self):
        if type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("Expected a positive output allowance")


DEFAULT_EXTRACTION_CONFIG = ExtractionConfig()
