"""Selected-source Bronze artifacts and post-persistence catalog registration."""

from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Protocol

from . import _bronze_filesystem as filesystem
from .defaults import BRONZE_ROOT, SELECTION_MAX_RATIONALE_CHARACTERS
from .models import ChampionTarget, _require_video_id
from .selection_models import (
    CandidateQualityAssessment, ComparativeCandidateAssessment, QualityGateResult,
    QualityReasonCode, SelectedSource, SelectionAttempt, SelectionFailureKind,
    SelectionMethod, SelectionStage, SemanticCallMetadata, _attempt_history,
)
from .transcript_models import CaptionKind, TranscriptSegment

BRONZE_SCHEMA_VERSION = 1


class BronzeError(RuntimeError):
    """Integration did not complete; messages never contain source content."""

    def __init__(self, video_id: str | None, phase: str):
        self.video_id = video_id
        self.phase = phase
        super().__init__(f"Bronze integration failed ({phase}, source={video_id})")


class BronzeContractError(BronzeError):
    """Invalid integration input or artifact contract."""


class BronzeIntegrityError(BronzeError):
    """Existing canonical material is invalid or conflicts with the source."""


class BronzeFilesystemError(BronzeError):
    """Local persistence could not complete safely."""


class BronzeRegistrationError(BronzeError):
    """Bronze remains available, but catalog registration did not complete."""


class IntegratedSourceCatalog(Protocol):
    def is_integrated(self, video_id: str) -> bool: ...

    def register_integrated(self, video_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BronzeConfig:
    root: str | Path = BRONZE_ROOT

    def __post_init__(self):
        if not isinstance(self.root, (str, Path)) or not str(self.root).strip():
            raise ValueError("Bronze requires a filesystem root")


DEFAULT_BRONZE_CONFIG = BronzeConfig()


@dataclass(frozen=True, slots=True)
class BronzeIntegrationContext:
    run_id: str | int | None = None
    selected_at_utc: datetime | None = None
    target: ChampionTarget | None = None

    def __post_init__(self):
        _run_id(self.run_id)
        if self.selected_at_utc is not None:
            _utc(self.selected_at_utc)
        if self.target is not None and not isinstance(self.target, ChampionTarget):
            raise ValueError("Invalid Bronze target context")


@dataclass(frozen=True, slots=True)
class BronzeIntegrationResult:
    video_id: str
    path: Path
    created: bool
    registered: bool


@dataclass(frozen=True, slots=True)
class ExistingBronzeState:
    video_id: str
    path: Path
    registered: bool


def canonical_bronze_path(video_id: str, config: BronzeConfig = DEFAULT_BRONZE_CONFIG) -> Path:
    _require_video_id(video_id)
    return Path(config.root).expanduser().resolve() / "youtube" / ("id-" + video_id.encode("ascii").hex() + ".json")


def _url(video_id):
    _require_video_id(video_id)
    return f"https://www.youtube.com/watch?v={video_id}"


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Expected UTC timestamp")
    return value.isoformat()


def _run_id(value):
    if value is not None and not (type(value) is int or isinstance(value, str) and value.strip()):
        raise ValueError("Invalid optional run identity")


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("Invalid artifact fields")
    return value


def _string(value, *, nullable=False, nonempty=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or nonempty and not value.strip():
        raise ValueError("Invalid artifact text")


def _timestamp(value, *, nullable=False):
    if nullable and value is None:
        return
    _string(value, nonempty=True)
    _utc(datetime.fromisoformat(value))


def _count(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < 0:
        raise ValueError("Invalid artifact count")


def _timing(value):
    _string(value, nonempty=True)
    if value != value.strip():
        raise ValueError("Invalid decimal representation")
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid artifact timing")
    return result


def _metadata(value):
    if value is None:
        return None
    _keys(value, "provider requested_model reported_model prompt_version")
    return SemanticCallMetadata(**value)


def validate_bronze_artifact(artifact: dict) -> dict:
    """Strict v1 validation; return the unmodified artifact, never normalize it."""
    try:
        _keys(artifact, "schema_version source acquisition selection transcript")
        if type(artifact["schema_version"]) is not int or artifact["schema_version"] != BRONZE_SCHEMA_VERSION:
            raise ValueError("Unsupported Bronze version")
        source = _keys(artifact["source"], "type video_id url title channel_title published_at_utc duration_seconds view_count")
        _require_video_id(source["video_id"])
        if source["type"] != "youtube" or source["url"] != _url(source["video_id"]):
            raise ValueError("Invalid canonical source")
        for name in ("title", "channel_title"):
            _string(source[name], nullable=True)
        _timestamp(source["published_at_utc"], nullable=True)
        for name in ("duration_seconds", "view_count"):
            _count(source[name], nullable=True)
        acquisition = _keys(artifact["acquisition"], "target_id target_key target_name run_id selected_at_utc bronze_created_at_utc")
        _string(acquisition["target_id"], nonempty=True)
        for name in ("target_key", "target_name"):
            _string(acquisition[name], nullable=True, nonempty=True)
        _run_id(acquisition["run_id"])
        _timestamp(acquisition["selected_at_utc"], nullable=True)
        _timestamp(acquisition["bronze_created_at_utc"])
        transcript = _keys(artifact["transcript"], "language_code caption_kind source_track_id provider_name provider_version text segments")
        for name in ("language_code", "provider_name", "provider_version"):
            _string(transcript[name], nonempty=True)
        CaptionKind(transcript["caption_kind"])
        _string(transcript["source_track_id"], nullable=True, nonempty=True)
        segments = transcript["segments"]
        if type(segments) is not list or not segments:
            raise ValueError("Missing ordered transcript")
        previous_index = -1
        for segment in segments:
            _keys(segment, "source_index source_text text start_seconds duration_seconds")
            TranscriptSegment(segment["source_index"], segment["source_text"], segment["text"],
                              _timing(segment["start_seconds"]), _timing(segment["duration_seconds"]))
            if segment["source_index"] <= previous_index:
                raise ValueError("Invalid source order")
            previous_index = segment["source_index"]
        if transcript["text"] != "\n".join(segment["text"] for segment in segments):
            raise ValueError("Full transcript does not match segments")
        selection = _keys(artifact["selection"], "method winner_quality quality_metadata comparison attempts")
        method = SelectionMethod(selection["method"])
        quality = _keys(selection["winner_quality"], "video_id target_relevant educational_substance transcript_usable noise_dominates rationale reason_codes passed")
        if type(quality["reason_codes"]) is not list:
            raise ValueError("Invalid reason codes")
        assessment = CandidateQualityAssessment(**{
            name: value for name, value in quality.items() if name not in ("passed", "reason_codes")
        }, reason_codes=tuple(QualityReasonCode(code) for code in quality["reason_codes"]))
        if (quality["video_id"] != source["video_id"] or quality["passed"] is not True
                or not QualityGateResult(assessment).passed):
            raise ValueError("Winner did not pass quality gate")
        quality_metadata = _metadata(selection["quality_metadata"])
        comparison = selection["comparison"]
        if method is SelectionMethod.ONLY_PASSING_CANDIDATE:
            if comparison is not None:
                raise ValueError("Direct selection cannot contain comparison")
        else:
            _keys(comparison, "selection_reason winner_assessment metadata")
            _string(comparison["selection_reason"], nonempty=True)
            if len(comparison["selection_reason"]) > SELECTION_MAX_RATIONALE_CHARACTERS:
                raise ValueError("Invalid comparison reason")
            _keys(comparison["winner_assessment"], "video_id target_relevance educational_usefulness information_density clarity breadth_depth noise")
            winner = ComparativeCandidateAssessment(**comparison["winner_assessment"])
            if winner.video_id != source["video_id"]:
                raise ValueError("Invalid comparison winner")
            _metadata(comparison["metadata"])
        if type(selection["attempts"]) is not list:
            raise ValueError("Invalid attempt sequence")
        attempts = []
        for attempt in selection["attempts"]:
            _keys(attempt, "stage number failure_kind")
            attempts.append(SelectionAttempt(SelectionStage(attempt["stage"]), attempt["number"],
                            None if attempt["failure_kind"] is None else SelectionFailureKind(attempt["failure_kind"])))
        _attempt_history(tuple(attempts), quality_metadata)
        expected_stage = SelectionStage.QUALITY if comparison is None else SelectionStage.COMPARISON
        if not attempts or attempts[-1].stage is not expected_stage or attempts[-1].failure_kind is not None:
            raise ValueError("Incomplete selection history")
        return artifact
    except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError, OverflowError):
        raise BronzeContractError(None, "artifact_validation") from None


def construct_bronze_artifact(selected: SelectedSource, *, context=None, created_at_utc=None) -> dict:
    """Explicit winner-only projection; no losing-candidate content or runtime handles."""
    try:
        if not isinstance(selected, SelectedSource):
            raise ValueError("Expected a successfully selected source")
        context = context if context is not None else BronzeIntegrationContext()
        if not isinstance(context, BronzeIntegrationContext):
            raise ValueError("Invalid acquisition context")
        if context.target is not None and context.target.id != selected.target_id:
            raise ValueError("Acquisition target mismatch")
        transcript = selected.source
        candidate = transcript.ranked_candidate.candidate
        video_id = candidate.video_id
        _require_video_id(video_id)
        winner_gate = next(gate for gate in selected.quality_gate_results if gate.video_id == video_id)
        quality = asdict(winner_gate.assessment)
        quality["reason_codes"] = [code.value for code in winner_gate.assessment.reason_codes]
        quality["passed"] = winner_gate.passed
        comparison = None
        if selected.comparison is not None:
            winner = next(item for item in selected.comparison.assessments if item.video_id == video_id)
            comparison = {"selection_reason": selected.comparison.selection_reason,
                          "winner_assessment": asdict(winner),
                          "metadata": None if selected.comparison.metadata is None else asdict(selected.comparison.metadata)}
        artifact = {
            "schema_version": BRONZE_SCHEMA_VERSION,
            "source": {"type": "youtube", "video_id": video_id, "url": _url(video_id),
                       "title": candidate.title, "channel_title": candidate.channel_title,
                       "published_at_utc": None if candidate.published_at_utc is None else _utc(candidate.published_at_utc),
                       "duration_seconds": candidate.duration_seconds, "view_count": candidate.view_count},
            "acquisition": {"target_id": selected.target_id,
                            "target_key": None if context.target is None else context.target.key,
                            "target_name": None if context.target is None else context.target.name,
                            "run_id": context.run_id,
                            "selected_at_utc": None if context.selected_at_utc is None else _utc(context.selected_at_utc),
                            "bronze_created_at_utc": _utc(created_at_utc if created_at_utc is not None else datetime.now(timezone.utc))},
            "selection": {"method": selected.method.value, "winner_quality": quality,
                          "quality_metadata": None if selected.quality_metadata is None else asdict(selected.quality_metadata),
                          "comparison": comparison,
                          "attempts": [{"stage": a.stage.value, "number": a.number,
                                        "failure_kind": None if a.failure_kind is None else a.failure_kind.value}
                                       for a in selected.attempts]},
            "transcript": {"language_code": transcript.track.language_code,
                           "caption_kind": transcript.track.kind.value,
                           "source_track_id": transcript.track.source_track_id,
                           "provider_name": transcript.track.provider_name,
                           "provider_version": transcript.track.provider_version,
                           "text": transcript.text,
                           "segments": [{"source_index": s.source_index, "source_text": s.source_text,
                                         "text": s.text, "start_seconds": str(s.start_seconds),
                                         "duration_seconds": str(s.duration_seconds)} for s in transcript.segments]},
        }
        return validate_bronze_artifact(artifact)
    except BronzeError:
        raise
    except (ValueError, TypeError, AttributeError, StopIteration):
        raise BronzeContractError(None, "construction") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant")


def decode_bronze_artifact(content: bytes) -> dict:
    try:
        artifact = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise BronzeContractError(None, "decoding") from None
    return validate_bronze_artifact(artifact)


def _identity(artifact):
    source, transcript = artifact["source"], artifact["transcript"]
    return (source["type"], source["video_id"], source["url"], transcript["language_code"],
            transcript["caption_kind"], transcript["text"], tuple(
                (s["source_index"], s["source_text"], s["text"],
                 _timing(s["start_seconds"]), _timing(s["duration_seconds"]))
                for s in transcript["segments"]))


def _require_compatible(existing, incoming, video_id):
    left, right = existing["transcript"]["source_track_id"], incoming["transcript"]["source_track_id"]
    if _identity(existing) != _identity(incoming) or left is not None and right is not None and left != right:
        raise BronzeIntegrityError(video_id, "content_conflict")


def _read_canonical(path, incoming, video_id):
    if path.is_symlink() or not path.is_file():
        raise BronzeIntegrityError(video_id, "canonical_file")
    try:
        existing = decode_bronze_artifact(path.read_bytes())
    except BronzeContractError:
        raise BronzeIntegrityError(video_id, "existing_artifact_validation") from None
    if existing["source"]["video_id"] != video_id:
        raise BronzeIntegrityError(video_id, "canonical_identity")
    if incoming is not None:
        _require_compatible(existing, incoming, video_id)
    return existing


def inspect_existing_bronze(video_id: str, *, catalog: IntegratedSourceCatalog,
                            config: BronzeConfig = DEFAULT_BRONZE_CONFIG) -> ExistingBronzeState | None:
    """Read canonical material and catalog state; never write, flush, or register."""
    path = canonical_bronze_path(video_id, config)
    try:
        exists = path.exists() or path.is_symlink()
        if exists:
            _read_canonical(path, None, video_id)
        try:
            registered = catalog.is_integrated(video_id)
        except Exception:
            raise BronzeRegistrationError(video_id, "catalog_inspection") from None
        if type(registered) is not bool:
            raise BronzeRegistrationError(video_id, "catalog_inspection_contract")
        if registered and not exists:
            raise BronzeIntegrityError(video_id, "registered_bronze_missing")
        return ExistingBronzeState(video_id, path, registered) if exists else None
    except BronzeError:
        raise
    except OSError:
        raise BronzeFilesystemError(video_id, "existing_artifact") from None


def recover_existing_bronze(video_id: str, *, catalog: IntegratedSourceCatalog,
                            config: BronzeConfig = DEFAULT_BRONZE_CONFIG) -> BronzeIntegrationResult:
    """Validate/flush existing canonical bytes, then register; never reconstruct or publish."""
    path = canonical_bronze_path(video_id, config)
    try:
        _read_canonical(path, None, video_id)
        filesystem.confirm_durable(path)
        registered = _register(catalog, video_id)
        return BronzeIntegrationResult(video_id, path, False, registered)
    except BronzeError:
        raise
    except OSError:
        raise BronzeFilesystemError(video_id, "existing_recovery") from None


def _register(catalog, video_id):
    try:
        registered = catalog.register_integrated(video_id)
    except Exception:
        raise BronzeRegistrationError(video_id, "registration") from None
    if type(registered) is not bool:
        raise BronzeRegistrationError(video_id, "registration_contract")
    return registered


def integrate_selected_source(selected: SelectedSource, *, catalog: IntegratedSourceCatalog,
                              config: BronzeConfig = DEFAULT_BRONZE_CONFIG,
                              context: BronzeIntegrationContext | None = None) -> BronzeIntegrationResult:
    """Caller serializes integrations and shares one Bronze root with this catalog.

    No catalog mutation occurs until a complete durable canonical file is validated.
    Existing first-integration provenance is never rewritten or backfilled.
    """
    artifact = construct_bronze_artifact(selected, context=context)
    video_id = artifact["source"]["video_id"]
    path = canonical_bronze_path(video_id, config)
    encoded_id = video_id.encode("ascii").hex()
    temporary = None
    created = False
    phase = "existing_artifact"
    try:
        if path.exists() or path.is_symlink():
            _read_canonical(path, artifact, video_id)
            filesystem.cleanup_temporary_files(path.parent, encoded_id)
        else:
            try:
                already_registered = catalog.is_integrated(video_id)
            except Exception:
                raise BronzeRegistrationError(video_id, "catalog_preflight") from None
            if already_registered:
                raise BronzeIntegrityError(video_id, "registered_bronze_missing")
            phase = "serialization"
            content = (json.dumps(artifact, ensure_ascii=False, allow_nan=False,
                                  sort_keys=True, indent=2) + "\n").encode("utf-8")
            phase = "directory_setup"
            filesystem.prepare_directory(path.parent)
            filesystem.cleanup_temporary_files(path.parent, encoded_id)
            phase = "temporary_write"
            temporary = filesystem.stage(path.parent, encoded_id, content)
            phase = "temporary_validation"
            if decode_bronze_artifact(temporary.read_bytes()) != artifact:
                raise BronzeIntegrityError(video_id, phase)
            phase = "publication"
            try:
                filesystem.publish(temporary, path)
                created = True
            except FileExistsError:
                # Publication is non-overwriting even if callers violate serialization.
                _read_canonical(path, artifact, video_id)
            phase = "final_validation"
            _read_canonical(path, artifact, video_id)
        phase = "durability_confirmation"
        filesystem.confirm_durable(path)
        registered = _register(catalog, video_id)
        return BronzeIntegrationResult(video_id, path, created, registered)
    except BronzeError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise BronzeFilesystemError(video_id, phase) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
