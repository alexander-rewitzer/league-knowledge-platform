"""Deterministic, source-faithful cleaning of immutable canonical Bronze material."""

from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

from . import _bronze_filesystem as filesystem
from .bronze import (
    BRONZE_SCHEMA_VERSION, DEFAULT_BRONZE_CONFIG, BronzeConfig, BronzeContractError,
    canonical_bronze_path, decode_bronze_artifact, validate_bronze_artifact,
)
from .defaults import SILVER_ROOT
from .models import _require_video_id
from .transcript_models import CaptionKind

SILVER_SCHEMA_VERSION = 1
SILVER_CLEANING_RULE_VERSION = 1
_TRACK_FIELDS = ("language_code", "caption_kind", "source_track_id", "provider_name", "provider_version")


class SilverError(RuntimeError):
    """Messages identify the phase and source, never transcript content."""

    def __init__(self, video_id: str | None, phase: str):
        self.video_id = video_id
        self.phase = phase
        super().__init__(f"Silver cleaning failed ({phase}, source={video_id})")


class SilverInputError(SilverError):
    """Bronze input is missing, invalid, or unsupported."""


class SilverContractError(SilverError):
    """Silver structure, serialization, or supported rules are invalid."""


class SilverIntegrityError(SilverError):
    """Canonical identity, provenance, or expected material content conflicts."""


class SilverFilesystemError(SilverError):
    """Local filesystem processing could not complete safely."""


@dataclass(frozen=True, slots=True)
class SilverConfig:
    root: str | Path = SILVER_ROOT

    def __post_init__(self):
        if not isinstance(self.root, (str, Path)) or not str(self.root).strip():
            raise ValueError("Silver requires a filesystem root")


DEFAULT_SILVER_CONFIG = SilverConfig()


@dataclass(frozen=True, slots=True)
class SilverCleaningResult:
    video_id: str
    path: Path
    created: bool


def canonical_silver_path(video_id: str, config: SilverConfig = DEFAULT_SILVER_CONFIG) -> Path:
    return Path(config.root).expanduser().resolve() / "youtube" / canonical_bronze_path(video_id).name


def _normalize(source_text):
    normalized = unicodedata.normalize("NFC", source_text)
    return " ".join(normalized.split())


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
    if datetime.fromisoformat(value).utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Expected UTC timestamp")


def _count(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < 0:
        raise ValueError("Expected nonnegative integer")


def _timing(value):
    _string(value, nonempty=True)
    if value != value.strip() or not Decimal(value).is_finite() or Decimal(value) < 0:
        raise ValueError("Invalid decimal timing")


def validate_silver_artifact(artifact: dict) -> dict:
    """Validate standalone v1 content; Unicode version is informational only."""
    try:
        _keys(artifact, "schema_version cleaning source bronze acquisition transcript")
        if type(artifact["schema_version"]) is not int or artifact["schema_version"] != SILVER_SCHEMA_VERSION:
            raise ValueError("Unsupported Silver version")
        cleaning = _keys(artifact["cleaning"], "rule_version input_field unicode_version")
        if (type(cleaning["rule_version"]) is not int
                or cleaning["rule_version"] != SILVER_CLEANING_RULE_VERSION
                or cleaning["input_field"] != "source_text"):
            raise ValueError("Unsupported cleaning rule")
        _string(cleaning["unicode_version"], nonempty=True)
        source = _keys(artifact["source"], "type video_id url title channel_title published_at_utc duration_seconds view_count")
        video_id = source["video_id"]
        _require_video_id(video_id)
        if source["type"] != "youtube" or source["url"] != f"https://www.youtube.com/watch?v={video_id}":
            raise ValueError("Invalid canonical source")
        for name in ("title", "channel_title"):
            _string(source[name], nullable=True)
        _timestamp(source["published_at_utc"], nullable=True)
        for name in ("duration_seconds", "view_count"):
            _count(source[name], nullable=True)
        bronze = _keys(artifact["bronze"], "schema_version relative_path sha256")
        if (type(bronze["schema_version"]) is not int or bronze["schema_version"] != BRONZE_SCHEMA_VERSION
                or bronze["relative_path"] != f"youtube/{canonical_bronze_path(video_id).name}"
                or not isinstance(bronze["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", bronze["sha256"]) is None):
            raise ValueError("Invalid Bronze reference")
        acquisition = _keys(artifact["acquisition"], "target_id target_key target_name run_id selected_at_utc bronze_created_at_utc")
        _string(acquisition["target_id"], nonempty=True)
        for name in ("target_key", "target_name"):
            _string(acquisition[name], nullable=True, nonempty=True)
        run_id = acquisition["run_id"]
        if run_id is not None and not (type(run_id) is int or isinstance(run_id, str) and run_id.strip()):
            raise ValueError("Invalid run identity")
        _timestamp(acquisition["selected_at_utc"], nullable=True)
        _timestamp(acquisition["bronze_created_at_utc"])
        transcript = _keys(artifact["transcript"], "language_code caption_kind source_track_id provider_name provider_version text segments")
        for name in ("language_code", "provider_name", "provider_version"):
            _string(transcript[name], nonempty=True)
        CaptionKind(transcript["caption_kind"])
        _string(transcript["source_track_id"], nullable=True, nonempty=True)
        segments = transcript["segments"]
        if type(segments) is not list or not segments:
            raise ValueError("Missing ordered segments")
        previous_index = -1
        for segment in segments:
            _keys(segment, "source_index text start_seconds duration_seconds")
            _count(segment["source_index"])
            if segment["source_index"] <= previous_index:
                raise ValueError("Invalid source order")
            previous_index = segment["source_index"]
            for name in ("start_seconds", "duration_seconds"):
                _timing(segment[name])
            _string(segment["text"])
            if _normalize(segment["text"]) != segment["text"]:
                raise ValueError("Text does not satisfy cleaning rules")
        if transcript["text"] != " ".join(s["text"] for s in segments if s["text"]):
            raise ValueError("Full transcript does not match segments")
        return artifact
    except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError, OverflowError, RecursionError):
        raise SilverContractError(None, "artifact_validation") from None


def construct_silver_artifact(bronze_artifact: dict, *, bronze_sha256: str) -> dict:
    try:
        validate_bronze_artifact(bronze_artifact)
    except BronzeContractError:
        raise SilverInputError(None, "bronze_validation") from None
    bronze = bronze_artifact
    segments = [{"source_index": s["source_index"], "text": _normalize(s["source_text"]),
                 "start_seconds": s["start_seconds"], "duration_seconds": s["duration_seconds"]}
                for s in bronze["transcript"]["segments"]]
    artifact = {
        "schema_version": SILVER_SCHEMA_VERSION,
        "cleaning": {"rule_version": SILVER_CLEANING_RULE_VERSION, "input_field": "source_text",
                     "unicode_version": unicodedata.unidata_version},
        "source": deepcopy(bronze["source"]),
        "bronze": {"schema_version": bronze["schema_version"],
                   "relative_path": f"youtube/{canonical_bronze_path(bronze['source']['video_id']).name}",
                   "sha256": bronze_sha256},
        "acquisition": deepcopy(bronze["acquisition"]),
        "transcript": {**{name: bronze["transcript"][name] for name in _TRACK_FIELDS},
                       "text": " ".join(s["text"] for s in segments if s["text"]), "segments": segments},
    }
    return validate_silver_artifact(artifact)


def _material(artifact):
    # Exclude only diagnostic Unicode provenance; every other field remains material.
    return {**artifact, "cleaning": {name: value for name, value in artifact["cleaning"].items()
                                    if name != "unicode_version"}}


def validate_silver_against_bronze(artifact: dict, bronze_artifact: dict, *, bronze_sha256: str) -> dict:
    validate_silver_artifact(artifact)
    expected = construct_silver_artifact(bronze_artifact, bronze_sha256=bronze_sha256)
    _require_compatible(artifact, expected)
    return artifact


def _require_compatible(existing, expected):
    if _material(existing) != _material(expected):
        raise SilverIntegrityError(expected["source"]["video_id"], "existing_conflict")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant")


def decode_silver_artifact(content: bytes) -> dict:
    try:
        artifact = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise SilverContractError(None, "decoding") from None
    return validate_silver_artifact(artifact)


def _read_silver(path, expected, video_id):
    if path.is_symlink() or not path.is_file():
        raise SilverIntegrityError(video_id, "canonical_file")
    artifact = decode_silver_artifact(path.read_bytes())
    _require_compatible(artifact, expected)
    return artifact


def clean_bronze_source(video_id: str, *, bronze_config: BronzeConfig = DEFAULT_BRONZE_CONFIG,
                        silver_config: SilverConfig = DEFAULT_SILVER_CONFIG) -> SilverCleaningResult:
    """Caller serializes cleaning; Bronze is read-only and no catalog is needed."""
    _require_video_id(video_id)
    temporary = None
    created = False
    phase = "configuration"
    try:
        bronze_root = Path(bronze_config.root).expanduser().resolve()
        silver_root = Path(silver_config.root).expanduser().resolve()
        if bronze_root.is_relative_to(silver_root) or silver_root.is_relative_to(bronze_root):
            raise SilverIntegrityError(video_id, "overlapping_roots")
        bronze_path = canonical_bronze_path(video_id, bronze_config)
        path = canonical_silver_path(video_id, silver_config)
        if (not bronze_path.parent.resolve().is_relative_to(bronze_root)
                or not path.parent.resolve().is_relative_to(silver_root)):
            raise SilverIntegrityError(video_id, "canonical_directory")
        phase = "bronze_read"
        if bronze_path.is_symlink():
            raise SilverIntegrityError(video_id, "canonical_identity")
        try:
            content = bronze_path.read_bytes()
        except FileNotFoundError:
            raise SilverInputError(video_id, "bronze_missing") from None
        try:
            bronze = decode_bronze_artifact(content)
        except BronzeContractError:
            raise SilverInputError(video_id, "bronze_validation") from None
        if bronze["source"]["video_id"] != video_id:
            raise SilverIntegrityError(video_id, "canonical_identity")
        expected = construct_silver_artifact(bronze, bronze_sha256=sha256(content).hexdigest())
        phase = "existing_artifact"
        encoded_id = video_id.encode("ascii").hex()
        if path.exists() or path.is_symlink():
            _read_silver(path, expected, video_id)
            filesystem.cleanup_temporary_files(path.parent, encoded_id, namespace="silver")
        else:
            try:
                serialized = (json.dumps(expected, ensure_ascii=False, allow_nan=False,
                                         sort_keys=True, indent=2) + "\n").encode("utf-8")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise SilverContractError(video_id, "serialization") from None
            phase = "directory_setup"
            filesystem.prepare_directory(path.parent)
            filesystem.cleanup_temporary_files(path.parent, encoded_id, namespace="silver")
            phase = "temporary_write"
            temporary = filesystem.stage(path.parent, encoded_id, serialized, namespace="silver")
            phase = "temporary_validation"
            if decode_silver_artifact(temporary.read_bytes()) != expected:
                raise SilverIntegrityError(video_id, phase)
            phase = "publication"
            try:
                filesystem.publish(temporary, path)
                created = True
            except FileExistsError:
                _read_silver(path, expected, video_id)
        phase = "final_validation"
        _read_silver(path, expected, video_id)
        phase = "durability_confirmation"
        filesystem.confirm_durable(path)
        return SilverCleaningResult(video_id, path, created)
    except SilverError:
        raise
    except OSError:
        raise SilverFilesystemError(video_id, phase) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
