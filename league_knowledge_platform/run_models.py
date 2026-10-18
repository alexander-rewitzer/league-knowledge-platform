"""Immutable local acquisition progress; selected identity is not integration."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json

from .defaults import MAX_TARGET_ATTEMPTS
from .models import ChampionCatalog, ChampionTarget, _require_video_id
from .selection_models import NoSelectionReason, SelectionMethod


class RunStateError(RuntimeError):
    """Run state cannot be read or changed safely; never a target-local failure."""


class RunNotFoundError(RunStateError):
    """The explicitly requested run does not exist."""


class RunConfigurationError(ValueError):
    """Invalid orchestration configuration, detected before target execution."""


class RunStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESUMABLE = "RESUMABLE"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"


class RunTargetStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SELECTED = "SELECTED"
    INTEGRATED = "INTEGRATED"
    NO_SELECTION = "NO_SELECTION"
    FAILED = "FAILED"


class RunStage(str, Enum):
    DISCOVERY = "DISCOVERY"
    COLLECTION = "COLLECTION"
    RANKING = "RANKING"
    TRANSCRIPTS = "TRANSCRIPTS"
    QUALITY = "QUALITY"
    COMPARISON = "COMPARISON"
    ORCHESTRATION = "ORCHESTRATION"
    BRONZE_PERSISTENCE = "BRONZE_PERSISTENCE"
    BRONZE_VALIDATION = "BRONZE_VALIDATION"
    SOURCE_REGISTRATION = "SOURCE_REGISTRATION"


class TargetFailureKind(str, Enum):
    CONFIGURATION = "CONFIGURATION"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    QUOTA = "QUOTA"
    NETWORK = "NETWORK"
    SERVICE = "SERVICE"
    TIMEOUT = "TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    RATE_LIMIT = "RATE_LIMIT"
    BLOCKED = "BLOCKED"
    PARSING = "PARSING"
    CONTRACT = "CONTRACT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    BALANCE = "BALANCE"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_CONTRACT = "PROVIDER_CONTRACT"
    INTERRUPTED = "INTERRUPTED"
    UNEXPECTED_COMPONENT_EXCEPTION = "UNEXPECTED_COMPONENT_EXCEPTION"
    FILESYSTEM = "FILESYSTEM"
    INTEGRITY = "INTEGRITY"
    REGISTRATION = "REGISTRATION"


def require_utc(value: datetime) -> None:
    if (not isinstance(value, datetime)
            or value.utcoffset() != timezone.utc.utcoffset(None)):
        raise ValueError("Run timestamps must be timezone-aware UTC")


def require_run_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("An explicit nonempty run ID is required")


def catalog_digest(catalog: ChampionCatalog) -> str:
    """Hash canonical persisted fields, not a file location or mutable latest roster."""
    content = {
        "schema_version": catalog.schema_version, "roster_version": catalog.roster_version,
        "generated_at_utc": catalog.generated_at_utc.isoformat(),
        "source_provider": catalog.source_provider, "source_url": catalog.source_url,
        "source_locale": catalog.source_locale,
        "targets": [(t.id, t.key, t.name) for t in catalog.targets],
    }
    return hashlib.sha256(json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TargetFailure:
    stage: RunStage
    kind: TargetFailureKind

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RunStage) or not isinstance(self.kind, TargetFailureKind):
            raise ValueError("Target failures require typed stage and kind")


@dataclass(frozen=True, slots=True)
class AcquisitionRun:
    run_id: str
    status: RunStatus
    catalog: ChampionCatalog
    snapshot_sha256: str
    max_target_attempts: int
    started_at_utc: datetime
    updated_at_utc: datetime
    finished_at_utc: datetime | None

    def __post_init__(self) -> None:
        require_run_id(self.run_id)
        if (not isinstance(self.status, RunStatus) or not isinstance(self.catalog, ChampionCatalog)
                or self.snapshot_sha256 != catalog_digest(self.catalog)
                or type(self.max_target_attempts) is not int
                or self.max_target_attempts != MAX_TARGET_ATTEMPTS):
            raise ValueError("Invalid acquisition run snapshot or attempt limit")
        require_utc(self.started_at_utc)
        require_utc(self.updated_at_utc)
        terminal = self.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES)
        if terminal != (self.finished_at_utc is not None):
            raise ValueError("Run completion and finish time disagree")
        if self.finished_at_utc is not None:
            require_utc(self.finished_at_utc)


@dataclass(frozen=True, slots=True)
class RunTarget:
    run_id: str
    target: ChampionTarget
    ordinal: int
    status: RunTargetStatus
    attempt_count: int
    selected_video_id: str | None
    selection_method: SelectionMethod | None
    no_selection_reason: NoSelectionReason | None
    failure: TargetFailure | None
    first_started_at_utc: datetime | None
    attempt_started_at_utc: datetime | None
    updated_at_utc: datetime
    finished_at_utc: datetime | None
    selected_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        require_run_id(self.run_id)
        if (not isinstance(self.target, ChampionTarget) or type(self.ordinal) is not int
                or self.ordinal < 0 or not isinstance(self.status, RunTargetStatus)
                or type(self.attempt_count) is not int
                or not 0 <= self.attempt_count <= MAX_TARGET_ATTEMPTS):
            raise ValueError("Invalid acquisition target progress")
        require_utc(self.updated_at_utc)
        for value in (self.updated_at_utc, self.first_started_at_utc,
                      self.attempt_started_at_utc, self.finished_at_utc, self.selected_at_utc):
            if value is not None:
                require_utc(value)
        pending = self.status is RunTargetStatus.PENDING
        if pending != (self.attempt_count == 0):
            raise ValueError("Pending state and attempt count disagree")
        if pending:
            if any(v is not None for v in (self.first_started_at_utc,
                                          self.attempt_started_at_utc, self.finished_at_utc)):
                raise ValueError("Pending work cannot have execution timestamps")
        elif self.first_started_at_utc is None or self.attempt_started_at_utc is None:
            raise ValueError("Started work needs execution timestamps")
        finished = self.status in (RunTargetStatus.INTEGRATED, RunTargetStatus.NO_SELECTION,
                                   RunTargetStatus.FAILED)
        if finished != (self.finished_at_utc is not None):
            raise ValueError("Target outcome and finish time disagree")
        if self.selected_video_id is not None:
            _require_video_id(self.selected_video_id)
            if (not isinstance(self.selection_method, SelectionMethod) or self.selected_at_utc is None
                    or self.status not in (RunTargetStatus.RUNNING, RunTargetStatus.SELECTED,
                                           RunTargetStatus.INTEGRATED, RunTargetStatus.FAILED)):
                raise ValueError("Selected work needs a selection method")
        elif (self.selection_method is not None or self.selected_at_utc is not None
              or self.status in (RunTargetStatus.SELECTED, RunTargetStatus.INTEGRATED)):
            raise ValueError("Selection fields must identify one selected source")
        if self.status is RunTargetStatus.NO_SELECTION:
            if not isinstance(self.no_selection_reason, NoSelectionReason):
                raise ValueError("No selection needs a domain reason")
        elif self.no_selection_reason is not None:
            raise ValueError("Unexpected no-selection reason")
        if (self.status is RunTargetStatus.FAILED) != isinstance(self.failure, TargetFailure):
            raise ValueError("Failed work needs a typed failure")
        if self.status is not RunTargetStatus.FAILED and self.failure is not None:
            raise ValueError("Only failed work has failure information")

    @property
    def terminal(self) -> bool:
        return (self.status in (RunTargetStatus.INTEGRATED, RunTargetStatus.NO_SELECTION)
                or self.status is RunTargetStatus.FAILED
                and self.attempt_count >= MAX_TARGET_ATTEMPTS)


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    status: RunStatus
    total_targets: int
    pending: int
    running: int
    selected: int
    no_selection: int
    failed: int
    retryable_targets: int
    integrated: int = 0
