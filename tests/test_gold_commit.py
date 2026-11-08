"""Offline durable integration, exact replay, events and single-selector recovery."""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldSnapshot, GoldHistoryError, GoldHistoryCompatibilityError,
    GoldEventType, commit_prepared_integration, integrate_source_knowledge,
    initialize_gold_store, initialize_gold_integration_store, enable_gold_integration_history,
    load_gold_snapshot, load_gold_history, prepare_gold_integration,
    publish_gold_snapshot, stage_gold_snapshot, process_source_knowledge,
    GoldNoChangePayload, GoldStatus, GoldProvenanceAddition,
)
from league_knowledge_platform import _gold_filesystem as fs
from league_knowledge_platform.gold_application import logical_gold_hash, integration_effects
from league_knowledge_platform.gold_history_codec import encode_history
from gold_integration_fixtures import BASE, FakeAgent, FakeValidator, source, plan, real_create
from test_gold_governance_models import examples, operation, SUPPORT
from test_gold_models import identity
from test_gold_models import unit
from gold_document_fixtures import document, topic
from test_gold_provenance import source_workspace
from source_audit_fixtures import workspace, FakeValidator as SourceValidator, validator_evaluation


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.gold = GoldConfig(self.root / 'gold')
        ref = initialize_gold_store(self.gold)
        staged = stage_gold_snapshot(BASE, config=self.gold, base=ref)
        self.base = publish_gold_snapshot(staged, config=self.gold, expected_base=ref)
        enable_gold_integration_history(self.gold)
        self.source = source()
        for name in ('gold_integration', 'gold_commit'):
            loader = patch('league_knowledge_platform.' + name + '.load_integration_source', return_value=self.source)
            loader.start()
            self.addCleanup(loader.stop)

    def prepare(self, index=0, **kwargs):
        return prepare_gold_integration('B', agent=FakeAgent([index]), validator=FakeValidator(),
                                        gold_config=self.gold, **kwargs)

    def commit(self, result, **kwargs):
        return commit_prepared_integration(result, gold_config=self.gold, **kwargs)

    def files(self):
        return {p.relative_to(self.gold.root): p.read_bytes() for p in self.gold.root.rglob('*') if p.is_file()}

    def test_all_seven_operations_and_exact_eight_event_vocabulary(self):
        expected = (
            {'NO_CHANGE_RECORDED'}, {'KNOWLEDGE_CREATED', 'PROVENANCE_ADDED'},
            {'KNOWLEDGE_ENRICHED', 'PROVENANCE_ADDED'}, {'KNOWLEDGE_REWRITTEN', 'PROVENANCE_ADDED'},
            {'KNOWLEDGE_MERGED', 'PROVENANCE_ADDED'}, {'RELATION_ADDED'},
            {'KNOWLEDGE_CREATED', 'CONFLICT_FLAGGED', 'RELATION_ADDED', 'PROVENANCE_ADDED'},
        )
        seen = set()
        # Each independent store starts at the same baseline.
        original = self.gold
        for n, kinds in enumerate(expected):
            with self.subTest(operation=n):
                self.gold = GoldConfig(self.root / str(n))
                ref = initialize_gold_store(self.gold)
                staged = stage_gold_snapshot(BASE, config=self.gold, base=ref)
                publish_gold_snapshot(staged, config=self.gold, expected_base=ref)
                enable_gold_integration_history(self.gold)
                prepared = self.prepare(n)
                result = self.commit(prepared)
                record = result.integration
                actual = {e.event_type.value for e in record.events}
                self.assertEqual(actual, kinds)
                seen.update(actual)
                self.assertEqual(record.effects, prepared.effects)
                self.assertEqual(load_gold_snapshot(self.gold).snapshot, prepared.candidate_snapshot)
                self.assertEqual(load_gold_history(self.gold).integrations, (record,))
                for event in record.events:
                    self.assertEqual(event.integration_id, record.integration_id)
                    self.assertEqual(event.source_reference, record.source_reference)
                    self.assertTrue(event.operation_ids)
                    self.assertEqual(event.document_changes, record.effects.documents)
        self.gold = original
        self.assertEqual(seen, {v.value for v in GoldEventType})

    def test_noop_retains_gold_reference_pairs_versions_but_records_durable_success(self):
        before = self.files()
        result = self.commit(self.prepare())
        after = self.files()
        self.assertEqual(result.gold_reference, self.base)
        self.assertEqual(result.integration.base_reference, result.integration.result_reference)
        self.assertEqual(result.integration.effects.documents, ())
        self.assertEqual(result.integration.events[0].reason, 'NO_CANONICAL_MUTATION')
        for path, content in before.items():
            if str(path) != 'CURRENT.json':
                self.assertEqual(after[path], content)

    def test_provenance_only_no_change_has_only_support_event_and_actual_atom(self):
        op = operation(GoldNoChangePayload((identity(),), (GoldProvenanceAddition(identity(), SUPPORT),)))
        def propose(request):
            return plan(operations=(op,), base_reference=request.inspection.reference,
                        source_reference=request.source.reference)
        result = self.commit(self.prepare(propose))
        self.assertEqual([e.event_type for e in result.integration.events], [GoldEventType.PROVENANCE_ADDED])
        event = result.integration.events[0]
        self.assertEqual(result.integration.effects.provenance_additions[event.effect_index].provenance, SUPPORT)
        self.assertEqual(event.source_item_ids, (SUPPORT.source_item_id,))
        self.assertNotEqual(result.gold_reference, self.base)

    def test_replay_precedes_semantic_roles_and_performs_no_writes(self):
        prepared = self.prepare(1)
        first = self.commit(prepared)
        before = self.files()
        with patch('league_knowledge_platform.gold_commit.prepare_gold_integration', side_effect=AssertionError), \
                patch.object(fs, 'write_exclusive', side_effect=AssertionError), \
                patch('sqlite3.connect', side_effect=AssertionError), \
                patch('socket.create_connection', side_effect=AssertionError):
            replay = integrate_source_knowledge('B', gold_config=self.gold)
            direct = self.commit(prepared)
        self.assertTrue(replay.reused and direct.reused)
        self.assertEqual(replay.integration, first.integration)
        self.assertEqual(direct.integration.integration_id, first.integration.integration_id)
        self.assertEqual(self.files(), before)

    def test_incompatible_replay_hash_reference_and_contract_fail_without_reprocessing(self):
        record = self.commit(self.prepare()).integration
        view = load_gold_history(self.gold)
        for changes in ({'sha256': '0'*64}, {'schema_version': 2}, {'audit_contract_version': 2}):
            with self.subTest(changes=changes), self.assertRaises(GoldHistoryCompatibilityError):
                view.find_integration(replace(record.source_reference, **changes))
        with self.assertRaises(GoldHistoryCompatibilityError):
            view.find_integration(record.source_reference, integration_contract_version=2)

    def test_new_stale_preparation_rejected_but_original_success_reused(self):
        old = self.prepare(1)
        other_source = replace(self.source, reference=replace(self.source.reference, source_id='C', relative_path='C.json'))
        def other_plan(request):
            refs = tuple(replace(p, source_knowledge_artifact=request.source.reference) for p in examples()[0].source_refs)
            return plan(operations=(replace(examples()[0], source_refs=refs),),
                        base_reference=request.inspection.reference, source_reference=request.source.reference)
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=other_source):
            stale = self.prepare(other_plan)
        first = self.commit(old)
        with patch('league_knowledge_platform.gold_commit.load_integration_source', return_value=other_source):
            with self.assertRaises(GoldHistoryError) as caught:
                self.commit(stale)
            self.assertEqual(caught.exception.code, 'STALE_PREPARED_INTEGRATION')
        self.assertEqual(self.commit(old).integration, first.integration)
        self.assertEqual(len(load_gold_history(self.gold).integrations), 1)

    def test_distinct_source_noops_append_once_and_old_replay_survives_audited_evolution(self):
        first = self.commit(self.prepare())
        first_view = load_gold_history(self.gold)
        other_source = replace(self.source, reference=replace(self.source.reference, source_id='C', relative_path='C.json'))
        def other_plan(request):
            refs = tuple(replace(p, source_knowledge_artifact=request.source.reference) for p in examples()[0].source_refs)
            return plan(operations=(replace(examples()[0], source_refs=refs),),
                        base_reference=request.inspection.reference, source_reference=request.source.reference)
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=other_source), \
                patch('league_knowledge_platform.gold_commit.load_integration_source', return_value=other_source):
            second = self.commit(self.prepare(other_plan))
        current = load_gold_history(self.gold)
        self.assertEqual(current.gold_reference, first.gold_reference)
        self.assertNotEqual(second.integration.integration_id, first.integration.integration_id)
        self.assertEqual(len(current.integrations), 2)
        self.assertEqual(len(first_view.integrations), 1)
        self.assertEqual(integrate_source_knowledge('B', gold_config=self.gold).integration, first.integration)

    def test_guarded_replay_rechecks_even_when_gold_noop_reference_unchanged(self):
        prepared = self.prepare()
        original = fs.publication_guard
        completed = []
        @contextmanager
        def another_commit_before_lock(root):
            with patch.object(fs, 'publication_guard', original):
                completed.append(self.commit(prepared))
            with original(root):
                yield
        with patch.object(fs, 'publication_guard', another_commit_before_lock):
            result = self.commit(prepared)
        self.assertTrue(result.reused)
        self.assertEqual(result.integration.integration_id, completed[0].integration.integration_id)
        self.assertEqual(len(load_gold_history(self.gold).integrations), 1)

    def test_prior_success_reused_after_later_audited_gold_mutation(self):
        first = self.commit(self.prepare())
        other = replace(self.source, reference=replace(self.source.reference, source_id='C', relative_path='C.json'))
        def create(request):
            op = examples()[1]
            refs = tuple(replace(p, source_knowledge_artifact=request.source.reference) for p in op.source_refs)
            op = replace(op, source_refs=refs, payload=replace(op.payload, new_unit=replace(op.payload.new_unit,
                unit=replace(op.payload.new_unit.unit, provenance=refs))))
            return plan(1, operations=(op,), base_reference=request.inspection.reference, source_reference=request.source.reference)
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=other), \
                patch('league_knowledge_platform.gold_commit.load_integration_source', return_value=other):
            self.commit(self.prepare(create))
        replay = integrate_source_knowledge('B', gold_config=self.gold)
        self.assertEqual(replay.integration, first.integration)
        self.assertNotEqual(replay.gold_reference, first.gold_reference)
        self.assertEqual(len(load_gold_history(self.gold).integrations), 2)

    def test_merge_pruning_retains_original_history_and_origin_event_document_hashes(self):
        history = unit(3, status=GoldStatus.MERGED, merged_into=identity(2))
        base = GoldSnapshot((document(topics=(topic(ids=(1,)),), units=(unit(),)),
            document(2, topics=(topic(2, ids=(2,)),), units=(unit(2),), historical=(history,), domain='macro')))
        self.gold = GoldConfig(self.root/'pruning')
        ref = initialize_gold_store(self.gold)
        staged = stage_gold_snapshot(base, config=self.gold, base=ref)
        publish_gold_snapshot(staged, config=self.gold, expected_base=ref)
        enable_gold_integration_history(self.gold)
        record = self.commit(self.prepare(4)).integration
        current = load_gold_snapshot(self.gold).snapshot
        self.assertEqual(len(current.documents), 1)
        self.assertIn(history, current.documents[0].historical_units)
        event = next(e for e in record.events if e.event_type is GoldEventType.KNOWLEDGE_MERGED)
        pruned = next(d for d in event.document_changes if d.proposed_version is None)
        self.assertIsNotNone(pruned.before_sha256)
        self.assertIsNone(pruned.after_sha256)
        self.assertEqual(event.merged_ids, (identity(2),))
        self.assertEqual(load_gold_history(self.gold).query_events(knowledge_id=identity(2))[0], event)

    def test_guarded_stale_check_after_initial_check_and_staging(self):
        stale = self.prepare(1)
        other_source = replace(self.source, reference=replace(self.source.reference, source_id='C', relative_path='C.json'))
        def enrich(request):
            refs = tuple(replace(p, source_knowledge_artifact=request.source.reference) for p in examples()[2].source_refs)
            support = tuple(replace(p, source_knowledge_artifact=request.source.reference) if p == SUPPORT else p
                            for p in examples()[2].payload.resulting_unit.provenance)
            op = replace(examples()[2], source_refs=refs, payload=replace(examples()[2].payload,
                resulting_unit=replace(examples()[2].payload.resulting_unit, provenance=support)))
            return plan(operations=(op,), base_reference=request.inspection.reference, source_reference=request.source.reference)
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=other_source):
            other = self.prepare(enrich)
        original = fs.publication_guard
        @contextmanager
        def another_commit_before_lock(root):
            with patch.object(fs, 'publication_guard', original), \
                    patch('league_knowledge_platform.gold_commit.load_integration_source', return_value=other_source):
                self.commit(other)
            with original(root):
                yield
        with patch.object(fs, 'publication_guard', another_commit_before_lock), self.assertRaises(GoldHistoryError) as caught:
            self.commit(stale)
        self.assertEqual(caught.exception.code, 'STALE_PREPARED_INTEGRATION')
        self.assertEqual(len(load_gold_history(self.gold).integrations), 1)

    def test_overlapping_support_deduplicates_actual_atom_and_retains_exact_origins(self):
        support = GoldProvenanceAddition(identity(), SUPPORT)
        first = operation(GoldNoChangePayload((identity(),), (support,)))
        second = replace(first, operation_id='op-2')
        link = replace(examples()[5], operation_id='op-3')
        def propose(request):
            return plan(operations=(first, second, link), base_reference=request.inspection.reference,
                        source_reference=request.source.reference)
        record = self.commit(self.prepare(propose)).integration
        support_events = tuple(e for e in record.events if e.event_type is GoldEventType.PROVENANCE_ADDED)
        self.assertEqual(len(support_events), 1)
        self.assertEqual(support_events[0].operation_ids, ('op-1', 'op-2'))
        relation = next(e for e in record.events if e.event_type is GoldEventType.RELATION_ADDED)
        self.assertEqual(relation.operation_ids, ('op-3',))
        self.assertEqual(record.effects.source_item_accounting, (('source-item-000001', ('op-1', 'op-2', 'op-3')),))
        self.assertNotIn(GoldEventType.NO_CHANGE_RECORDED, {e.event_type for e in record.events})

    def test_subprocess_interruption_before_or_after_selector_and_restart(self):
        script = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from league_knowledge_platform import GoldConfig, prepare_gold_integration, commit_prepared_integration
from league_knowledge_platform import _gold_filesystem as fs
from gold_integration_fixtures import source, FakeAgent, FakeValidator
gold=GoldConfig(Path(sys.argv[1]))
original=fs.replace_selector
def crash(root,content):
    if sys.argv[2]=='after': original(root,content)
    os._exit(73)
with patch('league_knowledge_platform.gold_integration.load_integration_source',return_value=source()), patch('league_knowledge_platform.gold_commit.load_integration_source',return_value=source()):
    prepared=prepare_gold_integration('B',agent=FakeAgent([1]),validator=FakeValidator(),gold_config=gold)
    with patch.object(fs,'replace_selector',crash): commit_prepared_integration(prepared,gold_config=gold)
'''
        for phase in ('before', 'after'):
            gold = GoldConfig(self.root/phase)
            ref = initialize_gold_store(gold)
            staged = stage_gold_snapshot(BASE, config=gold, base=ref)
            publish_gold_snapshot(staged, config=gold, expected_base=ref)
            enable_gold_integration_history(gold)
            env = dict(os.environ, PYTHONPATH=str(Path('tests').absolute()))
            child = subprocess.run([sys.executable, '-c', script, str(gold.root), phase], env=env,
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 73, child.stderr)
            view = load_gold_history(gold)
            self.assertEqual(len(view.integrations), int(phase == 'after'))
            if phase == 'after':
                with patch('league_knowledge_platform.gold_commit.prepare_gold_integration', side_effect=AssertionError):
                    replay = integrate_source_knowledge('B', gold_config=gold)
                self.assertTrue(replay.reused)
            else:
                completed = integrate_source_knowledge('B', agent=FakeAgent([1]), validator=FakeValidator(), gold_config=gold)
                self.assertFalse(completed.reused)
            self.assertEqual(len(load_gold_history(gold).integrations), 1)

    def test_legacy_unaccounted_document_mutation_rejected_before_any_write(self):
        prepared = self.prepare()
        document = replace(BASE.documents[0], title='Unaudited rename',
                           version=replace(BASE.documents[0].version, document_version=2))
        candidate = GoldSnapshot((document,))
        corrupt = replace(prepared, candidate_snapshot=candidate, candidate_sha256=logical_gold_hash(candidate),
                          effects=integration_effects(BASE, candidate, prepared.plan, prepared.source_item_ids),
                          outcome='PREPARED_CHANGE')
        before = self.files()
        with self.assertRaises(Exception):
            self.commit(corrupt)
        self.assertEqual(before, self.files())

    def test_pinned_queries_exact_filters_timing_and_before_after_resolve_without_io(self):
        result = self.commit(self.prepare(4), clock=lambda: '2026-01-01T00:00:00+00:00')
        view = load_gold_history(self.gold)
        record = result.integration
        with patch.object(fs, 'read_bytes', side_effect=AssertionError), patch.object(fs, 'write_exclusive', side_effect=AssertionError):
            self.assertEqual(view.get_integration(record.integration_id), record)
            self.assertEqual(view.query_events(integration_id=record.integration_id), record.events)
            self.assertEqual(view.query_events(source_reference=record.source_reference), record.events)
            self.assertEqual(view.query_events(source_id='B'), record.events)
            self.assertTrue(view.query_events(knowledge_id=identity(2)))
            self.assertEqual(view.query_events(since='2027-01-01T00:00:00+00:00'), ())
            self.assertEqual(view.query_events(until='2026-01-01T00:00:00+00:00'), record.events)
            for event in record.events:
                self.assertEqual({v.knowledge_id for v in view.event_unit_changes(event.event_id)}, set(event.unit_change_ids))
        with self.assertRaises(GoldHistoryError):
            view.query_events(since='2027-01-01T00:00:00+00:00', until='2026-01-01T00:00:00+00:00')

    def test_history_corruption_fails_gold_and_replay_without_repair(self):
        self.commit(self.prepare(1))
        selector = json.loads((self.gold.root/'CURRENT.json').read_bytes())
        path = self.gold.root/'history'/f"{selector['integration_history']['commit_id']}.json"
        original = path.read_bytes()
        path.write_bytes(original+b' ')
        before = self.files()
        for read in (lambda: load_gold_history(self.gold), lambda: load_gold_snapshot(self.gold),
                     lambda: integrate_source_knowledge('B', gold_config=self.gold)):
            with self.assertRaises(GoldHistoryError):
                read()
        self.assertEqual(before, self.files())

    def test_rehashed_invalid_record_is_not_accepted(self):
        self.commit(self.prepare(1))
        selector_path = self.gold.root/'CURRENT.json'
        selector = json.loads(selector_path.read_bytes())
        path = self.gold.root/'history'/f"{selector['integration_history']['commit_id']}.json"
        original = json.loads(path.read_bytes())
        import copy
        def missing_events(node): node['integration']['events'] = []
        def bad_effect(node): node['integration']['effects']['units'] = []
        def bad_schema(node): node['integration']['schema_version'] = 2
        def bad_round(node): node['integration']['rounds'][0]['decision'] = 'REJECT'
        def changed_structure(node): node['integration']['operations'][0]['new_placements'][0]['placement']['position'] = 0
        def extra_field(node): node['integration']['unexpected'] = True
        def bad_metadata(node): node['integration']['events'][0]['integration_id'] = 'integration-'+'0'*32
        def boolean_version(node): node['integration']['effects']['documents'][0]['previous_version'] = True
        def forged_origin(node): node['integration']['operations'][0]['support'] = []
        def wrong_gold_head(node): node['gold_after'] = node['gold_before']
        def duplicate_support(node): node['integration']['operations'][0]['support'] *= 2
        for mutate in (missing_events, bad_effect, bad_schema, bad_round, changed_structure, extra_field,
                       bad_metadata, boolean_version, forged_origin, wrong_gold_head, duplicate_support):
            node = copy.deepcopy(original)
            mutate(node)
            content = encode_history(node)
            path.write_bytes(content)
            selector['integration_history']['sha256'] = sha256(content).hexdigest()
            selector_path.write_bytes(encode_history(selector))
            with self.subTest(mutation=mutate.__name__), self.assertRaises(GoldHistoryError):
                load_gold_history(self.gold)

    def test_selected_history_rejects_missing_historical_gold_and_truncated_or_duplicate_json(self):
        self.commit(self.prepare(1))
        manifest = self.gold.root/'snapshots'/self.base.storage_id/'manifest.json'
        original_manifest = manifest.read_bytes()
        manifest.write_bytes(b'broken')
        with self.assertRaises(Exception):
            load_gold_history(self.gold)
        manifest.write_bytes(original_manifest)
        path = self.gold.root/'CURRENT.json'
        selector = json.loads(path.read_bytes())
        node = self.gold.root/'history'/f"{selector['integration_history']['commit_id']}.json"
        raw = node.read_bytes()
        for malformed in (raw[:30], raw.replace(b'{', b'{"commit_id":"duplicate",', 1)):
            node.write_bytes(malformed)
            selector['integration_history']['sha256'] = sha256(malformed).hexdigest()
            path.write_bytes(encode_history(selector))
            with self.assertRaises(GoldHistoryError):
                load_gold_history(self.gold)

    def test_atomic_failures_before_publication_keep_old_complete_state_and_retry_once(self):
        prepared = self.prepare(1)
        initial = load_gold_history(self.gold)
        original_write = fs.write_exclusive
        def fail_history(path, content):
            if path.parent.name == 'history':
                path.write_bytes(content[:20])
                raise OSError('synthetic secret failure')
            original_write(path, content)
        for target in ('stage', 'history', 'selector'):
            with self.subTest(target=target), ExitStack() as stack:
                if target == 'stage':
                    stack.enter_context(patch('league_knowledge_platform.gold_commit.stage_gold_snapshot', side_effect=OSError('secret')))
                elif target == 'history':
                    stack.enter_context(patch.object(fs, 'write_exclusive', side_effect=fail_history))
                else:
                    stack.enter_context(patch.object(fs, 'replace_selector', side_effect=OSError('secret')))
                with self.assertRaises(GoldHistoryError) as caught:
                    self.commit(prepared)
                self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(load_gold_history(self.gold), initial)
            self.assertEqual(load_gold_snapshot(self.gold).snapshot, BASE)
        result = self.commit(prepared)
        self.assertFalse(result.reused)
        self.assertEqual(len(load_gold_history(self.gold).integrations), 1)

    def test_observer_sees_old_before_switch_new_after_and_post_switch_error_is_confirmed(self):
        prepared = self.prepare(1)
        old = load_gold_history(self.gold)
        original = fs.replace_selector
        def replace_then_error(root, content):
            self.assertEqual(load_gold_history(self.gold), old)
            self.assertEqual(load_gold_snapshot(self.gold).snapshot, BASE)
            original(root, content)
            self.assertEqual(len(load_gold_history(self.gold).integrations), 1)
            self.assertEqual(load_gold_snapshot(self.gold).snapshot, prepared.candidate_snapshot)
            raise OSError('interrupted after replace')
        with patch.object(fs, 'replace_selector', side_effect=replace_then_error):
            result = self.commit(prepared)
        self.assertEqual(self.commit(prepared).integration.integration_id, result.integration.integration_id)


class InitializationTests(unittest.TestCase):
    def test_explicit_baseline_preserves_legacy_bytes_no_reconstruction_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            gold = GoldConfig(Path(directory)/'gold')
            ref = initialize_gold_store(gold)
            raw = (gold.root/'CURRENT.json').read_bytes()
            with self.assertRaises(GoldHistoryError):
                load_gold_history(gold)
            self.assertEqual(load_gold_snapshot(gold).reference, ref)
            history = enable_gold_integration_history(gold)
            view = load_gold_history(gold)
            self.assertEqual(view.integrations, ())
            self.assertEqual(view.gold_reference, ref)
            self.assertEqual((gold.root/'history'/f'legacy-selector-{ref.storage_id}.json').read_bytes(), raw)
            self.assertEqual(initialize_gold_integration_store(gold), history)
            staged = stage_gold_snapshot(GoldSnapshot(), config=gold, base=ref)
            with self.assertRaises(Exception) as caught:
                publish_gold_snapshot(staged, config=gold, expected_base=ref)
            self.assertEqual(caught.exception.code, 'COORDINATED_COMMIT_REQUIRED')

    def test_unknown_selector_versions_and_malformed_v2_never_auto_migrate(self):
        with tempfile.TemporaryDirectory() as directory:
            gold = GoldConfig(Path(directory)/'gold')
            initialize_gold_store(gold)
            path = gold.root/'CURRENT.json'
            selector = json.loads(path.read_bytes())
            for version in (2, 3, True):
                selector['store_schema_version'] = version
                path.write_bytes(encode_history(selector))
                before = path.read_bytes()
                with self.assertRaises(Exception):
                    initialize_gold_integration_store(gold)
                self.assertEqual(path.read_bytes(), before)


class CanonicalInputTests(unittest.TestCase):
    def test_actual_canonical_source_through_existing_prepare_to_commit_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, path, _ = source_workspace(Path(directory)/'source', 'B')
            original = path.read_bytes()
            gold = GoldConfig(Path(directory)/'gold')
            initialize_gold_integration_store(gold)
            agent, validator = FakeAgent([real_create]), FakeValidator()
            with patch('socket.create_connection', side_effect=AssertionError), patch('sqlite3.connect', side_effect=AssertionError):
                first = integrate_source_knowledge('B', agent=agent, validator=validator, gold_config=gold,
                    context=(('logical_run_id', 'fixture-run'), ('selected_video_id', 'B')), **configs)
                replay = integrate_source_knowledge('B', gold_config=gold, **configs)
            self.assertTrue(replay.reused)
            self.assertEqual(first.integration, replay.integration)
            self.assertEqual(len(agent.calls), 1)
            self.assertEqual(len(validator.calls), 1)
            self.assertEqual(first.integration.context[0], ('logical_run_id', 'fixture-run'))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(load_gold_snapshot(gold).snapshot.documents), 1)
            path.write_bytes(original+b' ')
            with self.assertRaises(Exception):
                integrate_source_knowledge('B', gold_config=gold, **configs)

    def test_canonical_empty_input_durable_terminal_noop_replay_with_zero_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(Path(directory)/'source', empty=True)
            process_source_knowledge('A', validator_capability=SourceValidator([validator_evaluation()]), **configs)
            gold = GoldConfig(Path(directory)/'gold')
            initialize_gold_integration_store(gold)
            before = load_gold_snapshot(gold)
            first = integrate_source_knowledge('A', gold_config=gold, **configs)
            replay = integrate_source_knowledge('A', gold_config=gold, **configs)
            self.assertEqual(first.integration.events[0].reason, 'NO_SOURCE_KNOWLEDGE')
            self.assertEqual(first.integration.operations, ())
            self.assertEqual(first.integration.rounds, ())
            self.assertEqual(first.integration.effects.units, ())
            self.assertEqual(load_gold_snapshot(gold), before)
            self.assertTrue(replay.reused)
            self.assertEqual(first.integration, replay.integration)


if __name__ == '__main__':
    unittest.main()
