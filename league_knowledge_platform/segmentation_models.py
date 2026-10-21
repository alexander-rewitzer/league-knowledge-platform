"""Immutable segmentation decisions and sanitized operational contracts."""

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
from typing import Protocol

from . import defaults
from .selection_models import SemanticCallMetadata


class SectionKind(str, Enum):
    CONTENT = "CONTENT"
    ADVERTISEMENT = "ADVERTISEMENT"


class SegmentationOutcome(str, Enum):
    SEGMENTED = "SEGMENTED"
    NO_SEGMENTABLE_CONTENT = "NO_SEGMENTABLE_CONTENT"


FAILURE_KINDS = frozenset((
    "TIMEOUT", "NETWORK", "RATE_LIMIT", "SERVICE", "INVALID_OUTPUT", "OUTPUT_TRUNCATED",
    "CONTEXT_LIMIT", "AUTHENTICATION", "AUTHORIZATION", "BALANCE", "CONFIGURATION",
    "INVALID_REQUEST", "PROVIDER_CONTRACT",
))
VALIDATION_CODES = frozenset((
    "SCHEMA", "UNKNOWN_SEGMENT", "INVALID_RANGE", "COVERAGE_GAP", "OVERLAP_OR_ORDER",
    "INCOMPLETE_COVERAGE", "EMPTY_PROPOSAL",
))


@dataclass(frozen=True, slots=True)
class SegmentationSegment:
    source_index: int
    text: str = field(repr=False)

    def __post_init__(self):
        if type(self.source_index) is not int or self.source_index < 0 or not isinstance(self.text, str):
            raise ValueError("Expected an existing source segment")


@dataclass(frozen=True, slots=True)
class SegmentationValidationIssue:
    code: str
    section_position: int | None = None
    expected_first_source_index: int | None = None
    received_first_source_index: int | None = None

    def __post_init__(self):
        if self.code not in VALIDATION_CODES or any(
            value is not None and (type(value) is not int or value < 0)
            for value in (self.section_position, self.expected_first_source_index, self.received_first_source_index)
        ):
            raise ValueError("Invalid structural feedback")


@dataclass(frozen=True, slots=True)
class SegmentationRequest:
    language_code: str
    segments: tuple[SegmentationSegment, ...] = field(repr=False)
    validation_feedback: tuple[SegmentationValidationIssue, ...] = ()
    output_limit: int = 8192

    def __post_init__(self):
        if (not isinstance(self.language_code, str) or not self.language_code.strip()
                or not isinstance(self.segments, tuple) or not self.segments
                or any(not isinstance(s, SegmentationSegment) for s in self.segments)
                or not isinstance(self.validation_feedback, tuple)
                or any(not isinstance(i, SegmentationValidationIssue) for i in self.validation_feedback)
                or type(self.output_limit) is not int
                or not 1 <= self.output_limit <= defaults.SEGMENTATION_MAX_OUTPUT_LIMIT):
            raise ValueError("Invalid complete segmentation request")
        indices = tuple(s.source_index for s in self.segments)
        if indices != tuple(sorted(set(indices))):
            raise ValueError("Segment identities must retain source order")


@dataclass(frozen=True, slots=True)
class SectionSpan:
    first_source_index: int
    last_source_index: int
    kind: SectionKind

    def __post_init__(self):
        if (any(type(v) is not int or v < 0 for v in (self.first_source_index, self.last_source_index))
                or not isinstance(self.kind, SectionKind)):
            raise ValueError("Invalid structural section decision")


@dataclass(frozen=True, slots=True)
class SegmentationProposal:
    sections: tuple[SectionSpan, ...]
    metadata: SemanticCallMetadata

    def __post_init__(self):
        if (not isinstance(self.sections, tuple) or any(not isinstance(s, SectionSpan) for s in self.sections)
                or not isinstance(self.metadata, SemanticCallMetadata)):
            raise ValueError("Invalid segmentation proposal")


@dataclass(frozen=True, slots=True)
class SegmentationAttempt:
    number: int
    failure_kind: str | None = None
    validation_codes: tuple[str, ...] = ()
    metadata: SemanticCallMetadata | None = None

    def __post_init__(self):
        if (type(self.number) is not int or not 1 <= self.number <= 3
                or self.failure_kind is not None and self.failure_kind not in FAILURE_KINDS
                or not isinstance(self.validation_codes, tuple)
                or any(code not in VALIDATION_CODES for code in self.validation_codes)
                or self.failure_kind is None and self.validation_codes
                or self.metadata is not None and not isinstance(self.metadata, SemanticCallMetadata)):
            raise ValueError("Invalid attempt provenance")


class SegmentationCapabilityError(RuntimeError):
    def __init__(self, kind: str, *, retryable: bool, metadata: SemanticCallMetadata | None = None):
        if (kind not in FAILURE_KINDS or type(retryable) is not bool
                or metadata is not None and not isinstance(metadata, SemanticCallMetadata)):
            raise ValueError("Invalid capability error")
        self.kind, self.retryable, self.metadata = kind, retryable, metadata
        super().__init__(f"Segmentation provider failed ({kind})")


class SegmentationError(RuntimeError):
    def __init__(self, video_id: str | None, phase: str, *, attempts=()):
        self.video_id, self.phase, self.attempts = video_id, phase, tuple(attempts)
        super().__init__(f"Segmentation failed ({phase}, source={video_id})")


class SegmentationInputError(SegmentationError):
    """Canonical cleaned Silver is missing, invalid or unsupported."""


class SegmentationContractError(SegmentationError):
    """Invalid result serialization, structure or supported versions."""


class SegmentationIntegrityError(SegmentationError):
    """Canonical input identity, provenance or content is incompatible."""


class SegmentationProviderError(SegmentationError):
    """An unrecoverable or exhausted semantic provider failure."""


class SegmentationValidationError(SegmentationError):
    """No valid complete proposal was obtained within the attempt budget."""


class SegmentationFilesystemError(SegmentationError):
    """Durable processing did not complete safely."""


class SegmentationCapability(Protocol):
    def propose_sections(self, request: SegmentationRequest) -> SegmentationProposal: ...


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    root: str | Path = defaults.SEGMENTATION_ROOT
    max_attempts: int = defaults.SEGMENTATION_MAX_ATTEMPTS
    retry_delays_seconds: tuple[float, float] = defaults.SEGMENTATION_RETRY_DELAYS_SECONDS

    def __post_init__(self):
        if (not isinstance(self.root, (str, Path)) or not str(self.root).strip()
                or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3
                or not isinstance(self.retry_delays_seconds, tuple) or len(self.retry_delays_seconds) != 2
                or any(type(v) not in (int, float) or not 0 <= v <= 60 or not math.isfinite(v)
                       for v in self.retry_delays_seconds)):
            raise ValueError("Invalid local segmentation configuration")


DEFAULT_SEGMENTATION_CONFIG = SegmentationConfig()


@dataclass(frozen=True, slots=True)
class SegmentationProcessingResult:
    video_id: str
    path: Path
    outcome: SegmentationOutcome
    created: bool
    artifact: dict = field(repr=False)

    @property
    def successful_segmentation(self):
        return self.outcome is SegmentationOutcome.SEGMENTED
