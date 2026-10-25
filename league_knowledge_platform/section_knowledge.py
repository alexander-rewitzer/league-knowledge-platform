"""Strict durable accepted-section checkpoints; no provider calls or upstream writes."""

from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

from . import _bronze_filesystem as filesystem
from ._deepseek_json import strict_json
from .extraction import load_extraction_section, validate_extraction_proposal, _validate_spans
from .extraction_models import (
    ArtifactReference, EvidenceSpan, ExtractionOutcome, ExtractionProposal, ExtractionProvenance,
    KnowledgeEvidence, KnowledgeItem, _section_id,
)
from .extraction_prompts import EXTRACTION_PROMPT_VERSION, EXTRACTION_REFINEMENT_PROMPT_VERSION
from .models import _require_video_id
from .section_validation_models import (
    DEFAULT_SECTION_VALIDATION_CONFIG, SectionDecision, SectionJudgeResult,
    SemanticRoundRecord, ValidatedSectionKnowledge, SectionKnowledgeContractError,
    SectionKnowledgeFilesystemError, SectionValidationIntegrityError, decode_judge_document,
)
from .section_validation_prompts import SECTION_JUDGE_PROMPT_VERSION
from .selection_models import SemanticCallMetadata
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG
from .silver import DEFAULT_SILVER_CONFIG, _keys, _timestamp

SUPPORTED_EXTRACTION_PROMPTS = (EXTRACTION_PROMPT_VERSION, EXTRACTION_REFINEMENT_PROMPT_VERSION)


def proposal_sha256(proposal):
    return sha256(json.dumps(asdict(proposal), sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_judge_references(evaluation, section, item_ids=None, *, item_count=None):
    evaluation.__post_init__()
    if evaluation.metadata.prompt_version != SECTION_JUDGE_PROMPT_VERSION:
        raise ValueError("Unsupported Judge prompt")
    for gate in evaluation.gates:
        for issue in gate.issues:
            if any((identity not in item_ids if item_ids is not None else
                    not _historical_item_id(identity, item_count))
                   for identity in issue.affected_item_ids):
                raise ValueError("Judge referenced another proposal")
            if issue.relevant_source_spans:
                _validate_spans(issue.relevant_source_spans, section)


def _historical_item_id(identity, item_count):
    number = int(identity.removeprefix("item-"))
    return 1 <= number <= item_count and identity == f"item-{number:06d}"


def validate_section_checkpoint(checkpoint, section):
    try:
        if not isinstance(checkpoint, ValidatedSectionKnowledge):
            raise ValueError("Expected typed section checkpoint")
        checkpoint.__post_init__()
        _timestamp(checkpoint.created_at_utc)
        if checkpoint.section_id != section.section_id or checkpoint.provenance != section.provenance:
            raise ValueError("Checkpoint belongs to different input")
        validate_extraction_proposal(checkpoint.accepted_proposal, section, supported_prompt_versions=SUPPORTED_EXTRACTION_PROMPTS)
        for number, record in enumerate(checkpoint.rounds, 1):
            if record.number != number or record.extraction_metadata.prompt_version not in SUPPORTED_EXTRACTION_PROMPTS:
                raise ValueError("Invalid round sequence or extraction contract")
            validate_judge_references(record.evaluation, section, item_count=record.proposal_item_count)
            expected = SectionDecision.PASS if number == len(checkpoint.rounds) else SectionDecision.REJECT
            if SectionJudgeResult(record.evaluation).decision is not expected:
                raise ValueError("Only final PASS may be published")
        last = checkpoint.rounds[-1]
        accepted = checkpoint.accepted_proposal
        if (last.proposal_sha256 != proposal_sha256(accepted) or last.proposal_item_count != len(accepted.items)
                or last.proposal_outcome is not accepted.outcome or last.extraction_metadata != accepted.metadata):
            raise ValueError("Accepted proposal and final round disagree")
        return checkpoint
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RuntimeError):
        raise SectionValidationIntegrityError("CHECKPOINT_COMPATIBILITY", phase="CHECKPOINT") from None


def canonical_section_knowledge_path(video_id, section_id, config=DEFAULT_SECTION_VALIDATION_CONFIG):
    _require_video_id(video_id)
    _section_id(section_id)
    config.__post_init__()
    return Path(config.root).expanduser().resolve() / "youtube" / f"id-{video_id.encode('ascii').hex()}" / f"{section_id}.json"


def _metadata(value):
    _keys(value, "provider requested_model reported_model prompt_version")
    return SemanticCallMetadata(**value)


def _gates_document(evaluation):
    return {gate.gate.value: {"verdict": gate.verdict.value, "issues": [asdict(i) for i in gate.issues]}
            for gate in evaluation.gates}


def checkpoint_document(checkpoint):
    accepted = checkpoint.accepted_proposal
    return {"schema_version": checkpoint.schema_version,
            "validation_contract_version": checkpoint.validation_contract_version,
            "created_at_utc": checkpoint.created_at_utc,
            "source": {"type": "youtube", "video_id": checkpoint.provenance.video_id},
            "section_id": checkpoint.section_id, "provenance": asdict(checkpoint.provenance),
            "accepted_proposal": {"extraction_contract_version": accepted.contract_version,
                "outcome": accepted.outcome.value, "items": [asdict(i) for i in accepted.items],
                "extraction_metadata": asdict(accepted.metadata)},
            "semantic_rounds_used": checkpoint.semantic_rounds_used,
            "rounds": [{"number": r.number, "proposal_outcome": r.proposal_outcome.value,
                "proposal_item_count": r.proposal_item_count, "proposal_sha256": r.proposal_sha256,
                "extraction_metadata": asdict(r.extraction_metadata), "judge_metadata": asdict(r.evaluation.metadata),
                "gates": _gates_document(r.evaluation), "decision": SectionJudgeResult(r.evaluation).decision.value}
                for r in checkpoint.rounds]}


def decode_section_checkpoint(content, section):
    try:
        d = strict_json(content.decode("utf-8"))
        _keys(d, "schema_version validation_contract_version created_at_utc source section_id provenance accepted_proposal semantic_rounds_used rounds")
        _keys(d["source"], "type video_id")
        if d["source"] != {"type": "youtube", "video_id": section.provenance.video_id}:
            raise ValueError("Source identity mismatch")
        p = _keys(d["provenance"], "video_id segmentation silver bronze segmentation_contract_version cleaning_rule_version")
        for name in ("segmentation", "silver", "bronze"):
            _keys(p[name], "relative_path sha256 schema_version")
        provenance = ExtractionProvenance(p["video_id"], *(ArtifactReference(**p[n]) for n in ("segmentation", "silver", "bronze")),
                                          p["segmentation_contract_version"], p["cleaning_rule_version"])
        a = _keys(d["accepted_proposal"], "extraction_contract_version outcome items extraction_metadata")
        if type(a["items"]) is not list or type(d["rounds"]) is not list:
            raise ValueError("Expected item and round arrays")
        items = []
        for item in a["items"]:
            _keys(item, "item_id statement evidence")
            if type(item["evidence"]) is not list:
                raise ValueError("Expected evidence array")
            evidence = []
            for e in item["evidence"]:
                _keys(e, "section_id spans")
                if type(e["spans"]) is not list:
                    raise ValueError("Expected spans array")
                evidence.append(KnowledgeEvidence(e["section_id"], tuple(EvidenceSpan(**_keys(s, "first_source_index last_source_index")) for s in e["spans"])))
            items.append(KnowledgeItem(item["item_id"], item["statement"], tuple(evidence)))
        proposal = ExtractionProposal(a["extraction_contract_version"], d["section_id"], provenance,
                                      ExtractionOutcome(a["outcome"]), tuple(items), _metadata(a["extraction_metadata"]))
        rounds = []
        for r in d["rounds"]:
            _keys(r, "number proposal_outcome proposal_item_count proposal_sha256 extraction_metadata judge_metadata gates decision")
            evaluation = decode_judge_document({"gates": r["gates"]}, _metadata(r["judge_metadata"]))
            if r["decision"] != SectionJudgeResult(evaluation).decision.value:
                raise ValueError("Recorded decision contradicts gates")
            rounds.append(SemanticRoundRecord(r["number"], ExtractionOutcome(r["proposal_outcome"]), r["proposal_item_count"],
                                              r["proposal_sha256"], _metadata(r["extraction_metadata"]), evaluation))
        result = ValidatedSectionKnowledge(d["schema_version"], d["validation_contract_version"], d["created_at_utc"],
                                           d["section_id"], provenance, proposal, tuple(rounds))
        if type(d["semantic_rounds_used"]) is not int or d["semantic_rounds_used"] != result.semantic_rounds_used:
            raise ValueError("Round count mismatch")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, ArithmeticError, RecursionError):
        raise SectionKnowledgeContractError("CHECKPOINT_SCHEMA", phase="CHECKPOINT") from None
    return validate_section_checkpoint(result, section)


def read_section_checkpoint(path, section, config):
    root = Path(config.root).expanduser().resolve()
    if path.is_symlink() or not path.parent.resolve().is_relative_to(root):
        raise SectionValidationIntegrityError("CHECKPOINT_PATH", phase="CHECKPOINT")
    try:
        return decode_section_checkpoint(path.read_bytes(), section)
    except OSError:
        raise SectionKnowledgeFilesystemError("CHECKPOINT_READ", phase="CHECKPOINT") from None


def load_validated_section_knowledge(video_id, section_id, *, silver_config=DEFAULT_SILVER_CONFIG,
                                     segmentation_config=DEFAULT_SEGMENTATION_CONFIG, config=DEFAULT_SECTION_VALIDATION_CONFIG):
    section = load_extraction_section(video_id, section_id, silver_config=silver_config, segmentation_config=segmentation_config)
    return read_section_checkpoint(canonical_section_knowledge_path(video_id, section_id, config), section, config)


def publish_section_checkpoint(checkpoint, section, config):
    path = canonical_section_knowledge_path(section.provenance.video_id, section.section_id, config)
    temporary, created = None, False
    try:
        validate_section_checkpoint(checkpoint, section)
        if path.is_symlink() or not path.parent.resolve().is_relative_to(Path(config.root).expanduser().resolve()):
            raise SectionValidationIntegrityError("CHECKPOINT_PATH", phase="PERSISTENCE")
        content = (json.dumps(checkpoint_document(checkpoint), ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        filesystem.prepare_directory(path.parent)
        temporary = filesystem.stage(path.parent, section.section_id, content, namespace="section-knowledge")
        staged = decode_section_checkpoint(temporary.read_bytes(), section)
        if staged != checkpoint:
            raise SectionValidationIntegrityError("STAGED_CHECKPOINT", phase="PERSISTENCE")
        try:
            filesystem.publish(temporary, path)
            created = True
        except FileExistsError:
            pass
        winner = read_section_checkpoint(path, section, config)
        filesystem.confirm_durable(path)
        return path, created, winner
    except (OSError, ValueError, TypeError, UnicodeError, ArithmeticError):
        raise SectionKnowledgeFilesystemError("CHECKPOINT_PUBLICATION", phase="PERSISTENCE", rounds=checkpoint.rounds) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
