"""Service rounds, pinned bounded inspection, pure prepared results and guarded I/O."""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace, fields
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldContractError, GoldSnapshot, GoldStatus, IntegrationConfig, IntegrationError, 
    IntegrationGate, IntegrationOperationalError, IntegrationValidationFailed, IntegrationVerdict, 
    SourceKnowledgeOutcome, initialize_gold_store, load_gold_snapshot, prepare_gold_integration, 
    publish_gold_snapshot, stage_gold_snapshot, validate_prepared_integration,
)
from league_knowledge_platform import _gold_filesystem as fs
from league_knowledge_platform.gold_integration_input import IntegrationInspection
from gold_integration_fixtures import BASE, BASE_REF, source, plan, evaluation, FakeAgent, FakeValidator
from gold_document_fixtures import document, layout, topic
from test_gold_models import unit


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gold=GoldConfig(Path(self.directory.name)/'gold')
        base=initialize_gold_store(self.gold)
        stage=stage_gold_snapshot(BASE,config=self.gold,base=base)
        self.reference=publish_gold_snapshot(stage,config=self.gold,expected_base=base)
        self.source=source()
        self.agent,self.validator=FakeAgent(),FakeValidator()
        self.loader=patch('league_knowledge_platform.gold_integration.load_integration_source',return_value=self.source)
        self.source_loader=self.loader.start();self.addCleanup(self.loader.stop)

    def run_prepare(self,**kwargs):
        return prepare_gold_integration('B',agent=self.agent,validator=self.validator,gold_config=self.gold,**kwargs)

    def test_pass_round_one_and_immutable_complete_noop_result(self):
        result=self.run_prepare(context=(('logical_run_id','fixture-run'),))
        self.assertEqual(result.outcome,'PREPARED_NO_CHANGE')
        self.assertEqual(result.candidate_snapshot,BASE)
        self.assertEqual(result.base_reference,self.reference)
        self.assertEqual(len(result.rounds),1)
        self.assertEqual(result.plan,self.validator.calls[0].plan)
        self.assertEqual(result.rounds[0].evaluation.decision,'PASS')
        self.assertIs(validate_prepared_integration(result),result)
        self.assertNotIn('integration_id',{f.name for f in fields(result)})
        with self.assertRaises(FrozenInstanceError):result.outcome='x'

    def test_each_operation_succeeds_through_independent_validator(self):
        for n in range(7):
            self.agent=FakeAgent([n]);self.validator=FakeValidator()
            result=self.run_prepare()
            self.assertEqual(len(self.validator.calls),1)
            self.assertEqual(result.would_change_gold,n!=0)
            self.assertEqual(load_gold_snapshot(self.gold).snapshot,BASE)

    def test_reject_then_pass_uses_original_source_same_base_complete_previous_plan(self):
        self.agent=FakeAgent([0,2]);self.validator=FakeValidator([evaluation('KNOWLEDGE_PRESERVATION'),evaluation()])
        result=self.run_prepare()
        first,second=self.agent.calls
        self.assertIs(first.source,second.source)
        self.assertIs(first.inspection,second.inspection)
        self.assertEqual(second.previous_plan,result.rounds[0].plan)
        self.assertEqual(second.feedback.decision,'REJECT')
        self.assertNotEqual(result.plan,result.rounds[0].plan)
        self.assertEqual(len(result.rounds),2)

    def test_three_completed_rejects_exhaust_without_success_or_write(self):
        self.agent=FakeAgent([0,0,0]);self.validator=FakeValidator([evaluation('SOURCE_GROUNDING')]*3)
        with self.assertRaises(IntegrationValidationFailed) as caught:self.run_prepare()
        self.assertEqual(caught.exception.code,'INTEGRATION_VALIDATION_FAILED')
        self.assertEqual(len(caught.exception.rounds),3)
        self.assertEqual(len(self.agent.calls),3)
        self.assertEqual(load_gold_snapshot(self.gold).reference,self.reference)

    def test_operational_failure_aborts_without_retry_or_consuming_failed_round(self):
        for role in ('AGENT','VALIDATOR'):
            self.agent=FakeAgent([0,RuntimeError('secret') if role=='AGENT' else 0])
            self.validator=FakeValidator([evaluation('TARGET_CORRECTNESS'),RuntimeError('secret')])
            with self.subTest(role=role),self.assertRaises(IntegrationOperationalError) as caught:self.run_prepare()
            self.assertEqual(caught.exception.phase,role)
            self.assertEqual(len(caught.exception.rounds),1)
            self.assertEqual(len(self.agent.calls),2)
            self.assertNotIn('secret',str(caught.exception))

    def test_missing_coverage_is_operational_and_does_not_invoke_validator(self):
        self.source_loader.return_value=source('source-item-000001','source-item-000002')
        with self.assertRaises(IntegrationOperationalError) as caught:self.run_prepare()
        self.assertEqual(caught.exception.code,'SOURCE_ITEM_COVERAGE')
        self.assertEqual(caught.exception.rounds,())
        self.assertEqual(self.validator.calls,[])

    def test_every_failed_gate_rejects_and_feedback_ids_are_checked(self):
        for gate in IntegrationGate:
            self.assertEqual(evaluation(gate.value).decision,'REJECT')
        self.validator=FakeValidator([evaluation('TARGET_CORRECTNESS',affected_gold_ids=('unknown',))])
        with self.assertRaises(IntegrationOperationalError) as caught:self.run_prepare()
        self.assertEqual(caught.exception.code,'FEEDBACK_REFERENCE')
        self.assertEqual(caught.exception.rounds,())

    def test_invalid_gate_output_or_contradictory_override_is_not_a_round(self):
        for output in ({'decision':'PASS'},replace(evaluation(),metadata=evaluation().metadata)):
            if isinstance(output,dict):
                self.validator=FakeValidator([output])
                with self.assertRaises(IntegrationOperationalError) as caught:self.run_prepare()
                self.assertEqual(caught.exception.rounds,())
        with self.assertRaises(IntegrationError):replace(evaluation(),gates=evaluation().gates[:-1])
        with self.assertRaises(IntegrationError):replace(evaluation().gates[0],verdict=IntegrationVerdict.FAIL)

    def test_empty_source_no_roles_search_plan_round_events_or_canonical_change(self):
        empty=source();empty.artifact.candidate.items=();empty.artifact.outcome=SourceKnowledgeOutcome.NO_SOURCE_KNOWLEDGE
        self.source_loader.return_value=empty
        with patch.object(IntegrationInspection,'query',side_effect=AssertionError):
            result=self.run_prepare()
        self.assertEqual(result.outcome,'NO_SOURCE_KNOWLEDGE')
        self.assertIsNone(result.plan);self.assertEqual(result.rounds,())
        self.assertFalse(result.would_change_gold)
        self.assertEqual(self.agent.calls,[]);self.assertEqual(self.validator.calls,[])
        self.assertEqual(result.base_reference,self.reference)

    def test_no_writes_sql_events_or_publisher_even_after_pass(self):
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.gold.root.rglob('*') if p.is_file()}
        self.agent=FakeAgent([1])
        with ExitStack() as stack:
            for name in ('write_exclusive','replace_selector','publication_guard','prepare_directory'):
                stack.enter_context(patch.object(fs,name,side_effect=AssertionError))
            stack.enter_context(patch('sqlite3.connect',side_effect=AssertionError))
            stack.enter_context(patch('socket.create_connection',side_effect=AssertionError))
            result=self.run_prepare()
        self.assertTrue(result.would_change_gold)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.gold.root.rglob('*') if p.is_file()})

    def test_current_change_does_not_refresh_pinned_context_or_prepared_base(self):
        def change_current(request):
            base=load_gold_snapshot(self.gold)
            other=replace(BASE.documents[0],title='New title',version=replace(BASE.documents[0].version,document_version=2))
            staged=stage_gold_snapshot(GoldSnapshot((other,)),config=self.gold,base=base.reference)
            publish_gold_snapshot(staged,config=self.gold,expected_base=base.reference)
            return plan(base_reference=request.inspection.reference)
        self.agent=FakeAgent([change_current])
        result=self.run_prepare()
        self.assertEqual(result.base_reference,self.reference)
        self.assertNotEqual(load_gold_snapshot(self.gold).reference,self.reference)
        self.assertEqual(result.candidate_snapshot,BASE)
        self.assertTrue(all(self.reference.manifest_sha256 in s for s in self.validator.calls[0].inspected_context))

    def test_inspection_limits_and_method_whitelist_fail_without_truncation(self):
        for config in (IntegrationConfig(max_inspection_calls=1),IntegrationConfig(max_inspection_bytes=10)):
            with self.subTest(config=config),self.assertRaises(IntegrationOperationalError):self.run_prepare(integration_config=config)
        inspection=IntegrationInspection(self.gold,IntegrationConfig())
        with self.assertRaises(IntegrationError):inspection.query('publish_gold_snapshot')
        self.assertEqual(inspection.calls,0)

    def test_validator_receives_full_before_state_of_targets_in_same_snapshot(self):
        self.run_prepare()
        observations=self.validator.calls[0].inspected_context
        self.assertTrue(any('get_gold_document' in s for s in observations))
        self.assertTrue(any(BASE.documents[0].units[0].content.strip() in s for s in observations))

    def test_inspection_historical_exact_lookup_retains_record(self):
        history=unit(3,status=GoldStatus.MERGED,merged_into=unit().knowledge_id)
        base=load_gold_snapshot(self.gold)
        candidate=GoldSnapshot((replace(BASE.documents[0],historical_units=(history,),version=replace(BASE.documents[0].version,document_version=2)),))
        staged=stage_gold_snapshot(candidate,config=self.gold,base=base.reference)
        publish_gold_snapshot(staged,config=self.gold,expected_base=base.reference)
        inspection=IntegrationInspection(self.gold,IntegrationConfig())
        self.assertEqual(inspection.query('get_gold_unit',knowledge_id=history.knowledge_id).value.unit,history)

    def test_result_tampering_is_detected_by_full_reconstruction(self):
        result=self.run_prepare()
        for kwargs in ({'candidate_sha256':'a'*64},{'base_sha256':'b'*64},{'outcome':'PREPARED_CHANGE'},
                       {'effects':None},{'rounds':()},{'source_item_ids':('missing',)}):
            with self.subTest(kwargs=kwargs),self.assertRaises(GoldContractError):validate_prepared_integration(replace(result,**kwargs))

    def test_missing_or_corrupt_gold_is_not_initialized_or_repaired(self):
        bad=GoldConfig(Path(self.directory.name)/'missing')
        with self.assertRaises(IntegrationOperationalError):
            prepare_gold_integration('B',agent=self.agent,validator=self.validator,gold_config=bad)
        self.assertFalse(bad.root.exists())
        (self.gold.root/'CURRENT.json').write_bytes(b'{bad')
        with self.assertRaises(IntegrationOperationalError):self.run_prepare()

    def test_source_change_during_operation_fails_not_success_or_repair(self):
        self.source_loader.side_effect=[self.source,source('other')]
        with self.assertRaises(IntegrationOperationalError) as caught:self.run_prepare()
        self.assertEqual(caught.exception.code,'SOURCE_CHANGED')
        self.assertEqual(len(caught.exception.rounds),1)

    def test_repeated_preparation_is_not_durable_replay(self):
        self.agent=FakeAgent([0,0]);self.validator=FakeValidator([evaluation(),evaluation()])
        self.run_prepare();self.run_prepare()
        self.assertEqual(len(self.agent.calls),2)
        self.assertEqual(load_gold_snapshot(self.gold).reference,self.reference)

