"""Versioned source artifacts, exact upstream compatibility and atomic first-winner publication."""

from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
import json
import re

from . import _bronze_filesystem as filesystem
from ._deepseek_json import strict_json
from .extraction_models import ArtifactReference, EvidenceSpan, ExtractionProvenance, KnowledgeEvidence
from .models import _require_video_id
from .section_validation_models import DEFAULT_SECTION_VALIDATION_CONFIG, GateVerdict
from .selection_models import SemanticCallMetadata
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG
from .silver import DEFAULT_SILVER_CONFIG, _keys, _timestamp
from .source_audit import validate_source_candidate, validate_source_evaluation
from .source_audit_input import load_source_audit_input, read_source_file, resolved_source_path, validate_source_path, verify_source_snapshot
from .source_audit_models import (
    DEFAULT_SOURCE_AUDIT_CONFIG, SectionCheckpointReference, SectionItemReference,
    SourceAuditDecision, SourceAuditGate, SourceAuditIntegrityError, SourceAuditIssue,
    SourceAuditRound, SourceConflict, SourceGateEvaluation, SourceIssueKind, SourceKnowledgeArtifact,
    SourceKnowledgeCandidate, SourceKnowledgeContractError, SourceKnowledgeFilesystemError,
    SourceKnowledgeItem, SourceKnowledgeOutcome, SourceValidatorEvaluation,
)


def canonical_source_knowledge_path(video_id, config=DEFAULT_SOURCE_AUDIT_CONFIG):
    _require_video_id(video_id)
    config.__post_init__()
    return resolved_source_path(config.root) / "youtube" / f"id-{video_id.encode('ascii').hex()}.json"


def candidate_document(candidate):
    return {"outcome": candidate.outcome.value, "items": [asdict(i) for i in candidate.items],
            "conflicts": [asdict(c) for c in candidate.conflicts],
            "audit_metadata": None if candidate.audit_metadata is None else asdict(candidate.audit_metadata)}


def candidate_sha256(candidate):
    return sha256(json.dumps(candidate_document(candidate), sort_keys=True, ensure_ascii=True,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def evaluation_document(evaluation):
    return {"gates": {g.gate.value: {"verdict": g.verdict.value, "issues": [asdict(i) for i in g.issues]}
                      for g in evaluation.gates}}


def _array(value):
    if type(value) is not list:
        raise ValueError("Expected JSON array")
    return value


def _metadata(value, *, optional=False):
    if optional and value is None:
        return None
    return SemanticCallMetadata(**_keys(value, "provider requested_model reported_model prompt_version"))


def _reference(value):
    return SectionItemReference(**_keys(value, "section_id item_id"))


def _evidence(value):
    _keys(value, "section_id spans")
    return KnowledgeEvidence(value["section_id"], tuple(EvidenceSpan(**_keys(s, "first_source_index last_source_index"))
                                                       for s in _array(value["spans"])))


def decode_source_evaluation(document, metadata):
    _keys(document, "gates")
    _keys(document["gates"], " ".join(g.value for g in SourceAuditGate))
    gates = []
    for gate in SourceAuditGate:
        value = _keys(document["gates"][gate.value], "verdict issues")
        issues = []
        for issue in _array(value["issues"]):
            _keys(issue, "kind reason affected_source_item_ids affected_section_item_refs relevant_evidence actionable_feedback")
            issues.append(SourceAuditIssue(SourceIssueKind(issue["kind"]), issue["reason"],
                tuple(_array(issue["affected_source_item_ids"])),
                tuple(_reference(v) for v in _array(issue["affected_section_item_refs"])),
                tuple(_evidence(v) for v in _array(issue["relevant_evidence"])), issue["actionable_feedback"]))
        gates.append(SourceGateEvaluation(gate, GateVerdict(value["verdict"]), tuple(issues)))
    return SourceValidatorEvaluation(tuple(gates), metadata)


def decode_candidate(document):
    _keys(document, "outcome items conflicts audit_metadata")
    items, conflicts = [], []
    for item in _array(document["items"]):
        _keys(item, "item_id statement evidence derived_from")
        items.append(SourceKnowledgeItem(item["item_id"], item["statement"],
            tuple(_evidence(e) for e in _array(item["evidence"])),
            tuple(_reference(r) for r in _array(item["derived_from"]))))
    for conflict in _array(document["conflicts"]):
        _keys(conflict, "conflict_id item_ids reason")
        conflicts.append(SourceConflict(conflict["conflict_id"], tuple(_array(conflict["item_ids"])), conflict["reason"]))
    return SourceKnowledgeCandidate(SourceKnowledgeOutcome(document["outcome"]), tuple(items), tuple(conflicts),
                                     _metadata(document["audit_metadata"], optional=True))


def validate_source_artifact(artifact, inputs):
    try:
        if not isinstance(artifact, SourceKnowledgeArtifact):
            raise ValueError("Expected source artifact")
        if any(type(v) is not int or v != 1 for v in (artifact.schema_version, artifact.source_audit_contract_version)):
            raise ValueError("Unsupported source artifact contract")
        _timestamp(artifact.created_at_utc)
        if (artifact.provenance != inputs.provenance or artifact.validated_sections != inputs.validated_sections
                or artifact.upstream_segmentation_outcome != inputs.upstream_segmentation_outcome):
            raise ValueError("Upstream snapshot mismatch")
        validate_source_candidate(artifact.candidate, inputs)
        if not isinstance(artifact.rounds, tuple) or not 1 <= len(artifact.rounds) <= 3:
            raise ValueError("Invalid source round budget")
        for number, record in enumerate(artifact.rounds, 1):
            if (not isinstance(record, SourceAuditRound) or type(record.number) is not int or record.number != number
                    or type(record.item_count) is not int or not 0 <= record.item_count <= len(inputs.items)
                    or bool(record.item_count) != bool(inputs.items)
                    or type(record.conflict_count) is not int or record.conflict_count < 0
                    or record.item_count < 2 and record.conflict_count
                    or not isinstance(record.candidate_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", record.candidate_sha256) is None
                    or (record.audit_metadata is None) != (not inputs.items)):
                raise ValueError("Invalid completed audit round")
            if record.audit_metadata is not None:
                record.audit_metadata.__post_init__()
            evaluation = validate_source_evaluation(record.evaluation, inputs, item_count=record.item_count)
            if any(i.kind is SourceIssueKind.UPSTREAM_COVERAGE_DEFECT for g in evaluation.gates for i in g.issues):
                raise ValueError("Upstream defect cannot produce successful source artifact")
            expected = SourceAuditDecision.PASS if number == len(artifact.rounds) else SourceAuditDecision.REJECT
            if evaluation.decision is not expected:
                raise ValueError("Only final round may PASS")
        final = artifact.rounds[-1]
        if (final.candidate_sha256 != candidate_sha256(artifact.candidate)
                or final.item_count != len(artifact.candidate.items) or final.conflict_count != len(artifact.candidate.conflicts)
                or final.audit_metadata != artifact.candidate.audit_metadata):
            raise ValueError("Final candidate disagrees with accepted round")
        # Recorded model/prompt versions are metadata, not compatibility gates.
        return artifact
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RuntimeError):
        raise SourceAuditIntegrityError("SOURCE_ARTIFACT_COMPATIBILITY", phase="ARTIFACT") from None


def source_artifact_document(artifact):
    candidate = candidate_document(artifact.candidate)
    return {"schema_version": artifact.schema_version, "source_audit_contract_version": artifact.source_audit_contract_version,
            "created_at_utc": artifact.created_at_utc,
            "source": {"type": "youtube", "video_id": artifact.provenance.video_id},
            "upstream_segmentation_outcome": artifact.upstream_segmentation_outcome,
            "provenance": asdict(artifact.provenance), "validated_sections": [asdict(r) for r in artifact.validated_sections],
            **candidate, "rounds_used": artifact.rounds_used,
            "rounds": [{"number": r.number, "candidate_sha256": r.candidate_sha256, "item_count": r.item_count,
                "conflict_count": r.conflict_count, "generation": "DETERMINISTIC_EMPTY" if r.audit_metadata is None else "MODEL",
                "audit_metadata": None if r.audit_metadata is None else asdict(r.audit_metadata),
                "validator_metadata": asdict(r.evaluation.metadata), **evaluation_document(r.evaluation),
                "decision": r.evaluation.decision.value} for r in artifact.rounds]}


def decode_source_artifact(content, inputs):
    try:
        d = strict_json(content.decode("utf-8"))
        _keys(d, "schema_version source_audit_contract_version created_at_utc source upstream_segmentation_outcome provenance validated_sections outcome items conflicts audit_metadata rounds_used rounds")
        _keys(d["source"], "type video_id")
        if d["source"] != {"type": "youtube", "video_id": inputs.provenance.video_id}:
            raise ValueError("Source mismatch")
        p = _keys(d["provenance"], "video_id segmentation silver bronze segmentation_contract_version cleaning_rule_version")
        provenance = ExtractionProvenance(p["video_id"], *(ArtifactReference(**_keys(p[n], "relative_path sha256 schema_version"))
            for n in ("segmentation", "silver", "bronze")), p["segmentation_contract_version"], p["cleaning_rule_version"])
        references = tuple(SectionCheckpointReference(**_keys(r, "section_id relative_path sha256 schema_version validation_contract_version"))
                           for r in _array(d["validated_sections"]))
        candidate = decode_candidate({k: d[k] for k in ("outcome", "items", "conflicts", "audit_metadata")})
        rounds = []
        for r in _array(d["rounds"]):
            _keys(r, "number candidate_sha256 item_count conflict_count generation audit_metadata validator_metadata gates decision")
            audit_metadata = _metadata(r["audit_metadata"], optional=True)
            if r["generation"] != ("DETERMINISTIC_EMPTY" if audit_metadata is None else "MODEL"):
                raise ValueError("Invalid generation record")
            evaluation = decode_source_evaluation({"gates": r["gates"]}, _metadata(r["validator_metadata"]))
            if r["decision"] != evaluation.decision.value:
                raise ValueError("Decision contradicts gates")
            rounds.append(SourceAuditRound(r["number"], r["candidate_sha256"], r["item_count"], r["conflict_count"], audit_metadata, evaluation))
        artifact = SourceKnowledgeArtifact(d["schema_version"], d["source_audit_contract_version"], d["created_at_utc"],
            provenance, d["upstream_segmentation_outcome"], references, candidate, tuple(rounds))
        if type(d["rounds_used"]) is not int or d["rounds_used"] != artifact.rounds_used:
            raise ValueError("Round count mismatch")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, ArithmeticError, RecursionError):
        raise SourceKnowledgeContractError("SOURCE_ARTIFACT_SCHEMA", phase="ARTIFACT") from None
    return validate_source_artifact(artifact, inputs)


def read_source_knowledge(path, inputs, config):
    try:
        return decode_source_artifact(read_source_file(path, config.root), inputs)
    except OSError:
        raise SourceKnowledgeFilesystemError("ARTIFACT_READ", phase="ARTIFACT") from None


def load_source_knowledge(video_id, *, silver_config=DEFAULT_SILVER_CONFIG,
                          segmentation_config=DEFAULT_SEGMENTATION_CONFIG,
                          section_validation_config=DEFAULT_SECTION_VALIDATION_CONFIG,
                          config=DEFAULT_SOURCE_AUDIT_CONFIG):
    inputs = load_source_audit_input(video_id, silver_config=silver_config, segmentation_config=segmentation_config,
                                     section_validation_config=section_validation_config)
    artifact = read_source_knowledge(canonical_source_knowledge_path(video_id, config), inputs, config)
    verify_source_snapshot(inputs)
    return artifact


def publish_source_knowledge(artifact, inputs, config):
    path = canonical_source_knowledge_path(inputs.provenance.video_id, config)
    temporary, created = None, False
    try:
        validate_source_artifact(artifact, inputs)
        verify_source_snapshot(inputs)
        validate_source_path(path, config.root, phase="PERSISTENCE")
        content = (json.dumps(source_artifact_document(artifact), sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        filesystem.prepare_directory(path.parent)
        temporary = filesystem.stage(path.parent, path.stem, content, namespace="source-knowledge")
        if decode_source_artifact(temporary.read_bytes(), inputs) != artifact:
            raise SourceAuditIntegrityError("STAGED_ARTIFACT", phase="PERSISTENCE")
        verify_source_snapshot(inputs)
        try:
            filesystem.publish(temporary, path)
            created = True
        except FileExistsError:
            pass
        winner = read_source_knowledge(path, inputs, config)
        filesystem.confirm_durable(path)
        verify_source_snapshot(inputs)
        return path, created, winner
    except (OSError, ValueError, TypeError, UnicodeError, ArithmeticError, RecursionError):
        raise SourceKnowledgeFilesystemError("SOURCE_ARTIFACT_PUBLICATION", phase="PERSISTENCE") from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
