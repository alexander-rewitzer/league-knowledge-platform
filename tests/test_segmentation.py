"""Offline partition validation and one bounded semantic/provider retry budget."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    SegmentationCapabilityError, SegmentationConfig, SegmentationProviderError,
    SegmentationRequest, SegmentationSegment, SegmentationValidationError,
    validate_segmentation_proposal,
)
from league_knowledge_platform.segmentation import _request, _run_semantic
from segmentation_http_fixtures import FakeSegmentation, proposal, source

CONFIG = SegmentationConfig(retry_delays_seconds=(0, 0))


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.request = _request(source("repeat", "", "repeat", "sponsor"))

    def test_one_or_many_sections_partition_actual_sequence_with_index_gaps(self):
        for result in (proposal((0, 9, "CONTENT")),
                       proposal((0, 6, "CONTENT"), (9, 9, "ADVERTISEMENT")),
                       proposal((0, 0, "CONTENT"), (3, 3, "CONTENT"), (6, 6, "CONTENT"), (9, 9, "ADVERTISEMENT"))):
            self.assertEqual(validate_segmentation_proposal(result, self.request), ())

    def test_gap_overlap_duplicate_reordering_unknown_and_reversed_ranges(self):
        cases = (
            (proposal((3, 9, "CONTENT")), "COVERAGE_GAP"),
            (proposal((0, 0, "CONTENT"), (6, 9, "CONTENT")), "COVERAGE_GAP"),
            (proposal((0, 6, "CONTENT"), (6, 9, "CONTENT")), "OVERLAP_OR_ORDER"),
            (proposal((0, 9, "CONTENT"), (0, 9, "CONTENT")), "OVERLAP_OR_ORDER"),
            (proposal((6, 9, "CONTENT"), (0, 3, "CONTENT")), "COVERAGE_GAP"),
            (proposal((0, 5, "CONTENT")), "UNKNOWN_SEGMENT"),
            (proposal((6, 0, "CONTENT")), "INVALID_RANGE"),
            (proposal((0, 6, "CONTENT")), "INCOMPLETE_COVERAGE"),
            (proposal(), "EMPTY_PROPOSAL"),
        )
        for value, code in cases:
            with self.subTest(code=code, spans=value.sections):
                issues = validate_segmentation_proposal(value, self.request)
                self.assertEqual(issues[0].code, code)

    def test_coverage_feedback_identifies_exact_expected_and_received_ids(self):
        issues = validate_segmentation_proposal(proposal((0, 0, "CONTENT"), (6, 9, "CONTENT")), self.request)
        self.assertEqual((issues[0].section_position, issues[0].expected_first_source_index,
                          issues[0].received_first_source_index), (1, 3, 6))

    def test_invalid_capability_return_and_mutated_decision_are_rejected(self):
        self.assertEqual(validate_segmentation_proposal({"sections": []}, self.request)[0].code, "SCHEMA")
        value = proposal((0, 9, "CONTENT"))
        object.__setattr__(value.sections[0], "kind", "NOISE")
        self.assertEqual(validate_segmentation_proposal(value, self.request)[0].code, "SCHEMA")

    def test_semantic_request_rejects_invented_order_and_invalid_ids(self):
        for entries in ((SegmentationSegment(3, "a"), SegmentationSegment(0, "b")),
                        (SegmentationSegment(0, "a"), SegmentationSegment(0, "b"))):
            with self.assertRaises(ValueError):
                SegmentationRequest("en", entries)
        for value in (True, -1, "1"):
            with self.assertRaises(ValueError):
                SegmentationSegment(value, "text")


class SemanticRetryTests(unittest.TestCase):
    def run_source(self, fake, silver=None, config=CONFIG):
        return _run_semantic("A", silver or source(), fake, config)

    def test_invalid_then_valid_uses_concrete_feedback_and_identical_complete_input(self):
        fake = FakeSegmentation([proposal((3, 6, "CONTENT")), proposal((0, 6, "CONTENT"))])
        result, attempts = self.run_source(fake)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].validation_codes, ("COVERAGE_GAP",))
        self.assertEqual(attempts[-1].metadata, result.metadata)
        self.assertEqual(fake.requests[0].segments, fake.requests[1].segments)
        self.assertEqual(fake.requests[1].validation_feedback[0].expected_first_source_index, 0)
        self.assertEqual(fake.requests[1].validation_feedback[0].received_first_source_index, 3)

    def test_transport_schema_and_success_share_same_three_attempt_budget(self):
        fake = FakeSegmentation([SegmentationCapabilityError("NETWORK", retryable=True),
                                 SegmentationCapabilityError("INVALID_OUTPUT", retryable=True),
                                 proposal((0, 6, "CONTENT"))])
        _, attempts = self.run_source(fake)
        self.assertEqual([a.failure_kind for a in attempts], ["NETWORK", "INVALID_OUTPUT", None])
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(fake.requests[2].validation_feedback[0].code, "SCHEMA")

    def test_three_invalid_proposals_fail_without_fourth_invocation(self):
        fake = FakeSegmentation([proposal()] * 3 + [proposal((0, 6, "CONTENT"))])
        with self.assertRaises(SegmentationValidationError) as caught:
            self.run_source(fake)
        self.assertEqual(len(caught.exception.attempts), 3)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(len(fake.script), 1)

    def test_three_provider_failures_are_explicit_and_sanitized(self):
        fake = FakeSegmentation([SegmentationCapabilityError("SERVICE", retryable=True)] * 4)
        with self.assertRaises(SegmentationProviderError) as caught:
            self.run_source(fake)
        self.assertEqual(caught.exception.phase, "SERVICE")
        self.assertEqual(len(caught.exception.attempts), 3)
        self.assertNotIn("Ahri mechanics", str(caught.exception))
        self.assertEqual(len(fake.requests), 3)

    def test_unrecoverable_auth_context_config_and_unexpected_exception_stop(self):
        for failure in (SegmentationCapabilityError("AUTHENTICATION", retryable=False),
                        SegmentationCapabilityError("CONTEXT_LIMIT", retryable=False),
                        SegmentationCapabilityError("CONFIGURATION", retryable=False), RuntimeError("private text")):
            fake = FakeSegmentation([failure, proposal((0, 6, "CONTENT"))])
            with self.subTest(failure=type(failure)), self.assertRaises(SegmentationProviderError):
                self.run_source(fake)
            self.assertEqual(len(fake.requests), 1)

    def test_output_truncation_increases_allowance_without_truncating_or_coarsening_input(self):
        fake = FakeSegmentation([SegmentationCapabilityError("OUTPUT_TRUNCATED", retryable=True),
                                 proposal((0, 6, "CONTENT"))])
        self.run_source(fake)
        self.assertEqual(fake.requests[1].output_limit, 2 * fake.requests[0].output_limit)
        self.assertEqual(fake.requests[0].segments, fake.requests[1].segments)

    def test_output_allowance_is_not_a_semantic_section_count_or_size_target(self):
        silver = source(*("Short distinct topic" for _ in range(150)))
        fake = FakeSegmentation([proposal(*((i * 3, i * 3, "CONTENT") for i in range(150)))])
        self.run_source(fake, silver)
        self.assertEqual(fake.requests[0].output_limit, 64 * 150)
        self.assertEqual(len(fake.requests[0].segments), 150)

    def test_config_never_allows_more_than_three_or_nested_retries(self):
        for count in (0, 4, True):
            with self.assertRaises(ValueError):
                replace(CONFIG, max_attempts=count)
        fake = FakeSegmentation([proposal()] * 3)
        with patch("league_knowledge_platform.segmentation.time.sleep") as sleep, self.assertRaises(SegmentationValidationError):
            self.run_source(fake)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
