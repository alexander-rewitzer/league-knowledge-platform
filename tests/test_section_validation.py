"""Completed semantic rounds, failure isolation and full-proposal refinement."""

from dataclasses import replace
import tempfile
import unittest

from league_knowledge_platform import (
    ExtractionCapabilityError, SectionJudgeCapabilityError, SectionValidationExhaustedError,
    SectionValidationInputError, SectionValidationOperationalError, canonical_section_knowledge_path,
    evaluate_section, process_section_knowledge,
)
from section_validation_fixtures import (
    FakeExtractor, FakeJudge, evaluation, initial_proposal, response, section, workspace,
)


class SectionValidationTests(unittest.TestCase):
    def test_pass_on_rounds_one_two_and_three_counts_only_completed_pairs(self):
        for rounds in (1, 2, 3):
            with self.subTest(rounds=rounds), tempfile.TemporaryDirectory() as directory:
                configs, unit = workspace(directory)
                extractor = FakeExtractor([response("First complete proposal"), response("Second complete proposal"), response("Third complete proposal")])
                judge = FakeJudge([evaluation("CONTEXT_PRESERVATION")] * (rounds - 1) + [evaluation()])
                result = process_section_knowledge("A", unit.section_id, extraction_capability=extractor,
                                                    judge_capability=judge, **configs)
                self.assertEqual(result.operation_rounds_used, rounds)
                self.assertEqual(result.checkpoint.semantic_rounds_used, rounds)
                self.assertEqual(len(judge.calls), rounds)
                self.assertEqual(len(extractor.calls), rounds)
                for n, request in enumerate(extractor.calls[1:], 1):
                    self.assertEqual(request.segments, unit.segments)
                    self.assertEqual(request.refinement.previous_proposal, judge.calls[n - 1].proposal)
                    self.assertTrue(request.refinement.feedback)
                self.assertTrue(result.created)
                self.assertEqual(len(result.checkpoint.accepted_proposal.items), 1)

    def test_supplied_proposal_avoids_initial_extraction_and_three_rejects_never_call_fourth(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            extractor = FakeExtractor([response("Replacement one"), response("Replacement two"), response("Forbidden fourth")])
            judge = FakeJudge([evaluation("FAITHFULNESS")] * 4)
            with self.assertRaises(SectionValidationExhaustedError) as caught:
                process_section_knowledge("A", unit.section_id, initial_proposal=initial_proposal(unit),
                                          extraction_capability=extractor, judge_capability=judge, **configs)
            self.assertEqual(caught.exception.kind, "SECTION_VALIDATION_FAILED")
            self.assertEqual(caught.exception.semantic_rounds_used, 3)
            self.assertEqual(len(judge.calls), 3)
            self.assertEqual(len(extractor.calls), 2)
            self.assertFalse(canonical_section_knowledge_path("A", unit.section_id, configs["config"]).exists())

    def test_extraction_and_judge_failures_do_not_consume_a_failed_round_or_retry(self):
        for phase, completed in (("EXTRACTION", 0), ("JUDGE", 0), ("REFINEMENT", 1), ("JUDGE", 1)):
            with self.subTest(phase=phase, completed=completed), tempfile.TemporaryDirectory() as directory:
                configs, unit = workspace(directory)
                extraction_error = ExtractionCapabilityError("TIMEOUT", retryable=True)
                judge_error = SectionJudgeCapabilityError("RATE_LIMIT", retryable=True)
                es = [extraction_error] if phase == "EXTRACTION" else [response("Original"), extraction_error if phase == "REFINEMENT" else response("Replacement")]
                js = [evaluation("FAITHFULNESS"), judge_error] if completed else [judge_error]
                extractor, judge = FakeExtractor(es), FakeJudge(js)
                with self.assertRaises(SectionValidationOperationalError) as caught:
                    process_section_knowledge("A", unit.section_id, extraction_capability=extractor, judge_capability=judge, **configs)
                self.assertEqual(caught.exception.semantic_rounds_used, completed)
                self.assertEqual(caught.exception.phase, phase)
                self.assertFalse(canonical_section_knowledge_path("A", unit.section_id, configs["config"]).exists())
                self.assertEqual(len(extractor.calls), completed + 1)
                self.assertEqual(len(judge.calls), 0 if phase == "EXTRACTION" else completed if phase == "REFINEMENT" else completed + 1)

    def test_malformed_judge_and_wrong_proposal_references_are_operational_not_reject(self):
        invalids = [None, {}, evaluation("FAITHFULNESS", item_ids=("item-999999",)),
                    evaluation("COMPLETENESS", spans=((0, 6),)), evaluation("FAITHFULNESS", spans=((3, 0),))]
        for invalid in invalids:
            with self.subTest(kind=type(invalid)), tempfile.TemporaryDirectory() as directory:
                configs, unit = workspace(directory)
                judge = FakeJudge([invalid, evaluation()])
                with self.assertRaises(SectionValidationOperationalError) as caught:
                    process_section_knowledge("A", unit.section_id, initial_proposal=initial_proposal(unit), judge_capability=judge, **configs)
                self.assertEqual(caught.exception.semantic_rounds_used, 0)
                self.assertEqual(len(judge.calls), 1)

    def test_zero_items_are_judged_then_persisted_or_refined(self):
        for reject_first in (False, True):
            with self.subTest(reject_first=reject_first), tempfile.TemporaryDirectory() as directory:
                configs, unit = workspace(directory)
                extractor = FakeExtractor([response(), response("Previously missed knowledge")])
                judge = FakeJudge(([evaluation("COMPLETENESS", item_ids=())] if reject_first else []) + [evaluation()])
                result = process_section_knowledge("A", unit.section_id, extraction_capability=extractor, judge_capability=judge, **configs)
                self.assertEqual(len(result.checkpoint.accepted_proposal.items), int(reject_first))
                self.assertEqual(result.operation_rounds_used, 2 if reject_first else 1)
                self.assertEqual(judge.calls[0].proposal.items, ())
                self.assertEqual(judge.calls[0].section, unit)

    def test_new_proposal_replaces_all_items_and_feedback_ids_belong_to_current_round(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            extractor = FakeExtractor([response("Only new statement")])
            previous = initial_proposal(unit, "Old one", "Old two")
            judge = FakeJudge([evaluation("DUPLICATION", item_ids=("item-000002",)), evaluation()])
            result = process_section_knowledge("A", unit.section_id, initial_proposal=previous,
                                               extraction_capability=extractor, judge_capability=judge, **configs)
            self.assertEqual(result.checkpoint.accepted_proposal.items[0].statement, "Only new statement")
            self.assertEqual(len(judge.calls[1].proposal.items), 1)
            self.assertEqual(extractor.calls[0].refinement.previous_proposal, previous)

    def test_mismatched_provenance_fails_before_judge_and_roots_must_be_disjoint(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            proposal = replace(initial_proposal(unit), section_id="section-000002")
            judge = FakeJudge([evaluation()])
            with self.assertRaises(SectionValidationInputError):
                process_section_knowledge("A", unit.section_id, initial_proposal=proposal, judge_capability=judge, **configs)
            self.assertEqual(judge.calls, [])
            from league_knowledge_platform import SectionValidationIntegrityError
            configs['config'] = replace(configs['config'], root=configs['silver_config'].root)
            with self.assertRaises(SectionValidationIntegrityError):
                process_section_knowledge('A', unit.section_id, judge_capability=judge, **configs)
            self.assertEqual(judge.calls, [])

    def test_standalone_evaluator_derives_overall_and_never_rewrites_items(self):
        unit = section()
        proposal = initial_proposal(unit)
        for failed, expected in (((), "PASS"), (("DUPLICATION",), "REJECT")):
            result = evaluate_section(unit, proposal, capability=FakeJudge([evaluation(*failed)]))
            self.assertEqual(result.decision.value, expected)
            self.assertFalse(hasattr(result, "items"))
            self.assertEqual(proposal.items[0].item_id, "item-000001")

    def test_capability_gate_order_is_not_a_different_semantic_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            original = evaluation()
            reordered = replace(original, gates=tuple(reversed(original.gates)))
            result = process_section_knowledge('A', unit.section_id, initial_proposal=initial_proposal(unit),
                judge_capability=FakeJudge([reordered]), **configs)
            self.assertTrue(result.created)
            self.assertEqual(result.checkpoint.rounds[0].evaluation.gates, original.gates)


if __name__ == "__main__":
    unittest.main()
