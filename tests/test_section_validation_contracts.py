"""Five-gate strict schema, local references and semantic rubric contract cases."""

import unittest

from league_knowledge_platform import SectionValidationConfig, SectionValidationOperationalError, evaluate_section
from league_knowledge_platform.section_validation_models import decode_judge_document
from league_knowledge_platform.section_validation_prompts import SECTION_JUDGE_SYSTEM_PROMPT
from section_validation_fixtures import FakeJudge, JUDGE_METADATA, evaluation, judge_document, initial_proposal, section


class SectionValidationContractTests(unittest.TestCase):
    def test_strict_gate_schema_rejects_missing_extra_numeric_and_overall_verdicts(self):
        bad = []
        for gate in judge_document()["gates"]:
            value = judge_document()
            del value["gates"][gate]
            bad.append(value)
        for key in ("overall_verdict", "items", "score", "section_id", "provenance"):
            value = judge_document()
            value[key] = "PASS"
            bad.append(value)
        for verdict in (0, True, "REJECT", "pass", None):
            value = judge_document()
            value["gates"]["FAITHFULNESS"]["verdict"] = verdict
            bad.append(value)
        value = judge_document()
        value["gates"]["QUALITY"] = value["gates"]["FAITHFULNESS"]
        bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                decode_judge_document(value, JUDGE_METADATA)

    def test_failed_gates_require_actionable_issues_pass_has_none_and_completeness_needs_spans(self):
        for alteration in ("no_issues", "empty_reason", "empty_feedback", "missing_spans", "extra_rewrite", "passed_with_issues"):
            value = judge_document("COMPLETENESS")
            gate = value["gates"]["COMPLETENESS"]
            if alteration == "no_issues": gate["issues"] = []
            elif alteration == "empty_reason": gate["issues"][0]["reason"] = ""
            elif alteration == "empty_feedback": gate["issues"][0]["actionable_feedback"] = "  "
            elif alteration == "missing_spans": gate["issues"][0]["relevant_source_spans"] = []
            elif alteration == "extra_rewrite": gate["issues"][0]["replacement_items"] = []
            else: gate["verdict"] = "PASS"
            # Convert fixture tuples to their actual JSON array form.
            import json
            value = json.loads(json.dumps(value))
            with self.subTest(alteration=alteration), self.assertRaises((ValueError, TypeError)):
                decode_judge_document(value, JUDGE_METADATA)

    def test_five_binary_gates_are_application_derived_for_each_single_failure(self):
        unit = section()
        proposal = initial_proposal(unit)
        for gate in judge_document()["gates"]:
            judge = FakeJudge([evaluation(gate)])
            result = evaluate_section(unit, proposal, capability=judge)
            self.assertEqual(result.decision.value, "REJECT")
            self.assertEqual(judge.calls[0].section, unit)
            self.assertEqual(judge.calls[0].proposal, proposal)

    def test_synthetic_semantic_cases_preserve_full_context_and_gate_outcomes(self):
        cases = [
            ("faithful paraphrase", "Usually creates an opportunity.", None),
            ("compression", "Usually wait for the interrupt cooldown.", None),
            ("lexical difference", "An unavailable interrupt usually permits a trade.", None),
            ("unsupported fact", "This also restores health.", "FAITHFULNESS"),
            ("strengthened certainty", "Always guarantees a successful all-in.", "FAITHFULNESS"),
            ("invented causality", "The cooldown directly increases your damage.", "FAITHFULNESS"),
            ("missed knowledge", "Only part of the substantive rule.", "COMPLETENESS"),
            ("repeated filler", "A reusable rule despite repeated filler.", None),
            ("coherent rule and reason", "Wait because the interrupt can stop the approach.", None),
            ("independent claims merged", "Wait for cooldown and use unrelated build advice.", "ATOMICITY"),
            ("preserved matchup phase", "Before level six against Fizz, preserve Charm.", None),
            ("missing matchup phase", "Preserve Charm.", "CONTEXT_PRESERVATION"),
            ("lost exception", "Trade without the source's unless condition.", "CONTEXT_PRESERVATION"),
            ("lost modality", "This always works.", "CONTEXT_PRESERVATION"),
            ("semantic duplicate", "A paraphrased duplicate rule.", "DUPLICATION"),
            ("distinct condition", "Wait only under an additional source-backed condition.", None),
            ("correct evidence", "Source-grounded rule.", None),
            ("wrong evidence", "Claim appears elsewhere but not in its evidence.", "FAITHFULNESS"),
            ("insufficient evidence", "Broader claim than the cited evidence.", "FAITHFULNESS"),
        ]
        unit = section()
        for label, statement, failed_gate in cases:
            proposal = initial_proposal(unit, statement)
            judge = FakeJudge([evaluation(*((failed_gate,) if failed_gate else ()))])
            with self.subTest(case=label):
                result = evaluate_section(unit, proposal, capability=judge)
                self.assertEqual(result.decision.value, "REJECT" if failed_gate else "PASS")
                self.assertEqual(judge.calls[0].proposal.items[0].statement, statement)
                self.assertEqual(judge.calls[0].section.segments, unit.segments)

    def test_prompt_has_material_tolerance_all_rubrics_evidence_and_no_source_correction(self):
        for phrase in ("NOT wording equality", "paraphrasing", "compression", "clearer wording", "abstraction",
                       "pedantic", "lexical differences", "CITED evidence", "wrong or insufficient", "not every",
                       "NO_KNOWLEDGE_PROPOSED", "maximum fragmentation", "phase", "cooldowns", "exceptions",
                       "modality", "comparisons", "semantic", "Different", "correct source facts", "diagnose",
                       "all proposed", "untrusted", "no overall", "complete content section"):
            self.assertIn(phrase.lower(), SECTION_JUDGE_SYSTEM_PROMPT.lower())

    def test_round_limit_and_judge_output_config_cannot_hide_a_fourth_round(self):
        for kwargs in ({"max_semantic_rounds": 4}, {"max_semantic_rounds": 0}, {"max_semantic_rounds": True},
                       {"judge_output_limit": 0}, {"root": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SectionValidationConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
