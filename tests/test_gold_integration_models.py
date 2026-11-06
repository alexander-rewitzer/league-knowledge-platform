"""Strict immutable preparation schemas, limits, gates and scoped feedback."""

from dataclasses import FrozenInstanceError, replace
import unittest

from league_knowledge_platform import (
    GoldContractError, IntegrationConfig, IntegrationGate, IntegrationIssue,
)
from gold_integration_fixtures import plan, evaluation


class IntegrationModelTests(unittest.TestCase):
    def test_versions_are_distinct_explicit_and_reject_unknown_or_boolean(self):
        for field in ('schema_version','integration_contract_version'):
            for version in (True,0,2,'1',None):
                with self.subTest(field=field,version=version),self.assertRaises(GoldContractError):
                    replace(plan(),**{field:version})

    def test_plan_shape_and_context_are_immutable_and_unique(self):
        for changes in ({'operations':[]},{'operations':()},{'operations':plan().operations*2},
                        {'proposed_layout':[]},{'source_reference':'path'},{'base_reference':None},{'metadata':None}):
            with self.subTest(changes=changes),self.assertRaises(GoldContractError):replace(plan(),**changes)
        with self.assertRaises(FrozenInstanceError):plan().operations=()

    def test_gate_catalogue_and_derived_verdict_cannot_be_overridden(self):
        self.assertEqual(len(IntegrationGate),8)
        good=evaluation()
        self.assertEqual(good.decision,'PASS')
        with self.assertRaises((FrozenInstanceError, TypeError)):good.decision='REJECT'
        with self.assertRaises(GoldContractError):replace(good,gates=good.gates+(good.gates[0],))
        with self.assertRaises(GoldContractError):replace(good.gates[0],verdict='PASS')
        with self.assertRaises(GoldContractError):replace(good.gates[0],issues=(IntegrationIssue('reason','fix'),))

    def test_actionable_feedback_is_required_for_failed_gate(self):
        for changes in ({'reason':''},{'actionable_feedback':' '},{'affected_operation_ids':('op','op')}):
            with self.subTest(changes=changes),self.assertRaises(GoldContractError):
                replace(IntegrationIssue('reason','fix'),**changes)

    def test_round_and_resource_limits_are_positive_bounded_integers(self):
        for limit in (True,0,4,-1,'3'):
            with self.assertRaises(GoldContractError):IntegrationConfig(max_semantic_rounds=limit)
        for name in ('max_inspection_calls','max_inspection_bytes','max_agent_exchanges',
                     'agent_output_limit','validator_output_limit'):
            for value in (True,0,-1,1.5):
                with self.subTest(name=name,value=value),self.assertRaises(GoldContractError):IntegrationConfig(**{name:value})

    def test_new_public_preparation_exports_are_present_without_durable_api(self):
        import league_knowledge_platform as package
        for name in ('IntegrationPlan','PreparedIntegrationResult','prepare_gold_integration',
                     'validate_prepared_integration','DeepSeekIntegrationAgent','DeepSeekIntegrationValidator'):
            self.assertIn(name,package.__all__)
        self.assertEqual(len(package.__all__),len(set(package.__all__)))
        for name in ('commit_integration','replay_integration','IntegrationEvent'):
            self.assertFalse(hasattr(package,name))
