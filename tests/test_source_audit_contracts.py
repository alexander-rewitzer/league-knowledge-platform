"""Strict complete candidates, trusted lineage, seven gates and semantic rubric fixtures."""

from dataclasses import replace
import tempfile
import unittest

from league_knowledge_platform import (
    SourceAuditGate, SourceAuditOperationalError, SourceConflictDraft, SourceAuditConfig, SourceKnowledgeOutcome,
    evaluate_source_candidate,
)
from league_knowledge_platform.source_audit import candidate_from_response, validate_source_candidate
from league_knowledge_platform.source_audit_prompts import SOURCE_AUDIT_SYSTEM_PROMPT, SOURCE_VALIDATOR_SYSTEM_PROMPT
from league_knowledge_platform.source_knowledge import decode_source_evaluation
from source_audit_fixtures import (
    FakeValidator, VALIDATOR_METADATA, audit_response, validator_document, validator_evaluation, workspace,
)


class SourceAuditContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.configs, self.inputs = workspace(self.temp.name)
        self.candidate = candidate_from_response(audit_response(), self.inputs)

    def test_many_to_one_constructs_application_ids_complete_lineage_and_exact_evidence(self):
        item = self.candidate.items[0]
        self.assertEqual(self.candidate.outcome, SourceKnowledgeOutcome.KNOWLEDGE_AVAILABLE)
        self.assertEqual(item.item_id, 'source-item-000001')
        self.assertEqual(item.derived_from, tuple(i.reference for i in self.inputs.items))
        self.assertEqual([e.section_id for e in item.evidence], ['section-000001', 'section-000002'])
        self.assertEqual(item.evidence, tuple(i.knowledge.evidence[0] for i in self.inputs.items))

    def test_missing_unknown_duplicate_and_split_input_accounting_fail(self):
        bad = [audit_response(('Lost', ('input-000001',))),
               audit_response(('Unknown', ('input-000001', 'input-000003'))),
               audit_response(('A', ('input-000001', 'input-000002')), ('B', ('input-000001',)))]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(ValueError): candidate_from_response(response, self.inputs)
        from league_knowledge_platform import SourceItemDraft
        with self.assertRaises(ValueError): SourceItemDraft('Duplicate', ('input-000001', 'input-000001'))

    def test_provider_order_cannot_reorder_trusted_contributor_evidence(self):
        result = candidate_from_response(audit_response(('Equivalent', ('input-000002', 'input-000001'))), self.inputs)
        self.assertEqual(result.items[0].derived_from, self.candidate.items[0].derived_from)
        self.assertEqual(result.items[0].evidence, self.candidate.items[0].evidence)

    def test_tampered_evidence_or_fake_local_id_rejected(self):
        item = self.candidate.items[0]
        for changed in (replace(item, item_id='source-item-000002'), replace(item, evidence=item.evidence[:1]),
                        replace(item, derived_from=tuple(reversed(item.derived_from)))):
            with self.assertRaises(ValueError): validate_source_candidate(replace(self.candidate, items=(changed,)), self.inputs)

    def test_conflicts_preserve_separate_items_with_application_owned_identity(self):
        response = audit_response(('Trade now', ('input-000001',)), ('Never trade now', ('input-000002',)),
                                  conflicts=(((2, 1), 'Incompatible under equivalent stated conditions.'),))
        result = candidate_from_response(response, self.inputs)
        self.assertEqual(result.conflicts[0].item_ids, ('source-item-000001', 'source-item-000002'))
        self.assertEqual(result.conflicts[0].conflict_id, 'conflict-000001')
        for positions in ((1, 3),):
            with self.assertRaises(ValueError):
                candidate_from_response(replace(response, conflicts=(SourceConflictDraft(positions, 'Invalid'),)), self.inputs)
        with self.assertRaises(ValueError): candidate_from_response(replace(response, conflicts=response.conflicts * 2), self.inputs)

    def test_seven_binary_gates_cannot_be_overridden_and_each_can_fail(self):
        for gate in SourceAuditGate:
            result = evaluate_source_candidate(self.inputs, self.candidate,
                capability=FakeValidator([validator_evaluation(gate.value)]), output_limit=100)
            self.assertEqual(result.decision.value, 'REJECT')
        self.assertEqual(evaluate_source_candidate(self.inputs, self.candidate,
            capability=FakeValidator([validator_evaluation()]), output_limit=100).decision.value, 'PASS')

    def test_strict_schema_missing_extra_numeric_duplicate_diagnostics_and_rewrites(self):
        bad = []
        for gate in SourceAuditGate:
            value = validator_document(); del value['gates'][gate.value]; bad.append(value)
        for field in ('overall', 'score', 'items', 'patches', 'verdict'):
            value = validator_document(); value[field] = 'PASS'; bad.append(value)
        for verdict in ('pass', 'REJECT', 1, True, None):
            value = validator_document(); value['gates']['SOURCE_FAITHFULNESS']['verdict'] = verdict; bad.append(value)
        for field in ('reason', 'actionable_feedback'):
            value = validator_document('NO_KNOWLEDGE_LOSS'); value['gates']['NO_KNOWLEDGE_LOSS']['issues'][0][field] = ''; bad.append(value)
        value = validator_document('NO_KNOWLEDGE_LOSS'); value['gates']['NO_KNOWLEDGE_LOSS']['issues'] = []; bad.append(value)
        value = validator_document('NO_KNOWLEDGE_LOSS'); value['gates']['NO_KNOWLEDGE_LOSS']['verdict'] = 'PASS'; bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)): decode_source_evaluation(value, VALIDATOR_METADATA)

    def test_invalid_issue_references_fail_operationally_not_semantically(self):
        for kwargs in ({'source_ids': ('source-item-999999',)}, {'input_refs': (('section-000003', 'item-000001'),)},
                       {'evidence': (('section-000003', ((12, 12),)),)}, {'evidence': (('section-000001', ((0, 6),)),)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceAuditOperationalError):
                evaluate_source_candidate(self.inputs, self.candidate,
                    capability=FakeValidator([validator_evaluation('NO_KNOWLEDGE_LOSS', **kwargs)]), output_limit=100)

    def test_synthetic_semantic_cases_check_complete_context_and_expected_gate_flow(self):
        cases = [('faithful paraphrase', None), ('compression', None), ('lexical duplicate consolidated', None),
                 ('meaningful conditions preserved', None), ('coherent rule plus reason', None),
                 ('invented causality', 'SOURCE_FAITHFULNESS'), ('external source correction', 'SOURCE_FAITHFULNESS'),
                 ('condition loss', 'NO_KNOWLEDGE_LOSS'), ('exception loss', 'NO_KNOWLEDGE_LOSS'),
                 ('modality change', 'NO_KNOWLEDGE_LOSS'), ('semantic duplicate remains', 'CROSS_SECTION_DEDUPLICATION'),
                 ('unrelated claims merged', 'COHERENCE_ATOMICITY'), ('artificial harmonization', 'CONFLICT_PRESERVATION'),
                 ('unsupported evidence', 'PROVENANCE_INTEGRITY')]
        for label, gate in cases:
            validator = FakeValidator([validator_evaluation(*(() if gate is None else (gate,)))])
            with self.subTest(label=label):
                result = evaluate_source_candidate(self.inputs, self.candidate, capability=validator, output_limit=100)
                self.assertEqual(result.decision.value, 'PASS' if gate is None else 'REJECT')
                self.assertEqual(validator.calls[0].inputs, self.inputs)
                self.assertEqual(validator.calls[0].candidate, self.candidate)

    def test_prompts_include_contrasting_examples_no_second_extraction_and_untrusted_data(self):
        for phrase in ('untrusted', 'COMPLETE', 'EXACTLY ONCE', 'No second extraction', 'modality', 'Gold', 'because', 'conflict', 'different'):
            self.assertIn(phrase.lower(), SOURCE_AUDIT_SYSTEM_PROMPT.lower())
        for phrase in ('untrusted', 'NOT lexical', 'UPSTREAM_COVERAGE_DEFECT', 'ALL SEVEN', 'No overall', 'unrelated greeting', 'usually', 'guarantees', 'Different preconditions'):
            self.assertIn(phrase.lower(), SOURCE_VALIDATOR_SYSTEM_PROMPT.lower())
        for gate in SourceAuditGate: self.assertIn(gate.value, SOURCE_VALIDATOR_SYSTEM_PROMPT)

    def test_configuration_cannot_introduce_fourth_round_or_zero_resource_allowance(self):
        for kwargs in ({'max_semantic_rounds': 4}, {'max_semantic_rounds': True}, {'max_semantic_rounds': 0},
                       {'audit_output_limit': 0}, {'validator_output_limit': 0}, {'root': ''}):
            with self.assertRaises(ValueError): SourceAuditConfig(**kwargs)


if __name__ == '__main__':
    unittest.main()
