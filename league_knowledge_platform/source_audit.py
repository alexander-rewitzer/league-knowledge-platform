"""Complete-candidate consolidation, seven-gate diagnosis and bounded refinement."""

from datetime import datetime, timezone

from .extraction import _validate_spans
from .extraction_models import KnowledgeEvidence
from .section_validation_models import DEFAULT_SECTION_VALIDATION_CONFIG
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG
from .silver import DEFAULT_SILVER_CONFIG
from .source_audit_input import load_source_audit_input, resolved_source_path, validate_source_path, verify_source_snapshot
from .source_audit_models import (
    DEFAULT_SOURCE_AUDIT_CONFIG, SourceAuditCapabilityError, SourceAuditConfig, SourceAuditDecision,
    SourceAuditError, SourceAuditExhaustedError, SourceAuditGate, SourceAuditInput, SourceAuditInputError,
    SourceAuditIntegrityError, SourceAuditOperationalError, SourceAuditRequest, SourceAuditResponse,
    SourceAuditRound, SourceAuditUpstreamCoverageError, SourceConflict, SourceIssueKind,
    SourceKnowledgeArtifact, SourceKnowledgeCandidate, SourceKnowledgeItem, SourceKnowledgeOutcome,
    SourceKnowledgeResult, SourceRefinementContext, SourceValidatorEvaluation, SourceValidatorRequest,
)


def contributing_evidence(contributors):
    """Stable union of exact existing spans, without expanding or coalescing them."""
    groups = {}
    for contributor in contributors:
        for evidence in contributor.knowledge.evidence:
            spans = groups.setdefault(evidence.section_id, [])
            for span in evidence.spans:
                if span not in spans:
                    spans.append(span)
    return tuple(KnowledgeEvidence(section, tuple(sorted(spans, key=lambda s: (s.first_source_index, s.last_source_index))))
                 for section, spans in groups.items())


def validate_source_candidate(candidate, inputs):
    if not isinstance(candidate, SourceKnowledgeCandidate) or not isinstance(inputs, SourceAuditInput):
        raise ValueError("Expected complete source input and candidate")
    inputs.__post_init__()
    candidate.__post_init__()
    if bool(inputs.items) != bool(candidate.items) or (candidate.audit_metadata is None) != (not inputs.items):
        raise ValueError("Candidate generation/outcome disagrees with validated input")
    by_reference = {i.reference: i for i in inputs.items}
    seen = []
    for position, item in enumerate(candidate.items, 1):
        if item.item_id != f"source-item-{position:06d}":
            raise ValueError("Invalid candidate-local identity")
        contributors = tuple(i for i in inputs.items if i.reference in item.derived_from)
        if tuple(i.reference for i in contributors) != item.derived_from:
            raise ValueError("Invented, unordered or repeated contributor")
        if item.evidence != contributing_evidence(contributors):
            raise ValueError("Evidence is not the trusted contributing union")
        seen.extend(item.derived_from)
    if len(seen) != len(by_reference) or set(seen) != set(by_reference):
        raise ValueError("Every validated input must appear exactly once")
    ids, conflicts = {i.item_id for i in candidate.items}, set()
    for position, conflict in enumerate(candidate.conflicts, 1):
        key = frozenset(conflict.item_ids)
        if (conflict.conflict_id != f"conflict-{position:06d}" or not key <= ids or key in conflicts
                or tuple(i.item_id for i in candidate.items if i.item_id in key) != conflict.item_ids):
            raise ValueError("Invalid or duplicate conflict references")
        conflicts.add(key)
    return candidate


def candidate_from_response(response, inputs):
    if not isinstance(response, SourceAuditResponse):
        raise ValueError("Expected complete audit response")
    response.__post_init__()
    by_token = {i.token: i for i in inputs.items}
    items, seen = [], []
    for position, draft in enumerate(response.items, 1):
        if not set(draft.input_refs) <= by_token.keys():
            raise ValueError("Invented contributor")
        contributors = tuple(i for i in inputs.items if i.token in draft.input_refs)
        seen.extend(draft.input_refs)
        items.append(SourceKnowledgeItem(f"source-item-{position:06d}", draft.statement,
            contributing_evidence(contributors), tuple(i.reference for i in contributors)))
    if len(seen) != len(by_token) or set(seen) != set(by_token):
        raise ValueError("Incomplete or repeated input accounting")
    conflicts = []
    for number, draft in enumerate(response.conflicts, 1):
        if any(p > len(items) for p in draft.item_positions):
            raise ValueError("Conflict references unknown output")
        conflicts.append(SourceConflict(f"conflict-{number:06d}", tuple(items[p-1].item_id for p in sorted(draft.item_positions)), draft.reason))
    candidate = SourceKnowledgeCandidate(SourceKnowledgeOutcome.KNOWLEDGE_AVAILABLE if items else SourceKnowledgeOutcome.NO_SOURCE_KNOWLEDGE,
                                          tuple(items), tuple(conflicts), response.metadata)
    return validate_source_candidate(candidate, inputs)


def validate_source_evaluation(evaluation, inputs, *, item_count):
    if not isinstance(evaluation, SourceValidatorEvaluation):
        raise ValueError("Expected complete seven-gate diagnosis")
    evaluation.__post_init__()
    if type(item_count) is not int or not 0 <= item_count <= len(inputs.items):
        raise ValueError("Invalid candidate item count")
    sections = {s.section_id: s for s in inputs.sections}
    references = {i.reference for i in inputs.items}
    for gate in evaluation.gates:
        for issue in gate.issues:
            for identity in issue.affected_source_item_ids:
                number = int(identity.removeprefix("source-item-"))
                if not 1 <= number <= item_count or identity != f"source-item-{number:06d}":
                    raise ValueError("Diagnosis references another candidate")
            if not set(issue.affected_section_item_refs) <= references:
                raise ValueError("Diagnosis references unknown validated input")
            for evidence in issue.relevant_evidence:
                if evidence.section_id not in sections:
                    raise ValueError("Diagnosis references non-CONTENT section")
                _validate_spans(evidence.spans, sections[evidence.section_id])
    gates = {g.gate: g for g in evaluation.gates}
    return SourceValidatorEvaluation(tuple(gates[g] for g in SourceAuditGate), evaluation.metadata)


def _propose(inputs, capability, config, history, previous=None, evaluation=None):
    # Empty validated input never invokes an Agent, including corrective rounds.
    if not inputs.items:
        return SourceKnowledgeCandidate(SourceKnowledgeOutcome.NO_SOURCE_KNOWLEDGE, (), (), None)
    if not callable(getattr(capability, "propose", None)):
        raise SourceAuditOperationalError("CONFIGURATION", phase="AUDIT", rounds=history)
    refinement = None if previous is None else SourceRefinementContext(previous, evaluation)
    request = SourceAuditRequest(inputs, config.audit_output_limit, refinement)
    try:
        response = capability.propose(request)
    except SourceAuditCapabilityError as error:
        raise SourceAuditOperationalError(error.kind, phase="AUDIT", rounds=history) from None
    except Exception:
        raise SourceAuditOperationalError("PROVIDER_CONTRACT", phase="AUDIT", rounds=history) from None
    try:
        return candidate_from_response(response, inputs)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise SourceAuditOperationalError("INVALID_CANDIDATE", phase="AUDIT", rounds=history) from None


def evaluate_source_candidate(inputs, candidate, *, capability, output_limit):
    validate_source_candidate(candidate, inputs)
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("Invalid output allowance")
    if not callable(getattr(capability, "evaluate", None)):
        raise SourceAuditOperationalError("CONFIGURATION", phase="VALIDATOR")
    try:
        evaluation = capability.evaluate(SourceValidatorRequest(inputs, candidate, output_limit))
    except SourceAuditCapabilityError as error:
        raise SourceAuditOperationalError(error.kind, phase="VALIDATOR") from None
    except Exception:
        raise SourceAuditOperationalError("PROVIDER_CONTRACT", phase="VALIDATOR") from None
    try:
        return validate_source_evaluation(evaluation, inputs, item_count=len(candidate.items))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise SourceAuditOperationalError("INVALID_OUTPUT", phase="VALIDATOR") from None


def process_source_knowledge(video_id, *, audit_capability=None, validator_capability=None,
                              silver_config=DEFAULT_SILVER_CONFIG, segmentation_config=DEFAULT_SEGMENTATION_CONFIG,
                              section_validation_config=DEFAULT_SECTION_VALIDATION_CONFIG,
                              config=DEFAULT_SOURCE_AUDIT_CONFIG):
    """One explicit local operation. No automatic upstream repair or failed-loop resume."""
    from .source_knowledge import (
        canonical_source_knowledge_path, candidate_sha256, publish_source_knowledge, read_source_knowledge,
    )
    from . import _bronze_filesystem as filesystem
    from .bronze import DEFAULT_BRONZE_CONFIG
    history = []
    try:
        if not isinstance(config, SourceAuditConfig):
            raise ValueError("Expected source audit configuration")
        config.__post_init__()
        root = resolved_source_path(config.root)
        for protected_config in (DEFAULT_BRONZE_CONFIG, silver_config, segmentation_config, section_validation_config):
            protected = resolved_source_path(protected_config.root)
            if root.is_relative_to(protected) or protected.is_relative_to(root):
                raise SourceAuditIntegrityError("OVERLAPPING_ROOTS")
        inputs = load_source_audit_input(video_id, silver_config=silver_config, segmentation_config=segmentation_config,
                                         section_validation_config=section_validation_config)
        path = canonical_source_knowledge_path(video_id, config)
        validate_source_path(path, root)
        if path.exists() or path.is_symlink():
            artifact = read_source_knowledge(path, inputs, config)
            try:
                filesystem.confirm_durable(path)
            except OSError:
                from .source_audit_models import SourceKnowledgeFilesystemError
                raise SourceKnowledgeFilesystemError("ARTIFACT_DURABILITY", phase="PERSISTENCE") from None
            verify_source_snapshot(inputs)
            return SourceKnowledgeResult(path, False, artifact, 0)
    except SourceAuditError:
        raise
    except (ValueError, TypeError, AttributeError, ArithmeticError, OSError):
        raise SourceAuditInputError("INPUT_CONFIGURATION") from None

    previous, evaluation = None, None
    while True:
        candidate = _propose(inputs, audit_capability, config, history, previous, evaluation)
        try:
            evaluation = evaluate_source_candidate(inputs, candidate, capability=validator_capability,
                                                   output_limit=config.validator_output_limit)
        except SourceAuditError as error:
            raise type(error)(error.kind, phase=error.phase, rounds=history) from None
        history.append(SourceAuditRound(len(history)+1, candidate_sha256(candidate), len(candidate.items), len(candidate.conflicts),
                                         candidate.audit_metadata, evaluation))
        if any(i.kind is SourceIssueKind.UPSTREAM_COVERAGE_DEFECT for g in evaluation.gates for i in g.issues):
            raise SourceAuditUpstreamCoverageError("UPSTREAM_COVERAGE_DEFECT", phase="SEMANTIC", rounds=history)
        if evaluation.decision is SourceAuditDecision.PASS:
            artifact = SourceKnowledgeArtifact(1, 1, datetime.now(timezone.utc).isoformat(), inputs.provenance,
                inputs.upstream_segmentation_outcome, inputs.validated_sections, candidate, tuple(history))
            try:
                path, created, winner = publish_source_knowledge(artifact, inputs, config)
                return SourceKnowledgeResult(path, created, winner, len(history))
            except SourceAuditError as error:
                raise type(error)(error.kind, phase=error.phase, rounds=history) from None
        if len(history) >= config.max_semantic_rounds:
            raise SourceAuditExhaustedError("SOURCE_AUDIT_FAILED", phase="SEMANTIC", rounds=history)
        previous = candidate
