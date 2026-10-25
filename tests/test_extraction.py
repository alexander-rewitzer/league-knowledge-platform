"""Transient extraction, evidence validation and future-validator boundary."""

from dataclasses import FrozenInstanceError, replace
import unittest

from league_knowledge_platform import (
    EvidenceSpan, ExtractionCapabilityError, ExtractionConfig, ExtractionInputError,
    ExtractionOutcome, ExtractionValidationError, KnowledgeEvidence,
    extract_knowledge, validate_extraction_proposal,
)
from league_knowledge_platform.extraction_prompts import EXTRACTION_SYSTEM_PROMPT
from extraction_http_fixtures import FakeExtraction, METADATA, response, section


class ExtractionTests(unittest.TestCase):
    def test_ids_and_section_provenance_are_attached_only_by_application(self):
        unit = section()
        provider = FakeExtraction([response("Usually trade only if the interrupt is unavailable.",
                                            "The interrupt can stop an approach.")])
        result = extract_knowledge(unit, capability=provider)
        self.assertEqual([i.item_id for i in result.items], ["item-000001", "item-000002"])
        self.assertEqual(result.provenance, unit.provenance)
        self.assertEqual(result.section_id, unit.section_id)
        self.assertEqual(result.metadata, METADATA)
        self.assertEqual(result.items[0].evidence, (KnowledgeEvidence(unit.section_id, (EvidenceSpan(0, 3),)),))
        self.assertEqual(result.outcome, ExtractionOutcome.ITEMS_PROPOSED)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(hasattr(provider.calls[0], "section_id"))

    def test_multiple_spans_with_gapped_indices_and_original_statement_preserved(self):
        statement = " Usually trade only if available; otherwise wait. "
        result = extract_knowledge(section(), capability=FakeExtraction([response(statement, spans=((0, 0), (3, 3)))]))
        self.assertEqual(result.items[0].statement, statement)
        self.assertEqual(result.items[0].evidence[0].spans, (EvidenceSpan(0, 0), EvidenceSpan(3, 3)))
        self.assertEqual(result.contract_version, 1)

    def test_unknown_missing_interior_outside_reversed_and_boolean_indices_rejected(self):
        for endpoints in ((1, 3), (0, 2), (0, 6), (3, 0), (3, 99)):
            provider = FakeExtraction([response("Rule", spans=(endpoints,))])
            with self.subTest(endpoints=endpoints), self.assertRaises(ExtractionValidationError):
                extract_knowledge(section(), capability=provider)
            self.assertEqual(len(provider.calls), 1)
        for bad in (True, -1, "0", 0.5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                EvidenceSpan(bad, 3)

    def test_zero_items_remain_an_unaccepted_proposal_for_whole_section_validator(self):
        unit = section("Hello everyone", "Thanks for watching", "Buy a product")
        result = extract_knowledge(unit, capability=FakeExtraction([response()]))
        self.assertEqual(result.outcome, ExtractionOutcome.NO_KNOWLEDGE_PROPOSED)
        self.assertEqual(result.items, ())
        self.assertEqual(result.provenance, unit.provenance)
        self.assertIn("Hello", unit.text)
        for forbidden in ("accepted", "passed", "confidence", "canonical", "path"):
            self.assertFalse(hasattr(result, forbidden))

    def test_repeated_calls_have_no_cache_first_result_priority_or_durable_identity(self):
        unit = section()
        provider = FakeExtraction([response("First proposal"), response("Different proposal"), response()])
        results = [extract_knowledge(unit, capability=provider) for _ in range(3)]
        self.assertEqual(len(provider.calls), 3)
        self.assertNotEqual(results[0], results[1])
        self.assertEqual(results[0].items[0].item_id, results[1].items[0].item_id)
        self.assertEqual(results[2].outcome, ExtractionOutcome.NO_KNOWLEDGE_PROPOSED)

    def test_empty_content_section_still_produces_an_llm_proposal_not_an_upstream_terminal_outcome(self):
        unit = section("", "", "Nonempty advertisement")
        provider = FakeExtraction([response()])
        result = extract_knowledge(unit, capability=provider)
        self.assertEqual(unit.text, "")
        self.assertEqual(result.outcome, ExtractionOutcome.NO_KNOWLEDGE_PROPOSED)
        self.assertEqual(len(provider.calls), 1)

    def test_identical_statements_rejected_without_semantic_merge_or_silent_drop(self):
        with self.assertRaises(ExtractionValidationError):
            extract_knowledge(section(), capability=FakeExtraction([response("Same rule", "Same rule")]))
        result = extract_knowledge(section(), capability=FakeExtraction([response(
            "Trade only if ability A is unavailable.", "Trade only if ability B is unavailable.",
            "The source also recommends not trading in that situation.")]))
        self.assertEqual(len(result.items), 3)

    def test_failures_never_become_empty_proposals_and_are_not_retried(self):
        for kind in ("TIMEOUT", "NETWORK", "RATE_LIMIT", "SERVICE", "AUTHENTICATION", "CONTEXT_LIMIT", "OUTPUT_TRUNCATED"):
            provider = FakeExtraction([ExtractionCapabilityError(kind, retryable=True), response()])
            with self.subTest(kind=kind), self.assertRaises(ExtractionCapabilityError) as caught:
                extract_knowledge(section(), capability=provider)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(len(provider.calls), 1)
        provider = FakeExtraction([RuntimeError("private text and credentials")])
        with self.assertRaises(ExtractionCapabilityError) as caught:
            extract_knowledge(section(), capability=provider)
        self.assertEqual(caught.exception.kind, "PROVIDER_CONTRACT")
        self.assertNotIn("private", str(caught.exception))

    def test_wrong_or_mutated_response_is_rejected_as_a_whole(self):
        mutated = response("Rule")
        object.__setattr__(mutated, "outcome", ExtractionOutcome.NO_KNOWLEDGE_PROPOSED)
        wrong_prompt = replace(response("Rule"), metadata=replace(METADATA, prompt_version="other"))
        for invalid in (None, {}, mutated, wrong_prompt):
            with self.subTest(invalid=type(invalid)), self.assertRaises(ExtractionValidationError):
                extract_knowledge(section(), capability=FakeExtraction([invalid]))

    def test_final_validation_rejects_other_context_ids_duplicate_ids_and_mutated_members(self):
        unit = section()
        valid = extract_knowledge(unit, capability=FakeExtraction([response("Rule", "Other rule")]))
        for bad in (replace(valid, section_id="section-000002"),
                    replace(valid, provenance=replace(valid.provenance, silver=replace(valid.provenance.silver, sha256="f" * 64))),
                    replace(valid, items=(valid.items[0], valid.items[0])),
                    replace(valid, items=(replace(valid.items[0], evidence=(KnowledgeEvidence("section-000002", (EvidenceSpan(0, 3),)),)),))):
            with self.assertRaises(ExtractionValidationError):
                validate_extraction_proposal(bad, unit)
        object.__setattr__(valid.items[0].evidence[0].spans[0], "first_source_index", True)
        with self.assertRaises(ExtractionValidationError):
            validate_extraction_proposal(valid, unit)

    def test_invalid_input_is_rejected_before_capability_and_models_are_immutable(self):
        provider = FakeExtraction([response("Rule")])
        with self.assertRaises(ExtractionInputError):
            extract_knowledge({}, capability=provider)
        unit = section()
        with self.assertRaises(FrozenInstanceError):
            unit.text = "rewrite"
        object.__setattr__(unit.segments[0], "source_index", True)
        with self.assertRaises(ExtractionInputError):
            extract_knowledge(unit, capability=provider)
        self.assertEqual(provider.calls, [])
        with self.assertRaises(ExtractionCapabilityError):
            extract_knowledge(section(), capability=None)
        for limit in (0, -1, True, "8192"):
            with self.assertRaises(ValueError):
                ExtractionConfig(limit)

    def test_complete_input_and_configured_allowance_no_semantic_prefilter_or_truncation(self):
        unit = section("Source " * 10000, "", "advertisement")
        provider = FakeExtraction([response("A rule")])
        extract_knowledge(unit, capability=provider, config=ExtractionConfig(32768))
        self.assertEqual(provider.calls[0].segments, unit.segments)
        self.assertEqual(provider.calls[0].output_limit, 32768)
        self.assertNotIn(unit.text, repr(unit))
        self.assertNotIn(unit.text, repr(provider.calls[0]))

    def test_prompt_covers_semantic_requirements_without_claiming_deterministic_judgment(self):
        for phrase in ("HIGH COVERAGE ALL", "not", "maximally fragmented", "usually", "often", "sometimes",
                       "only if", "unless", "causal", "conditions", "uncertainty", "source correction",
                       "conflicting", "numbers", "subjective importance", "semantically equivalent",
                       "smallest sufficient", "multiple", "untrusted", "Completeness", "transient proposal",
                       "cross-section", "greetings", "calls to action", "macro", "rune", "independent claims"):
            self.assertIn(phrase.lower(), EXTRACTION_SYSTEM_PROMPT.lower())

    def test_refinement_prompt_support_is_opt_in_and_default_standalone_validation_is_unchanged(self):
        from league_knowledge_platform.extraction_prompts import EXTRACTION_REFINEMENT_PROMPT_VERSION
        unit = section()
        original = extract_knowledge(unit, capability=FakeExtraction([response('Rule')]))
        refined = replace(original, metadata=replace(original.metadata, prompt_version=EXTRACTION_REFINEMENT_PROMPT_VERSION))
        with self.assertRaises(ExtractionValidationError):
            validate_extraction_proposal(refined, unit)
        self.assertEqual(validate_extraction_proposal(refined, unit, supported_prompt_versions=(EXTRACTION_REFINEMENT_PROMPT_VERSION,)), refined)


if __name__ == "__main__":
    unittest.main()
