"""Canonical result contracts, exact source derivation and extraction provenance."""

from copy import deepcopy
from decimal import localcontext
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    SegmentationAttempt, SegmentationContractError, SegmentationInputError,
    SegmentationIntegrityError, construct_segmentation_artifact, content_sections,
    decode_segmentation_artifact, validate_segmentation_artifact, validate_segmentation_against_silver,
)
from segmentation_http_fixtures import METADATA, bronze_bytes, proposal, source


def artifact(silver=None, spans=None, attempts=None):
    silver = silver if silver is not None else source()
    value = spans if spans is not None else proposal((0, 3, "CONTENT"), (6, 6, "ADVERTISEMENT"))
    history = attempts if attempts is not None else (SegmentationAttempt(1, metadata=METADATA),)
    return construct_segmentation_artifact(silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest(),
                                          proposal=value, attempts=history)


class SegmentationArtifactTests(unittest.TestCase):
    def test_source_text_is_only_derived_from_silver_no_model_content_fields(self):
        silver = source(" repeat ".strip(), "", "repeat")
        result = artifact(silver)
        self.assertEqual([s["text"] for s in result["sections"]], ["repeat", "repeat"])
        self.assertEqual([m["source_index"] for s in result["sections"] for m in s["members"]], [0, 3, 6])
        self.assertNotIn("title", result["sections"][0])
        self.assertNotIn("summary", result["sections"][0])
        self.assertEqual(result["sections"][0]["section_id"], "section-000001")

    def test_overlaps_and_nonmonotonic_times_use_envelope_of_all_members(self):
        silver = source()
        for segment, start, duration in zip(silver["transcript"]["segments"], ("10", "20", "0.1"), ("100", "1", "0")):
            segment.update(start_seconds=start, duration_seconds=duration)
        with localcontext() as context:
            context.prec = 2
            result = artifact(silver, proposal((0, 6, "CONTENT")))
        self.assertEqual((result["sections"][0]["start_seconds"], result["sections"][0]["end_seconds"]), ("0.1", "110"))
        for cleaned, original in zip(result["sections"][0]["members"], silver["transcript"]["segments"]):
            self.assertEqual(cleaned["start_seconds"], original["start_seconds"])
            self.assertEqual(cleaned["duration_seconds"], original["duration_seconds"])

    def test_exact_decimal_addition_is_independent_of_global_context_precision(self):
        silver = source("one")
        silver["transcript"]["segments"][0].update(start_seconds="1000000000000000000000000000000", duration_seconds="0.000001")
        with localcontext() as context:
            context.prec = 2
            result = artifact(silver, proposal((0, 0, "CONTENT")))
        self.assertEqual(result["sections"][0]["end_seconds"], "1000000000000000000000000000000.000001")

    def test_all_provenance_is_copied_without_aliasing_or_large_upstream_payload_duplication(self):
        silver = source()
        before = deepcopy(silver)
        result = artifact(silver)
        for name in ("source", "bronze", "acquisition"):
            self.assertEqual(result[name], silver[name])
        self.assertEqual(result["silver"]["sha256"], sha256(bronze_bytes(silver)).hexdigest())
        self.assertEqual(result["silver"]["relative_path"], "youtube/id-41.json")
        self.assertEqual(result["silver"]["segment_count"], 3)
        self.assertNotIn("transcript", result)
        self.assertNotIn("source_text", json.dumps(result))
        result["source"]["title"] = "changed copy"
        self.assertEqual(silver, before)

    def test_canonical_timing_round_trip_is_independent_of_decimal_exponent_formatting(self):
        silver = source("one")
        silver["transcript"]["segments"][0].update(start_seconds="1E+4", duration_seconds="0")
        encoded = []
        for capitals in (0, 1):
            with localcontext() as context:
                context.capitals = capitals
                result = artifact(silver, proposal((0, 0, "CONTENT")))
                self.assertEqual(result["sections"][0]["start_seconds"], "1E+4")
                self.assertEqual(result["sections"][0]["end_seconds"], "1E+4")
                encoded.append(bronze_bytes(result))
            for reload_capitals in (0, 1):
                with localcontext() as context:
                    context.capitals = reload_capitals
                    self.assertEqual(decode_segmentation_artifact(encoded[-1]), result)
        self.assertEqual(encoded[0], encoded[1])

    def test_full_round_trip_and_input_validation_without_provider(self):
        silver = source()
        result = artifact(silver)
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("No upstream file access")):
            decoded = decode_segmentation_artifact(bronze_bytes(result))
        self.assertEqual(decoded, result)
        self.assertIs(validate_segmentation_against_silver(result, silver,
            silver_sha256=sha256(bronze_bytes(silver)).hexdigest()), result)

    def test_content_view_retains_durable_ads_and_cannot_mutate_the_original(self):
        result = artifact()
        before = deepcopy(result)
        selected = content_sections(result)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["kind"], "CONTENT")
        selected[0]["text"] = "changed view"
        self.assertEqual(result, before)
        self.assertEqual(result["sections"][1]["text"], "Sponsor product")

    def test_all_advertisement_source_is_segmented_not_empty_outcome(self):
        result = artifact(spans=proposal((0, 6, "ADVERTISEMENT")))
        self.assertEqual(result["outcome"], "SEGMENTED")
        self.assertEqual(content_sections(result), ())
        self.assertEqual(len(result["sections"]), 1)

    def test_content_view_preserves_source_context_and_nested_copy_isolation(self):
        original = artifact(spans=proposal((0, 0, "CONTENT"), (3, 6, "CONTENT")))
        other_silver = source()
        other_silver["source"].update(video_id="B", url="https://www.youtube.com/watch?v=B")
        other_silver["bronze"]["relative_path"] = "youtube/id-42.json"
        other = artifact(other_silver, proposal((0, 0, "CONTENT"), (3, 6, "CONTENT")))
        first_view, other_view = content_sections(original), content_sections(other)
        self.assertEqual(first_view[0]["text"], other_view[0]["text"])
        self.assertEqual(first_view[0]["members"], other_view[0]["members"])
        self.assertNotEqual(first_view, other_view)
        self.assertEqual(first_view[0]["source"]["video_id"], "A")
        self.assertEqual(other_view[0]["source"]["video_id"], "B")
        before = deepcopy(original)
        for name in ("source", "silver", "bronze", "acquisition"):
            self.assertEqual(first_view[0][name], original[name])
            first_view[0][name].clear()
            self.assertEqual(first_view[1][name], original[name])
        first_view[0]["members"][0]["source_index"] = 999
        self.assertEqual(original, before)

    def test_empty_outcome_has_provenance_but_no_fake_sections_or_semantic_calls(self):
        silver = source("", "", "")
        result = construct_segmentation_artifact(silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest())
        self.assertEqual(result["outcome"], "NO_SEGMENTABLE_CONTENT")
        self.assertEqual(result["sections"], [])
        self.assertEqual(result["attempts"], [])
        self.assertIsNone(result["semantic"])
        self.assertEqual(result["silver"]["segment_count"], 3)
        self.assertEqual(content_sections(result), ())
        validate_segmentation_against_silver(result, silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest())
        with self.assertRaises(SegmentationContractError):
            construct_segmentation_artifact(silver, silver_sha256="0" * 64, proposal=proposal((0, 6, "CONTENT")))

    def test_missing_segment_list_is_invalid_input_not_empty_outcome(self):
        silver = source()
        silver["transcript"]["segments"] = []
        with self.assertRaises(SegmentationInputError):
            construct_segmentation_artifact(silver, silver_sha256="0" * 64)

    def test_canonical_versions_schema_and_attempt_audit_are_strict(self):
        mutations = (
            lambda a: a.update(schema_version=2), lambda a: a.update(schema_version=True),
            lambda a: a.update(segmentation_contract_version=2), lambda a: a.update(extra="unknown"),
            lambda a: a.update(outcome="PARTIAL"), lambda a: a["source"].update(video_id="../A"),
            lambda a: a["source"].update(url="https://youtu.be/A"),
            lambda a: a["silver"].update(schema_version=True),
            lambda a: a["silver"].update(cleaning_rule_version=2),
            lambda a: a["silver"].update(segment_count=0), lambda a: a["silver"].update(sha256="bad"),
            lambda a: a["silver"].update(relative_path="../input.json"),
            lambda a: a["bronze"].update(schema_version=2),
            lambda a: a["semantic"].update(prompt_version="different-contract"),
            lambda a: a.update(attempts=[]), lambda a: a["attempts"][0].update(number=True),
            lambda a: a["attempts"][0].update(number=2),
            lambda a: a["attempts"][0].update(failure_kind="INVALID_OUTPUT"),
            lambda a: a["attempts"][0].update(metadata=None),
            lambda a: a["sections"][0].update(title="generated title"),
            lambda a: a["sections"][0].update(summary="generated summary"),
            lambda a: a["sections"][0].update(kind="NOISE"),
            lambda a: a["sections"][0].update(section_id="invented"),
            lambda a: a["sections"][0].update(start_position=1),
            lambda a: a["sections"][0].update(end_position_exclusive=3),
            lambda a: a["sections"][0]["members"][0].update(start_seconds="NaN"),
            lambda a: a["sections"][0]["members"][0].update(duration_seconds="-1"),
            lambda a: a["sections"][0].update(end_seconds="999"),
        )
        for mutate in mutations:
            result = artifact()
            mutate(result)
            with self.subTest(mutate=mutate), self.assertRaises(SegmentationContractError):
                validate_segmentation_artifact(result)
        result = artifact()
        result["sections"][0]["members"][1]["source_index"] = 0
        with self.assertRaises(SegmentationContractError):
            validate_segmentation_artifact(result)

    def test_provenance_and_text_tampering_cannot_pass_silver_aware_validation(self):
        silver = source()
        mutations = (
            lambda a: a["sections"][0].update(text="LLM rewritten text"),
            lambda a: a["sections"][0]["members"][1].update(source_index=4),
            lambda a: a["source"].update(title="different title"),
            lambda a: a["acquisition"].update(run_id="later-run"),
            lambda a: a["silver"].update(sha256="0" * 64),
            lambda a: a["bronze"].update(sha256="0" * 64),
        )
        for mutate in mutations:
            result = artifact(silver)
            mutate(result)
            with self.subTest(mutate=mutate), self.assertRaises(SegmentationIntegrityError):
                validate_segmentation_against_silver(result, silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest())

    def test_semantically_different_valid_partitions_are_each_structurally_valid(self):
        silver = source()
        for spans in (proposal((0, 6, "CONTENT")), proposal((0, 3, "CONTENT"), (6, 6, "ADVERTISEMENT"))):
            result = artifact(silver, spans)
            validate_segmentation_against_silver(result, silver, silver_sha256=sha256(bronze_bytes(silver)).hexdigest())

    def test_prior_failures_and_success_provenance_are_bounded_and_serializable(self):
        result = artifact(attempts=(SegmentationAttempt(1, "NETWORK"),
                                   SegmentationAttempt(2, "INVALID_OUTPUT", ("COVERAGE_GAP",)),
                                   SegmentationAttempt(3, metadata=METADATA)))
        self.assertEqual([a["number"] for a in result["attempts"]], [1, 2, 3])
        self.assertEqual(decode_segmentation_artifact(bronze_bytes(result)), result)

    def test_decoder_rejects_duplicate_keys_nonfinite_invalid_utf8_and_malformed_json(self):
        for raw in (b'{', b'\xff', b'null', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(SegmentationContractError):
                decode_segmentation_artifact(raw)


if __name__ == "__main__":
    unittest.main()
