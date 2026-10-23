"""One-section semantic refinement; only completed proposal/Judge pairs consume rounds."""

from datetime import datetime, timezone
from pathlib import Path

from . import _bronze_filesystem as filesystem
from .bronze import DEFAULT_BRONZE_CONFIG
from .extraction import extract_knowledge, load_extraction_section, validate_extraction_proposal
from .extraction_models import (
    DEFAULT_EXTRACTION_CONFIG, ExtractionError, ExtractionFeedback, ExtractionRefinementContext,
)
from .section_knowledge import (
    SUPPORTED_EXTRACTION_PROMPTS, canonical_section_knowledge_path, proposal_sha256,
    publish_section_checkpoint, read_section_checkpoint, validate_judge_references,
)
from .section_validation_models import (
    DEFAULT_SECTION_VALIDATION_CONFIG, GateVerdict, SectionDecision, SectionJudgeCapabilityError,
    SectionJudgeEvaluation, SectionJudgeRequest, SectionJudgeResult, SectionKnowledgeFilesystemError,
    SectionKnowledgeResult, SectionValidationConfig, SectionValidationError,
    SectionValidationExhaustedError, SectionValidationInputError, SectionValidationIntegrityError,
    SectionValidationOperationalError, SemanticRoundRecord, ValidatedSectionKnowledge,
    SemanticGate,
)
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG
from .silver import DEFAULT_SILVER_CONFIG


def evaluate_section(section, proposal, *, capability, output_limit=None):
    try:
        validate_extraction_proposal(proposal, section, supported_prompt_versions=SUPPORTED_EXTRACTION_PROMPTS)
        request = SectionJudgeRequest(section, proposal) if output_limit is None else SectionJudgeRequest(section, proposal, output_limit)
    except (ExtractionError, ValueError, TypeError, AttributeError, ArithmeticError):
        raise SectionValidationInputError("SECTION_PROPOSAL_COMPATIBILITY") from None
    if not callable(getattr(capability, "evaluate", None)):
        raise SectionValidationOperationalError("CONFIGURATION", phase="JUDGE")
    try:
        evaluation = capability.evaluate(request)
    except SectionJudgeCapabilityError as error:
        raise SectionValidationOperationalError(error.kind, phase="JUDGE") from None
    except Exception:
        raise SectionValidationOperationalError("PROVIDER_CONTRACT", phase="JUDGE") from None
    try:
        if not isinstance(evaluation, SectionJudgeEvaluation):
            raise ValueError("Expected structured Judge response")
        validate_judge_references(evaluation, section, {i.item_id for i in proposal.items})
        gates = {g.gate: g for g in evaluation.gates}
        evaluation = SectionJudgeEvaluation(tuple(gates[g] for g in SemanticGate), evaluation.metadata)
        return SectionJudgeResult(evaluation)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise SectionValidationOperationalError("INVALID_OUTPUT", phase="JUDGE") from None


def _extract(section, capability, config, history, previous=None, evaluation=None):
    phase = "EXTRACTION" if previous is None else "REFINEMENT"
    refinement = None
    if previous is not None:
        feedback = tuple(ExtractionFeedback(g.gate.value, i.reason, i.affected_item_ids, i.relevant_source_spans, i.actionable_feedback)
                         for g in evaluation.gates if g.verdict is GateVerdict.FAIL for i in g.issues)
        refinement = ExtractionRefinementContext(previous, feedback)
    try:
        return extract_knowledge(section, capability=capability, config=config, refinement=refinement)
    except ExtractionError as error:
        raise SectionValidationOperationalError(error.kind, phase=phase, rounds=history) from None
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        raise SectionValidationOperationalError("INVALID_OUTPUT", phase=phase, rounds=history) from None


def process_section_knowledge(video_id, section_id, *, initial_proposal=None,
                              extraction_capability=None, judge_capability=None,
                              bronze_config=DEFAULT_BRONZE_CONFIG, silver_config=DEFAULT_SILVER_CONFIG,
                              segmentation_config=DEFAULT_SEGMENTATION_CONFIG,
                              extraction_config=DEFAULT_EXTRACTION_CONFIG,
                              config=DEFAULT_SECTION_VALIDATION_CONFIG):
    """Explicit local operation: no automatic retry, failed-loop resume or source-wide runner."""
    history = []
    try:
        if not isinstance(config, SectionValidationConfig):
            raise ValueError("Expected section validation configuration")
        config.__post_init__()
        root = Path(config.root).expanduser().resolve()
        for protected_config in (bronze_config, silver_config, segmentation_config):
            protected = Path(protected_config.root).expanduser().resolve()
            if root.is_relative_to(protected) or protected.is_relative_to(root):
                raise SectionValidationIntegrityError("OVERLAPPING_ROOTS")
        section = load_extraction_section(video_id, section_id, silver_config=silver_config, segmentation_config=segmentation_config)
        if initial_proposal is not None:
            validate_extraction_proposal(initial_proposal, section, supported_prompt_versions=SUPPORTED_EXTRACTION_PROMPTS)
        path = canonical_section_knowledge_path(video_id, section_id, config)
        if path.is_symlink() or not path.parent.resolve().is_relative_to(root):
            raise SectionValidationIntegrityError("CHECKPOINT_PATH")
        if path.exists():
            checkpoint = read_section_checkpoint(path, section, config)
            try:
                filesystem.confirm_durable(path)
            except OSError:
                raise SectionKnowledgeFilesystemError("CHECKPOINT_DURABILITY", phase="PERSISTENCE") from None
            return SectionKnowledgeResult(path, False, checkpoint, 0)
    except SectionValidationError:
        raise
    except (ExtractionError, ValueError, TypeError, AttributeError, ArithmeticError, OSError):
        raise SectionValidationInputError("UPSTREAM_INPUT") from None

    proposal = initial_proposal
    if proposal is None:
        proposal = _extract(section, extraction_capability, extraction_config, history)
    while True:
        try:
            result = evaluate_section(section, proposal, capability=judge_capability, output_limit=config.judge_output_limit)
        except SectionValidationError as error:
            raise type(error)(error.kind, phase=error.phase, rounds=history) from None
        record = SemanticRoundRecord(len(history) + 1, proposal.outcome, len(proposal.items), proposal_sha256(proposal),
                                      proposal.metadata, result.evaluation)
        history.append(record)
        if result.decision is SectionDecision.PASS:
            checkpoint = ValidatedSectionKnowledge(1, 1, datetime.now(timezone.utc).isoformat(), section.section_id,
                                                   section.provenance, proposal, tuple(history))
            try:
                path, created, winner = publish_section_checkpoint(checkpoint, section, config)
                return SectionKnowledgeResult(path, created, winner, len(history))
            except SectionValidationError as error:
                raise type(error)(error.kind, phase=error.phase, rounds=history) from None
        if len(history) >= config.max_semantic_rounds:
            raise SectionValidationExhaustedError("SECTION_VALIDATION_FAILED", phase="SEMANTIC", rounds=history)
        proposal = _extract(section, extraction_capability, extraction_config, history, previous=proposal, evaluation=result.evaluation)
