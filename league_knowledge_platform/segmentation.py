"""Single semantic SEGMENT stage with deterministic validation and canonical outcomes."""

from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, DecimalException, localcontext, MAX_EMAX, MIN_EMIN
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from . import _bronze_filesystem as filesystem, defaults
from .bronze import DEFAULT_BRONZE_CONFIG, BronzeConfig
from .segmentation_models import (
    DEFAULT_SEGMENTATION_CONFIG, SectionKind, SectionSpan, SegmentationAttempt,
    SegmentationCapability, SegmentationCapabilityError, SegmentationConfig,
    SegmentationContractError, SegmentationError, SegmentationFilesystemError,
    SegmentationInputError, SegmentationIntegrityError, SegmentationOutcome,
    SegmentationProcessingResult, SegmentationProposal, SegmentationProviderError,
    SegmentationRequest, SegmentationSegment, SegmentationValidationError, SegmentationValidationIssue,
)
from .segmentation_prompts import SEGMENTATION_PROMPT_VERSION
from .selection_models import SemanticCallMetadata
from .silver import (
    DEFAULT_SILVER_CONFIG, SilverConfig, SilverContractError, canonical_silver_path,
    decode_silver_artifact, validate_silver_artifact, _keys, _string, _timestamp, _count, _timing,
)
from .models import _require_video_id
from .transcript_models import CaptionKind
from ._deepseek_json import strict_json

SEGMENTATION_SCHEMA_VERSION = 1
SEGMENTATION_CONTRACT_VERSION = 1
_TRACK_FIELDS = ("language_code", "caption_kind", "source_track_id", "provider_name", "provider_version")


def canonical_segmentation_path(video_id: str, config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG) -> Path:
    return Path(config.root).expanduser().resolve() / "youtube" / canonical_silver_path(video_id).name


def validate_segmentation_proposal(proposal: SegmentationProposal, request: SegmentationRequest):
    """Return concrete structural issues, never semantic judgments about section quality."""
    try:
        if not isinstance(proposal, SegmentationProposal):
            raise ValueError("Expected boundary-only proposal")
        proposal.__post_init__()
        proposal.metadata.__post_init__()
        if proposal.metadata.prompt_version != SEGMENTATION_PROMPT_VERSION:
            raise ValueError("Unsupported semantic contract")
        for span in proposal.sections:
            span.__post_init__()
    except (ValueError, TypeError, AttributeError):
        return (SegmentationValidationIssue("SCHEMA"),)
    if not proposal.sections:
        return (SegmentationValidationIssue("EMPTY_PROPOSAL"),)
    positions = {s.source_index: i for i, s in enumerate(request.segments)}
    cursor = 0
    for number, section in enumerate(proposal.sections):
        first, last = positions.get(section.first_source_index), positions.get(section.last_source_index)
        if first is None or last is None:
            return (SegmentationValidationIssue("UNKNOWN_SEGMENT", number),)
        if last < first:
            return (SegmentationValidationIssue("INVALID_RANGE", number),)
        if first != cursor:
            expected = request.segments[cursor].source_index if cursor < len(request.segments) else None
            return (SegmentationValidationIssue("COVERAGE_GAP" if first > cursor else "OVERLAP_OR_ORDER",
                                                number, expected, section.first_source_index),)
        cursor = last + 1
    if cursor != len(request.segments):
        return (SegmentationValidationIssue("INCOMPLETE_COVERAGE", len(proposal.sections),
                                            request.segments[cursor].source_index),)
    return ()


def _request(silver, feedback=(), output_limit=8192):
    return SegmentationRequest(silver["transcript"]["language_code"], tuple(
        SegmentationSegment(s["source_index"], s["text"]) for s in silver["transcript"]["segments"]
    ), feedback, output_limit)


def _exact_end(start, duration):
    left, right = Decimal(start), Decimal(duration)
    if not left:
        return right
    if not right:
        return left
    with localcontext() as context:
        context.prec = max(left.adjusted(), right.adjusted()) - min(left.as_tuple().exponent, right.as_tuple().exponent) + 2
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        return left + right


def _interval(members):
    return (str(min(Decimal(m["start_seconds"]) for m in members)),
            str(max(_exact_end(m["start_seconds"], m["duration_seconds"]) for m in members)))


def _metadata(value):
    _keys(value, "provider requested_model reported_model prompt_version")
    metadata = SemanticCallMetadata(**value)
    if metadata.prompt_version != SEGMENTATION_PROMPT_VERSION:
        raise ValueError("Unsupported prompt contract")
    return metadata


def _attempt(value):
    _keys(value, "number failure_kind validation_codes metadata")
    if type(value["validation_codes"]) is not list:
        raise ValueError("Invalid validation history")
    return SegmentationAttempt(value["number"], value["failure_kind"], tuple(value["validation_codes"]),
                               None if value["metadata"] is None else _metadata(value["metadata"]))


def validate_segmentation_artifact(artifact: dict) -> dict:
    """Standalone schema/structure validation; source text fidelity also requires Silver comparison."""
    try:
        _keys(artifact, "schema_version segmentation_contract_version outcome source silver bronze acquisition semantic attempts sections")
        for name, expected in (("schema_version", SEGMENTATION_SCHEMA_VERSION),
                               ("segmentation_contract_version", SEGMENTATION_CONTRACT_VERSION)):
            if type(artifact[name]) is not int or artifact[name] != expected:
                raise ValueError("Unsupported segmentation version")
        outcome = SegmentationOutcome(artifact["outcome"])
        source = _keys(artifact["source"], "type video_id url title channel_title published_at_utc duration_seconds view_count")
        video_id = source["video_id"]
        _require_video_id(video_id)
        if source["type"] != "youtube" or source["url"] != f"https://www.youtube.com/watch?v={video_id}":
            raise ValueError("Invalid canonical identity")
        for name in ("title", "channel_title"):
            _string(source[name], nullable=True)
        _timestamp(source["published_at_utc"], nullable=True)
        for name in ("duration_seconds", "view_count"):
            _count(source[name], nullable=True)
        silver = _keys(artifact["silver"], "schema_version cleaning_rule_version relative_path sha256 segment_count language_code caption_kind source_track_id provider_name provider_version")
        bronze = _keys(artifact["bronze"], "schema_version relative_path sha256")
        for reference in (silver, bronze):
            if (type(reference["schema_version"]) is not int or reference["schema_version"] != 1
                    or reference["relative_path"] != f"youtube/{canonical_silver_path(video_id).name}"
                    or not isinstance(reference["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is None):
                raise ValueError("Invalid upstream reference")
        if type(silver["cleaning_rule_version"]) is not int or silver["cleaning_rule_version"] != 1:
            raise ValueError("Unsupported cleaning version")
        _count(silver["segment_count"])
        if silver["segment_count"] == 0:
            raise ValueError("Invalid Silver count")
        for name in ("language_code", "provider_name", "provider_version"):
            _string(silver[name], nonempty=True)
        CaptionKind(silver["caption_kind"])
        _string(silver["source_track_id"], nullable=True, nonempty=True)
        acquisition = _keys(artifact["acquisition"], "target_id target_key target_name run_id selected_at_utc bronze_created_at_utc")
        _string(acquisition["target_id"], nonempty=True)
        for name in ("target_key", "target_name"):
            _string(acquisition[name], nullable=True, nonempty=True)
        run_id = acquisition["run_id"]
        if run_id is not None and not (type(run_id) is int or isinstance(run_id, str) and run_id.strip()):
            raise ValueError("Invalid run identity")
        _timestamp(acquisition["selected_at_utc"], nullable=True)
        _timestamp(acquisition["bronze_created_at_utc"])
        if type(artifact["sections"]) is not list or type(artifact["attempts"]) is not list:
            raise ValueError("Invalid section/attempt arrays")
        if outcome is SegmentationOutcome.NO_SEGMENTABLE_CONTENT:
            if artifact["sections"] or artifact["attempts"] or artifact["semantic"] is not None:
                raise ValueError("Empty outcome cannot invent sections or calls")
            return artifact
        semantic = _metadata(artifact["semantic"])
        attempts = tuple(_attempt(a) for a in artifact["attempts"])
        if (not 1 <= len(attempts) <= 3 or tuple(a.number for a in attempts) != tuple(range(1, len(attempts) + 1))
                or any(a.failure_kind is None for a in attempts[:-1])
                or attempts[-1].failure_kind is not None or attempts[-1].metadata != semantic):
            raise ValueError("Invalid bounded attempt provenance")
        if not artifact["sections"]:
            raise ValueError("Segmented outcome requires sections")
        cursor, previous_index = 0, -1
        for number, section in enumerate(artifact["sections"]):
            _keys(section, "section_id kind start_position end_position_exclusive members start_seconds end_seconds text")
            if section["section_id"] != f"section-{number + 1:06d}":
                raise ValueError("Invalid section identity/order")
            SectionKind(section["kind"])
            _count(section["start_position"])
            _count(section["end_position_exclusive"])
            members = section["members"]
            if (type(members) is not list or not members or section["start_position"] != cursor
                    or section["end_position_exclusive"] != cursor + len(members)):
                raise ValueError("Invalid contiguous coverage")
            for member in members:
                _keys(member, "source_index start_seconds duration_seconds")
                _count(member["source_index"])
                if member["source_index"] <= previous_index:
                    raise ValueError("Invalid member order/uniqueness")
                previous_index = member["source_index"]
                _timing(member["start_seconds"])
                _timing(member["duration_seconds"])
            start, end = _interval(members)
            if section["start_seconds"] != start or section["end_seconds"] != end:
                raise ValueError("Invalid section timing")
            _string(section["text"])
            cursor = section["end_position_exclusive"]
        if cursor != silver["segment_count"]:
            raise ValueError("Incomplete coverage")
        return artifact
    except (ValueError, TypeError, KeyError, AttributeError, DecimalException, OverflowError, RecursionError):
        raise SegmentationContractError(None, "artifact_validation") from None


def construct_segmentation_artifact(silver, *, silver_sha256, proposal=None, attempts=()):
    try:
        validate_silver_artifact(silver)
    except SilverContractError:
        raise SegmentationInputError(None, "silver_validation") from None
    segmentable = any(s["text"] != "" for s in silver["transcript"]["segments"])
    if segmentable:
        if validate_segmentation_proposal(proposal, _request(silver)):
            raise SegmentationContractError(silver["source"]["video_id"], "proposal_validation")
    elif proposal is not None or attempts:
        raise SegmentationContractError(silver["source"]["video_id"], "empty_outcome")
    sections, cursor = [], 0
    positions = {s["source_index"]: i for i, s in enumerate(silver["transcript"]["segments"])}
    for span in proposal.sections if proposal is not None else ():
        end_position = positions[span.last_source_index] + 1
        originals = silver["transcript"]["segments"][cursor:end_position]
        members = [{name: s[name] for name in ("source_index", "start_seconds", "duration_seconds")} for s in originals]
        start, end = _interval(members)
        sections.append({"section_id": f"section-{len(sections) + 1:06d}", "kind": span.kind.value,
                         "start_position": cursor, "end_position_exclusive": end_position,
                         "members": members, "start_seconds": start, "end_seconds": end,
                         "text": " ".join(s["text"] for s in originals if s["text"] != "")})
        cursor = end_position
    artifact = {
        "schema_version": SEGMENTATION_SCHEMA_VERSION, "segmentation_contract_version": SEGMENTATION_CONTRACT_VERSION,
        "outcome": (SegmentationOutcome.SEGMENTED if segmentable else SegmentationOutcome.NO_SEGMENTABLE_CONTENT).value,
        "source": deepcopy(silver["source"]),
        "silver": {"schema_version": silver["schema_version"], "cleaning_rule_version": silver["cleaning"]["rule_version"],
                   "relative_path": f"youtube/{canonical_silver_path(silver['source']['video_id']).name}",
                   "sha256": silver_sha256, "segment_count": len(silver["transcript"]["segments"]),
                   **{name: silver["transcript"][name] for name in _TRACK_FIELDS}},
        "bronze": deepcopy(silver["bronze"]), "acquisition": deepcopy(silver["acquisition"]),
        "semantic": asdict(proposal.metadata) if proposal is not None else None,
        "attempts": [{**asdict(a), "validation_codes": list(a.validation_codes)} for a in attempts], "sections": sections,
    }
    return validate_segmentation_artifact(artifact)


def validate_segmentation_against_silver(artifact, silver_artifact, *, silver_sha256):
    validate_segmentation_artifact(artifact)
    proposal = None
    if artifact["outcome"] == SegmentationOutcome.SEGMENTED.value:
        proposal = SegmentationProposal(tuple(SectionSpan(s["members"][0]["source_index"],
            s["members"][-1]["source_index"], SectionKind(s["kind"])) for s in artifact["sections"]), _metadata(artifact["semantic"]))
    try:
        expected = construct_segmentation_artifact(silver_artifact, silver_sha256=silver_sha256,
            proposal=proposal, attempts=tuple(_attempt(a) for a in artifact["attempts"]))
    except SegmentationContractError:
        raise SegmentationIntegrityError(silver_artifact["source"]["video_id"], "silver_provenance_or_content") from None
    if artifact != expected:
        raise SegmentationIntegrityError(silver_artifact["source"]["video_id"], "silver_provenance_or_content")
    return artifact


def decode_segmentation_artifact(content: bytes):
    try:
        artifact = strict_json(content.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise SegmentationContractError(None, "decoding") from None
    return validate_segmentation_artifact(artifact)


def content_sections(validated_artifact):
    """Mechanical extraction view; never mutates or deletes persisted advertisements."""
    validate_segmentation_artifact(validated_artifact)
    return tuple(deepcopy(s) for s in validated_artifact["sections"] if s["kind"] == SectionKind.CONTENT.value)


def _read_input(video_id, silver_config):
    path = canonical_silver_path(video_id, silver_config)
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path(silver_config.root).expanduser().resolve()):
        raise SegmentationIntegrityError(video_id, "canonical_silver_path")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise SegmentationInputError(video_id, "silver_missing") from None
    try:
        artifact = decode_silver_artifact(content)
    except SilverContractError:
        raise SegmentationInputError(video_id, "silver_validation") from None
    if artifact["source"]["video_id"] != video_id:
        raise SegmentationIntegrityError(video_id, "silver_identity")
    return artifact, sha256(content).hexdigest()


def _read_canonical(path, silver, digest):
    video_id = silver["source"]["video_id"]
    if path.is_symlink() or not path.is_file() or not path.parent.resolve().is_relative_to(path.parent.parent):
        raise SegmentationIntegrityError(video_id, "canonical_result_path")
    return validate_segmentation_against_silver(decode_segmentation_artifact(path.read_bytes()), silver, silver_sha256=digest)


def load_segmentation_artifact(video_id, *, silver_config=DEFAULT_SILVER_CONFIG, config=DEFAULT_SEGMENTATION_CONFIG):
    """Read-only loading validates the exact current Silver snapshot before downstream use."""
    _require_video_id(video_id)
    try:
        silver, digest = _read_input(video_id, silver_config)
        return _read_canonical(canonical_segmentation_path(video_id, config), silver, digest)
    except OSError:
        raise SegmentationFilesystemError(video_id, "loading") from None


def _run_semantic(video_id, silver, capability, config):
    if not callable(getattr(capability, "propose_sections", None)):
        raise SegmentationProviderError(video_id, "CONFIGURATION")
    limit = min(defaults.SEGMENTATION_MAX_OUTPUT_LIMIT, max(8192, 64 * len(silver["transcript"]["segments"])))
    feedback, attempts = (), []
    for number in range(1, config.max_attempts + 1):
        request = _request(silver, feedback, limit)
        metadata, issues = None, ()
        try:
            proposal = capability.propose_sections(request)
            issues = validate_segmentation_proposal(proposal, request)
            if issues:
                metadata = proposal.metadata if isinstance(proposal, SegmentationProposal) else None
                raise SegmentationCapabilityError("INVALID_OUTPUT", retryable=True, metadata=metadata)
        except SegmentationCapabilityError as error:
            kind, retryable, metadata = error.kind, error.retryable, error.metadata
            if kind == "INVALID_OUTPUT":
                feedback = issues or (SegmentationValidationIssue("SCHEMA"),)
        except (ValueError, TypeError, AttributeError):
            kind, retryable = "INVALID_OUTPUT", True
            feedback = (SegmentationValidationIssue("SCHEMA"),)
        except Exception:
            kind, retryable = "PROVIDER_CONTRACT", False
        else:
            attempts.append(SegmentationAttempt(number, metadata=proposal.metadata))
            return proposal, tuple(attempts)
        attempts.append(SegmentationAttempt(number, kind, tuple(i.code for i in feedback) if kind == "INVALID_OUTPUT" else (), metadata))
        if not retryable or number == config.max_attempts:
            error_type = SegmentationValidationError if kind in ("INVALID_OUTPUT", "OUTPUT_TRUNCATED") else SegmentationProviderError
            raise error_type(video_id, kind, attempts=attempts) from None
        if kind == "OUTPUT_TRUNCATED":
            limit = min(defaults.SEGMENTATION_MAX_OUTPUT_LIMIT, limit * 2)
        time.sleep(config.retry_delays_seconds[number - 1])


def segment_silver_source(video_id, *, capability: SegmentationCapability | None = None,
                          silver_config: SilverConfig = DEFAULT_SILVER_CONFIG,
                          bronze_config: BronzeConfig = DEFAULT_BRONZE_CONFIG,
                          config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG):
    """Explicit caller-serialized operation; at most three capability invocations, no outer retry loop."""
    _require_video_id(video_id)
    if not isinstance(config, SegmentationConfig):
        raise ValueError("Expected local segmentation configuration")
    config.__post_init__()
    temporary, created, phase = None, False, "configuration"
    try:
        root = Path(config.root).expanduser().resolve()
        for upstream in (silver_config.root, bronze_config.root):
            protected = Path(upstream).expanduser().resolve()
            if root.is_relative_to(protected) or protected.is_relative_to(root):
                raise SegmentationIntegrityError(video_id, "overlapping_roots")
        path = canonical_segmentation_path(video_id, config)
        if not path.parent.resolve().is_relative_to(root):
            raise SegmentationIntegrityError(video_id, "canonical_result_path")
        phase = "silver_read"
        silver, digest = _read_input(video_id, silver_config)
        encoded = video_id.encode("ascii").hex()
        phase = "existing_result"
        if path.exists() or path.is_symlink():
            artifact = _read_canonical(path, silver, digest)
            filesystem.cleanup_temporary_files(path.parent, encoded, namespace="segmentation")
        else:
            proposal, attempts = None, ()
            if any(s["text"] != "" for s in silver["transcript"]["segments"]):
                proposal, attempts = _run_semantic(video_id, silver, capability, config)
            phase = "construction"
            artifact = construct_segmentation_artifact(silver, silver_sha256=digest, proposal=proposal, attempts=attempts)
            try:
                content = (json.dumps(artifact, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise SegmentationContractError(video_id, "serialization") from None
            phase = "directory_setup"
            filesystem.prepare_directory(path.parent)
            filesystem.cleanup_temporary_files(path.parent, encoded, namespace="segmentation")
            phase = "temporary_write"
            temporary = filesystem.stage(path.parent, encoded, content, namespace="segmentation")
            phase = "temporary_validation"
            staged = validate_segmentation_against_silver(decode_segmentation_artifact(temporary.read_bytes()), silver, silver_sha256=digest)
            if staged != artifact:
                raise SegmentationIntegrityError(video_id, phase)
            phase = "publication"
            try:
                filesystem.publish(temporary, path)
                created = True
            except FileExistsError:
                pass  # The first valid durable result wins, even if its valid boundaries differ.
        phase = "final_validation"
        artifact = _read_canonical(path, silver, digest)
        phase = "durability_confirmation"
        filesystem.confirm_durable(path)
        return SegmentationProcessingResult(video_id, path, SegmentationOutcome(artifact["outcome"]), created, artifact)
    except SegmentationError:
        raise
    except (OSError, DecimalException, OverflowError):
        raise SegmentationFilesystemError(video_id, phase) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
