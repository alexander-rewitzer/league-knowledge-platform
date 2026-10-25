"""Completed semantic rounds, full replacement, empty generation and explicit failures."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from league_knowledge_platform import (
    SourceAuditCapabilityError, SourceAuditExhaustedError, SourceAuditOperationalError,
    SourceAuditUpstreamCoverageError, SourceIssueKind, process_source_knowledge,
)
from source_audit_fixtures import FakeAudit, FakeValidator, audit_response, validator_evaluation, workspace


class SourceAuditTests(unittest.TestCase):
    def test_pass_round_one_two_three_uses_full_original_inputs_and_complete_previous_candidate(self):
        for pass_round in (1, 2, 3):
            with self.subTest(pass_round=pass_round), tempfile.TemporaryDirectory() as directory:
                configs, inputs = workspace(directory)
                responses = [audit_response((f'Complete candidate {n}', ('input-000001', 'input-000002'))) for n in range(pass_round)]
                agent = FakeAudit(responses)
                validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS')] * (pass_round-1) + [validator_evaluation()])
                result = process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
                self.assertEqual(result.operation_rounds_used, pass_round)
                self.assertEqual(result.artifact.rounds_used, pass_round)
                self.assertEqual((len(agent.calls), len(validator.calls)), (pass_round, pass_round))
                self.assertIsNone(agent.calls[0].refinement)
                for n, call in enumerate(agent.calls):
                    self.assertEqual(call.inputs, inputs)
                    self.assertEqual(len(call.inputs.items), 2)
                    if n:
                        self.assertEqual(call.refinement.previous_candidate, validator.calls[n-1].candidate)
                        self.assertEqual(call.refinement.evaluation.decision.value, 'REJECT')
                self.assertEqual(result.artifact.candidate.items[0].statement, f'Complete candidate {pass_round-1}')

    def test_three_rejects_fail_no_fourth_call_or_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory)
            agent = FakeAudit([audit_response()] * 4)
            validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS')] * 4)
            with self.assertRaises(SourceAuditExhaustedError) as caught:
                process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
            self.assertEqual(caught.exception.kind, 'SOURCE_AUDIT_FAILED')
            self.assertEqual(caught.exception.semantic_rounds_used, 3)
            self.assertEqual((len(agent.calls), len(validator.calls)), (3, 3))
            self.assertFalse(Path(configs['config'].root).exists())

    def test_agent_failure_and_invalid_candidate_do_not_consume_failed_round(self):
        for failure in (SourceAuditCapabilityError('TIMEOUT', retryable=True), RuntimeError('sensitive'),
                        audit_response(('Missing input', ('input-000001',)))):
            with self.subTest(failure=type(failure)), tempfile.TemporaryDirectory() as directory:
                configs, _ = workspace(directory)
                agent = FakeAudit([audit_response(), failure, audit_response()])
                validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS')])
                with self.assertRaises(SourceAuditOperationalError) as caught:
                    process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
                self.assertEqual(caught.exception.semantic_rounds_used, 1)
                self.assertEqual(caught.exception.phase, 'AUDIT')
                self.assertNotIn('sensitive', str(caught.exception))
                self.assertEqual((len(agent.calls), len(validator.calls)), (2, 1))

    def test_validator_failure_or_malformed_result_never_becomes_semantic_reject(self):
        for failure in (SourceAuditCapabilityError('CONTEXT_LIMIT', retryable=False), object(),
                        validator_evaluation('NO_KNOWLEDGE_LOSS', source_ids=('source-item-999999',))):
            with self.subTest(failure=type(failure)), tempfile.TemporaryDirectory() as directory:
                configs, _ = workspace(directory)
                agent = FakeAudit([audit_response()] * 3)
                validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS'), failure, validator_evaluation()])
                with self.assertRaises(SourceAuditOperationalError) as caught:
                    process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
                self.assertEqual(caught.exception.semantic_rounds_used, 1)
                self.assertEqual(caught.exception.phase, 'VALIDATOR')
                self.assertEqual((len(agent.calls), len(validator.calls)), (2, 2))
                self.assertFalse(Path(configs['config'].root).exists())

    def test_upstream_missing_knowledge_escalates_after_valid_reject_without_reextraction(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory)
            agent = FakeAudit([audit_response(), audit_response()])
            validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS', kind=SourceIssueKind.UPSTREAM_COVERAGE_DEFECT,
                input_refs=(), reason='Substantive source claim is absent from all validated input items.')])
            with self.assertRaises(SourceAuditUpstreamCoverageError) as caught:
                process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 1)
            self.assertEqual((len(agent.calls), len(validator.calls)), (1, 1))
            self.assertFalse(Path(configs['config'].root).exists())

    def test_empty_inputs_use_no_agent_but_still_require_seven_gate_pass(self):
        for upstream in ('content', 'ads', 'empty'):
            with self.subTest(upstream=upstream), tempfile.TemporaryDirectory() as directory:
                configs, _ = workspace(directory, empty=True, upstream=upstream)
                agent, validator = FakeAudit([]), FakeValidator([validator_evaluation()])
                result = process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
                self.assertEqual((agent.calls, len(validator.calls)), ([], 1))
                self.assertEqual(result.artifact.candidate.items, ())
                self.assertEqual(result.artifact.candidate.conflicts, ())
                self.assertEqual(result.artifact.outcome.value, 'NO_SOURCE_KNOWLEDGE')
                self.assertIsNone(result.artifact.candidate.audit_metadata)
                self.assertEqual(result.operation_rounds_used, 1)

    def test_empty_rejects_never_invoke_agent_or_bypass_round_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            agent = FakeAudit([])
            validator = FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS', source_ids=(), input_refs=())] * 4)
            with self.assertRaises(SourceAuditExhaustedError) as caught:
                process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 3)
            self.assertEqual((len(agent.calls), len(validator.calls)), (0, 3))

    def test_empty_validator_failure_needs_no_agent_and_has_no_durable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            with self.assertRaises(SourceAuditOperationalError) as caught:
                process_source_knowledge('A', **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 0)
            self.assertFalse(Path(configs['config'].root).exists())

    def test_empty_upstream_coverage_defect_escalates_without_invented_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            agent, validator = FakeAudit([]), FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS',
                kind=SourceIssueKind.UPSTREAM_COVERAGE_DEFECT, source_ids=(), input_refs=())])
            with self.assertRaises(SourceAuditUpstreamCoverageError) as caught:
                process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 1)
            self.assertEqual((len(agent.calls), len(validator.calls)), (0, 1))
            self.assertFalse(Path(configs['config'].root).exists())

    def test_explicit_smaller_budget_is_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory)
            configs['config'] = replace(configs['config'], max_semantic_rounds=1)
            with self.assertRaises(SourceAuditExhaustedError) as caught:
                process_source_knowledge('A', audit_capability=FakeAudit([audit_response()]),
                    validator_capability=FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS')]), **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 1)


if __name__ == '__main__':
    unittest.main()
