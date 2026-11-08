"""Durable commit/replay of existing preparation, with one canonical selector."""

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from . import _gold_filesystem as fs
from .gold_document_models import GoldConfig
from .gold_store import (
    LoadedGoldSnapshot, _manifest, _read_selection, _read_selector, stage_gold_snapshot,
)
from .gold_integration import prepare_gold_integration, validate_prepared_integration
from .gold_integration_input import load_integration_source
from .gold_integration_models import IntegrationConfig
from .gold_history import (
    history_errors, load_gold_history, read_selected_history, history_node, history_path, coordinated_selector,
)
from .gold_history_models import (
    GoldCommitError, GoldHistoryReference, GoldIntegrationRecord, GoldRoundAudit, GoldCommitResult, check,
)
from .gold_history_codec import document, encode_history
from .gold_events import operation_audits, derive_gold_events


def _source_matches(result, source_configs):
    source = load_integration_source(result.source_reference.source_id, **source_configs)
    check(source.reference == result.source_reference and source.item_ids == frozenset(result.source_item_ids), "SOURCE_COMPATIBILITY")
    check((source.artifact.outcome.value == "NO_SOURCE_KNOWLEDGE") == (result.outcome == "NO_SOURCE_KNOWLEDGE"), "SOURCE_OUTCOME")
    return source


def _reused(view, record):
    return GoldCommitResult(record, view.gold_reference, view.reference, True)


def commit_prepared_integration(result, *, gold_config=GoldConfig(), clock=None, **source_configs):
    """Validate original in-memory55result, then commit without new semantic work.

    History must be enabled explicitly. A successful replay returns the prior ID,
    even after legitimate later audited evolution. A new stale proposal fails.
    """
    with history_errors():
        validate_prepared_integration(result)
        _source_matches(result, source_configs)
        view = load_gold_history(gold_config)
        prior = view.find_integration(result.source_reference)
        if prior is not None:
            return _reused(view, prior)
        check(view.gold_reference == result.base_reference, "STALE_PREPARED_INTEGRATION")
        target = result.base_reference
        if result.would_change_gold:
            routes = {d.document_id: f"{d.entity.namespace}/{d.entity.entity_key}/{d.domain_key}/{d.document_id}"
                      for d in result.candidate_snapshot.documents}
            target = stage_gold_snapshot(result.candidate_snapshot, config=gold_config,
                                         base=result.base_reference, routes=routes).reference
        with fs.publication_guard(gold_config.root):
            selector, loaded = _read_selection(gold_config)
            view = read_selected_history(gold_config, selector, loaded)
            prior = view.find_integration(result.source_reference)
            if prior is not None:
                return _reused(view, prior)
            check(loaded.reference == result.base_reference and loaded.snapshot == result.base_snapshot,
                  "STALE_PREPARED_INTEGRATION")
            _source_matches(result, source_configs)
            check(_manifest(gold_config, target)[1] == result.candidate_snapshot, "STAGED_GOLD")
            timestamp = datetime.now(timezone.utc) if clock is None else clock()
            if isinstance(timestamp, datetime):
                timestamp = timestamp.isoformat()
            record = GoldIntegrationRecord(1, 1, result.schema_version, result.integration_contract_version,
                "integration-" + uuid4().hex, timestamp, result.source_reference, result.source_item_ids,
                result.base_reference, target, result.base_sha256, result.candidate_sha256, result.outcome,
                operation_audits(result), tuple(GoldRoundAudit(r.number, r.plan.metadata, r.evaluation.metadata,
                    r.evaluation.decision) for r in result.rounds), result.context, result.effects, ())
            record = replace(record, events=derive_gold_events(record, result.base_snapshot, result.candidate_snapshot))
            identity = uuid4().hex
            content = encode_history(history_node(identity, view.reference, result.base_reference, target,
                result.base_sha256, result.candidate_sha256, document(record)))
            fs.write_exclusive(history_path(gold_config, identity), content)
            reference = GoldHistoryReference(identity, sha256(content).hexdigest())
            proposed = coordinated_selector(target, reference)
            read_selected_history(gold_config, proposed, LoadedGoldSnapshot(target, result.candidate_snapshot))
            check(_read_selector(gold_config) == selector, "COMMIT_SELECTION_CHANGED")
            try:
                fs.replace_selector(gold_config.root, encode_history(proposed))
            except OSError:
                # The atomic replacement may already have happened. Confirm only
                # a fully validated visible success; never repair or half-rollback.
                confirmed = load_gold_history(gold_config)
                found = confirmed.find_integration(result.source_reference)
                if found is None or found.integration_id != record.integration_id:
                    raise GoldCommitError("COMMIT_PUBLICATION_FAILED") from None
                return GoldCommitResult(found, confirmed.gold_reference, confirmed.reference, False)
            confirmed = load_gold_history(gold_config)
            check(confirmed.reference == reference and confirmed.get_integration(record.integration_id) == record,
                  "POST_PUBLICATION_INTEGRITY")
            return GoldCommitResult(record, confirmed.gold_reference, confirmed.reference, False)


def integrate_source_knowledge(video_id, *, gold_config=GoldConfig(), agent=None, validator=None,
                               integration_config=IntegrationConfig(), context=(), clock=None, **source_configs):
    """Replay before55; otherwise prepare once using existing semantic service."""
    with history_errors():
        source = load_integration_source(video_id, **source_configs)
        view = load_gold_history(gold_config)
        prior = view.find_integration(source.reference)
        if prior is not None:
            return _reused(view, prior)
        prepared = prepare_gold_integration(video_id, agent=agent, validator=validator, gold_config=gold_config,
            integration_config=integration_config, context=context, **source_configs)
        return commit_prepared_integration(prepared, gold_config=gold_config, clock=clock, **source_configs)
