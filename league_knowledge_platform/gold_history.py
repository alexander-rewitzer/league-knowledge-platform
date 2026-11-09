"""Hash-linked immutable history selected atomically with canonical Gold."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import uuid4

from . import _gold_filesystem as fs
from .gold_document_models import GoldConfig
from .gold_documents import validate_document_transition
from .gold_store import _manifest, _read_selection, _read_selector, initialize_gold_store
from .gold_application import logical_gold_hash
from .gold_models import GoldContractError, GoldSourceArtifactReference, _identity
from .gold_store import GoldSnapshotReference
from .gold_history_codec import keys, encode_history, decode_history_bytes, decode_integration_record
from .gold_history_models import (
    GoldHistoryError, GoldHistoryCompatibilityError, GoldHistoryReference,
    GoldIntegrationRecord, check, version, digest, date,
)
from .gold_events import validate_audited_state, derive_gold_events


@contextmanager
def history_errors():
    try:
        yield
    except GoldHistoryError:
        raise
    except GoldContractError as error:
        raise GoldHistoryError(error.code) from None
    except OSError:
        raise GoldHistoryError("HISTORY_FILESYSTEM") from None
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise GoldHistoryError("HISTORY_INVALID") from None


def coordinated_selector(gold, history):
    return {"store_schema_version": 2, "storage_id": gold.storage_id, "manifest_sha256": gold.manifest_sha256,
            "integration_history": asdict(history)}


def history_path(config, identity):
    # Validate independently of a caller-supplied relative path.
    GoldHistoryReference(identity, "0" * 64)
    return fs.safe_path(config.root, f"history/{identity}.json")


def history_node(identity, parent, before, after, base_hash, candidate_hash, record):
    return {"history_schema_version": 1, "integration_contract_version": 1, "commit_id": identity,
            "parent": None if parent is None else asdict(parent), "gold_before": asdict(before),
            "gold_after": asdict(after), "base_sha256": base_hash, "candidate_sha256": candidate_hash,
            "integration": record}


@dataclass(frozen=True, slots=True)
class GoldHistorySnapshot:
    reference: GoldHistoryReference
    gold_reference: GoldSnapshotReference
    baseline_reference: GoldSnapshotReference
    integrations: tuple[GoldIntegrationRecord, ...]

    def get_integration(self, integration_id):
        _identity(integration_id, "integration")
        for record in self.integrations:
            if record.integration_id == integration_id:
                return record
        raise GoldHistoryError("UNKNOWN_INTEGRATION")

    def find_integration(self, source_reference, *, integration_contract_version=1):
        version(integration_contract_version)
        check(type(source_reference) is GoldSourceArtifactReference, "SOURCE_REFERENCE")
        replace(source_reference)
        key = (source_reference.source_type, source_reference.source_id, source_reference.relative_path)
        for record in self.integrations:
            prior = record.source_reference
            if (prior.source_type, prior.source_id, prior.relative_path) == key:
                if prior != source_reference or record.integration_contract_version != integration_contract_version:
                    raise GoldHistoryCompatibilityError("REPLAY_SOURCE_COMPATIBILITY")
                return record
        return None

    def query_events(self, *, integration_id=None, knowledge_id=None, source_id=None,
                     source_reference=None, since=None, until=None):
        if integration_id is not None:
            _identity(integration_id, "integration")
        if knowledge_id is not None:
            _identity(knowledge_id)
        if source_reference is not None:
            check(type(source_reference) is GoldSourceArtifactReference, "SOURCE_REFERENCE")
            replace(source_reference)
        if source_id is not None:
            from .models import _require_video_id
            _require_video_id(source_id)
        start, end = date(since) if since is not None else None, date(until) if until is not None else None
        check(start is None or end is None or start <= end, "TIME_RANGE")
        events = []
        for sequence, record in enumerate(self.integrations):
            for ordinal, event in enumerate(record.events):
                if (integration_id is not None and event.integration_id != integration_id
                        or knowledge_id is not None and knowledge_id not in event.affected_knowledge_ids
                        or source_id is not None and event.source_reference.source_id != source_id
                        or source_reference is not None and event.source_reference != source_reference
                        or start is not None and date(event.timestamp) < start
                        or end is not None and date(event.timestamp) > end):
                    continue
                events.append((date(event.timestamp), sequence, ordinal, event))
        return tuple(v[3] for v in sorted(events, key=lambda v: v[:3]))

    def event_unit_changes(self, event_id):
        for record in self.integrations:
            for event in record.events:
                if event.event_id == event_id:
                    return tuple(u for u in record.effects.units if u.knowledge_id in event.unit_change_ids)
        raise GoldHistoryError("UNKNOWN_EVENT")


def read_selected_history(config, selector, loaded):
    """No selector reread: this view and Gold come from exactly one selection."""
    with history_errors():
        check(selector["store_schema_version"] == 2, "HISTORY_NOT_ENABLED")
        head = GoldHistoryReference(**selector["integration_history"])
        ref, nodes, seen = head, [], set()
        while ref is not None:
            check(ref.commit_id not in seen, "HISTORY_CYCLE")
            seen.add(ref.commit_id)
            content = fs.read_bytes(history_path(config, ref.commit_id))
            check(sha256(content).hexdigest() == ref.sha256, "HISTORY_HASH")
            node = keys(decode_history_bytes(content), "history_schema_version integration_contract_version commit_id parent gold_before gold_after base_sha256 candidate_sha256 integration")
            version(node["history_schema_version"])
            version(node["integration_contract_version"])
            check(node["commit_id"] == ref.commit_id, "HISTORY_ID")
            digest(node["base_sha256"])
            digest(node["candidate_sha256"])
            before = GoldSnapshotReference(**keys(node["gold_before"], GoldSnapshotReference))
            after = GoldSnapshotReference(**keys(node["gold_after"], GoldSnapshotReference))
            parent = None if node["parent"] is None else GoldHistoryReference(**keys(node["parent"], GoldHistoryReference))
            nodes.append((node, before, after))
            ref = parent
        nodes.reverse()
        records, ids, event_ids, sources, snapshots = [], set(), set(), set(), {}

        def snapshot(ref):
            if ref not in snapshots:
                snapshots[ref] = _manifest(config, ref)[1]
            return snapshots[ref]

        previous = None
        for sequence, (node, before_ref, after_ref) in enumerate(nodes):
            before, after = snapshot(before_ref), snapshot(after_ref)
            check(logical_gold_hash(before) == node["base_sha256"]
                  and logical_gold_hash(after) == node["candidate_sha256"], "HISTORY_GOLD_HASH")
            if sequence == 0:
                check(node["parent"] is None and node["integration"] is None
                      and before_ref == after_ref and before == after, "HISTORY_BASELINE")
                baseline = before_ref
            else:
                check(node["parent"] is not None and before_ref == previous, "HISTORY_CONTINUITY")
                record = decode_integration_record(node["integration"])
                check(record.base_reference == before_ref and record.result_reference == after_ref
                      and record.base_sha256 == node["base_sha256"]
                      and record.candidate_sha256 == node["candidate_sha256"], "RECORD_CONTEXT")
                check(record.integration_id not in ids, "DUPLICATE_INTEGRATION")
                ids.add(record.integration_id)
                source = record.source_reference
                key = (source.source_type, source.source_id, source.relative_path)
                check(key not in sources, "DUPLICATE_REPLAY")
                sources.add(key)
                validate_document_transition(before, after)
                check(before != after or before_ref == after_ref, "NOOP_GOLD_MUTATION")
                validate_audited_state(record, before, after)
                check(record.events == derive_gold_events(record, before, after), "EVENT_DERIVATION")
                check(bool(record.events), "MISSING_EVENTS")
                for event in record.events:
                    check(event.event_id not in event_ids, "DUPLICATE_EVENT")
                    event_ids.add(event.event_id)
                records.append(record)
            previous = after_ref
        check(previous == loaded.reference and snapshot(previous) == loaded.snapshot, "GOLD_HISTORY_CONSISTENCY")
        return GoldHistorySnapshot(head, loaded.reference, baseline, tuple(records))


def load_gold_history(config=GoldConfig()):
    with history_errors():
        selector, loaded = _read_selection(config)
        return read_selected_history(config, selector, loaded)


def enable_gold_integration_history(config=GoldConfig()):
    """Explicit opt-in/migration only: back up legacy selector; reconstruct no events."""
    with history_errors(), fs.publication_guard(config.root):
        selector, loaded = _read_selection(config)
        if selector["store_schema_version"] == 2:
            return read_selected_history(config, selector, loaded).reference
        raw = fs.read_bytes(fs.safe_path(config.root, "CURRENT.json"))
        backup = fs.safe_path(config.root, f"history/legacy-selector-{loaded.reference.storage_id}.json")
        if backup.exists():
            check(fs.read_bytes(backup) == raw, "LEGACY_BACKUP_CONFLICT")
        else:
            fs.write_exclusive(backup, raw)
        identity = uuid4().hex
        state_hash = logical_gold_hash(loaded.snapshot)
        content = encode_history(history_node(identity, None, loaded.reference, loaded.reference,
                                              state_hash, state_hash, None))
        fs.write_exclusive(history_path(config, identity), content)
        reference = GoldHistoryReference(identity, sha256(content).hexdigest())
        proposed = coordinated_selector(loaded.reference, reference)
        read_selected_history(config, proposed, loaded)
        check(_read_selector(config) == selector, "INITIALIZATION_STALE")
        fs.replace_selector(config.root, encode_history(proposed))
        return reference


def initialize_gold_integration_store(config=GoldConfig()):
    """Explicit coordinated initialization; no existing store/data is silently reset."""
    initialize_gold_store(config)
    return enable_gold_integration_history(config)
