"""One explicit, transient preparation operation; no durable success or publication."""

from dataclasses import replace

from .gold_application import (IntegrationEffects, apply_integration_plan, validate_integration_plan,
                               integration_effects, logical_gold_hash)
from .gold_document_models import GoldConfig
from .gold_models import GoldContractError
from .gold_documents import validate_gold_snapshot
from .source_audit_models import SourceAuditError
from .gold_integration_input import IntegrationInspection, load_integration_source
from .gold_integration_models import (
    IntegrationAgentRequest, IntegrationConfig, IntegrationError, IntegrationEvaluation,
    IntegrationOperationalError, IntegrationRound, IntegrationValidationFailed,
    IntegrationValidatorRequest, PreparedIntegrationResult, require,
)


def validate_integration_evaluation(evaluation, plan, source_item_ids, base):
    require(type(evaluation) is IntegrationEvaluation, "EVALUATION")
    replace(evaluation)
    operation_ids = {o.operation_id for o in plan.operations}
    gold_ids = {u.knowledge_id for u in validate_gold_snapshot(base).units}
    gold_ids.update(d.document_id for d in base.documents)
    gold_ids.update(t.topic_id for d in base.documents for t in d.topics)
    gold_ids.update(d.document_id for d in plan.proposed_layout)
    gold_ids.update(t.topic_id for d in plan.proposed_layout for t in d.topics)
    from .gold_governance import _new_intents
    gold_ids.update(n.unit.knowledge_id for o in plan.operations for n in _new_intents(o.payload))
    for gate in evaluation.gates:
        for issue in gate.issues:
            require(set(issue.affected_operation_ids) <= operation_ids
                    and set(issue.affected_source_item_ids) <= source_item_ids
                    and set(issue.affected_gold_ids) <= gold_ids, "FEEDBACK_REFERENCE")
    return evaluation


def validate_prepared_integration(result):
    """Reconstruct integrity only; this cannot certify canonical input or durable success.

    Future commit must revalidate source compatibility, history and the live base.
    No caller-supplied immutable dataclass is treated as an authorization token.
    """
    require(type(result) is PreparedIntegrationResult, "RESULT")
    replace(result)
    require(type(result.effects) is IntegrationEffects, "EFFECTS")
    validate_gold_snapshot(result.base_snapshot)
    validate_gold_snapshot(result.candidate_snapshot)
    require(result.base_sha256 == logical_gold_hash(result.base_snapshot), "BASE_HASH")
    if result.outcome == "NO_SOURCE_KNOWLEDGE":
        require(not result.source_item_ids and result.plan is None and not result.rounds, "EMPTY_RESULT")
        candidate = result.base_snapshot
    else:
        require(bool(result.source_item_ids) and 1 <= len(result.rounds) <= 3, "RESULT_ROUNDS")
        for n, record in enumerate(result.rounds, 1):
            require(record.number == n and record.plan.source_reference == result.source_reference
                    and record.plan.base_reference == result.base_reference, "RESULT_ROUND_CONTEXT")
            require(record.evaluation.decision == ("PASS" if n == len(result.rounds) else "REJECT"), "RESULT_DECISION")
            validate_integration_plan(record.plan, base=result.base_snapshot,
                source_reference=result.source_reference, source_item_ids=frozenset(result.source_item_ids),
                base_reference=result.base_reference)
            validate_integration_evaluation(record.evaluation, record.plan,
                frozenset(result.source_item_ids), result.base_snapshot)
        require(result.plan == result.rounds[-1].plan, "RESULT_PLAN")
        candidate = apply_integration_plan(result.plan, base=result.base_snapshot,
            source_reference=result.source_reference, source_item_ids=frozenset(result.source_item_ids),
            base_reference=result.base_reference)
        require(result.outcome == ("PREPARED_NO_CHANGE" if candidate == result.base_snapshot else "PREPARED_CHANGE"), "OUTCOME")
    require(result.candidate_snapshot == candidate and result.candidate_sha256 == logical_gold_hash(candidate), "CANDIDATE_HASH")
    require(result.effects == integration_effects(result.base_snapshot, candidate, result.plan, result.source_item_ids), "EFFECTS")
    return result


def prepare_gold_integration(video_id, *, agent=None, validator=None, gold_config=GoldConfig(),
                             integration_config=IntegrationConfig(), context=(), **source_configs):
    """Return a complete prepared transaction in memory. Never stage/commit it.

    Repeated calls are new preparations; durable replay belongs before this API.
    Operational failure aborts immediately, retaining only completed round records.
    """
    rounds, phase = [], "INPUT"
    try:
        require(type(integration_config) is IntegrationConfig, "CONFIGURATION")
        replace(integration_config)
        require(type(context) is tuple and all(type(p) is tuple and len(p) == 2 for p in context), "CONTEXT")
        require(all(p[0] in ("logical_run_id", "target_id", "selected_video_id") for p in context), "CONTEXT")
        source = load_integration_source(video_id, **source_configs)
        phase = "INSPECTION"
        inspection = IntegrationInspection(gold_config, integration_config)
        base, reference = inspection.snapshot, inspection.reference
        plan, candidate = None, base
        if source.item_ids:
            inspection.query("search_gold", query=" ".join(i.statement for i in source.artifact.candidate.items),
                             limit=10, status="ACTIVE")
            previous, feedback = None, None
            while True:
                phase = "AGENT"
                require(callable(getattr(agent, "propose", None)), "CONFIGURATION")
                plan = agent.propose(IntegrationAgentRequest(source, inspection, integration_config, previous, feedback))
                phase = "PLAN"
                validate_integration_plan(plan, base=base, source_reference=source.reference,
                                          source_item_ids=source.item_ids, base_reference=reference)
                phase = "INSPECTION"
                inspection.include_plan_context(plan)
                phase = "VALIDATOR"
                require(callable(getattr(validator, "evaluate", None)), "CONFIGURATION")
                evaluation = validator.evaluate(IntegrationValidatorRequest(source, plan, inspection.observations,
                                                                           integration_config.validator_output_limit))
                validate_integration_evaluation(evaluation, plan, source.item_ids, base)
                rounds.append(IntegrationRound(len(rounds) + 1, plan, evaluation))
                if evaluation.decision == "PASS":
                    break
                if len(rounds) >= integration_config.max_semantic_rounds:
                    raise IntegrationValidationFailed("INTEGRATION_VALIDATION_FAILED", phase="SEMANTIC", rounds=rounds)
                previous, feedback = plan, evaluation
            phase = "APPLICATION"
            candidate = apply_integration_plan(plan, base=base, source_reference=source.reference,
                                                source_item_ids=source.item_ids, base_reference=reference)
        phase = "INPUT"
        require(load_integration_source(video_id, **source_configs) == source, "SOURCE_CHANGED")
        phase = "RESULT"
        outcome = ("NO_SOURCE_KNOWLEDGE" if not source.item_ids else
                   "PREPARED_NO_CHANGE" if candidate == base else "PREPARED_CHANGE")
        result = PreparedIntegrationResult(1, 1, source.reference, tuple(sorted(source.item_ids)), reference,
            base, candidate, plan, tuple(rounds), outcome,
            integration_effects(base, candidate, plan, source.item_ids), logical_gold_hash(base),
            logical_gold_hash(candidate), context)
        return validate_prepared_integration(result)
    except IntegrationValidationFailed:
        raise
    except IntegrationError as error:
        raise IntegrationOperationalError(error.code, phase=phase, rounds=rounds) from None
    except SourceAuditError as error:
        raise IntegrationOperationalError(error.kind, phase=phase, rounds=rounds) from None
    except GoldContractError as error:
        raise IntegrationOperationalError(error.code, phase=phase, rounds=rounds) from None
    except OSError:
        raise IntegrationOperationalError("FILESYSTEM", phase=phase, rounds=rounds) from None
    except Exception:
        # Third-party diagnostics/paths/content are never copied to operation errors.
        raise IntegrationOperationalError("OPERATION_FAILED", phase=phase, rounds=rounds) from None
