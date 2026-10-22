"""Read-only artifact identity, exact byte provenance and section fidelity."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    ExtractionInputError, ExtractionIntegrityError, SegmentationConfig, SilverConfig,
    canonical_segmentation_path, canonical_silver_path, extraction_section_from_artifacts,
    load_extraction_section, construct_segmentation_artifact,
)
from extraction_http_fixtures import input_bytes, section, bronze_bytes, source
from test_segmentation_artifacts import artifact


class ExtractionInputTests(unittest.TestCase):
    def make(self, segmented, silver, section_id="section-000001"):
        return extraction_section_from_artifacts(segmentation_content=segmented, silver_content=silver, section_id=section_id)

    def test_actual_member_text_order_gaps_empty_repetition_and_timing_are_preserved(self):
        segmented, silver = input_bytes("Repeat", "", "Repeat")
        unit = self.make(segmented, silver)
        self.assertEqual([s.source_index for s in unit.segments], [0, 3])
        self.assertEqual([s.text for s in unit.segments], ["Repeat", ""])
        self.assertEqual(unit.text, "Repeat")
        self.assertEqual(unit.segments[0].start_seconds, "0.12500")
        self.assertEqual(unit.segments[0].duration_seconds, "1E-8")
        self.assertEqual(unit.provenance.segmentation.sha256, sha256(segmented).hexdigest())
        self.assertEqual(unit.provenance.silver.sha256, sha256(silver).hexdigest())
        self.assertEqual(unit.provenance.bronze.sha256, json.loads(silver)["bronze"]["sha256"])

    def test_advertisement_unknown_section_and_empty_upstream_outcome_are_not_units(self):
        segmented, silver = input_bytes()
        for identity in ("section-000002", "section-999999", None):
            with self.subTest(identity=identity), self.assertRaises(ExtractionInputError):
                self.make(segmented, silver, identity)
        empty_silver = source("", "", "")
        with self.assertRaises(ExtractionInputError) as caught:
            empty = construct_segmentation_artifact(empty_silver, silver_sha256=sha256(bronze_bytes(empty_silver)).hexdigest())
            self.make(bronze_bytes(empty), bronze_bytes(empty_silver))
        self.assertEqual(caught.exception.kind, "NO_SEGMENTABLE_CONTENT")

    def test_versions_malformed_json_and_tampered_text_or_membership_are_rejected(self):
        segmented, silver = input_bytes()
        for bad in (b"{", b"[]", b'\xff', b'{"schema_version":1,"schema_version":1}', None):
            with self.subTest(bad=bad), self.assertRaises(ExtractionInputError):
                self.make(bad, silver)
        for field, value in (("schema_version", 99), ("segmentation_contract_version", 99)):
            changed = json.loads(segmented)
            changed[field] = value
            with self.assertRaises(ExtractionInputError):
                self.make(bronze_bytes(changed), silver)
        changed = json.loads(segmented)
        changed["sections"][0]["text"] = "Invented replacement"
        with self.assertRaises(ExtractionIntegrityError):
            self.make(bronze_bytes(changed), silver)
        changed = json.loads(segmented)
        changed["sections"][0]["members"][0]["source_index"] = 1
        with self.assertRaises(ExtractionIntegrityError):
            self.make(bronze_bytes(changed), silver)

    def test_exact_silver_byte_hash_not_just_logically_equivalent_json(self):
        segmented, silver = input_bytes()
        alternate = json.dumps(json.loads(silver)).encode()
        with self.assertRaises(ExtractionIntegrityError):
            self.make(segmented, alternate)

    def test_read_only_loader_checks_identity_and_has_no_cleanup_flush_or_provider(self):
        segmented, silver = input_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silver_config, segmentation_config = SilverConfig(root / "silver"), SegmentationConfig(root / "sections")
            for path, content in ((canonical_silver_path("A", silver_config), silver),
                                  (canonical_segmentation_path("A", segmentation_config), segmented)):
                path.parent.mkdir(parents=True)
                path.write_bytes(content)
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch("league_knowledge_platform._bronze_filesystem.confirm_durable", side_effect=AssertionError), \
                    patch("league_knowledge_platform._bronze_filesystem.cleanup_temporary_files", side_effect=AssertionError):
                unit = load_extraction_section("A", "section-000001", silver_config=silver_config,
                                               segmentation_config=segmentation_config)
            self.assertEqual(unit.provenance.video_id, "A")
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            for path, content in ((canonical_silver_path("B", silver_config), silver),
                                  (canonical_segmentation_path("B", segmentation_config), segmented)):
                path.write_bytes(content)
            with self.assertRaises(ExtractionIntegrityError):
                load_extraction_section("B", "section-000001", silver_config=silver_config, segmentation_config=segmentation_config)
            with self.assertRaises(ExtractionInputError):
                load_extraction_section("Missing", "section-000001", silver_config=silver_config, segmentation_config=segmentation_config)

    def test_path_escape_is_rejected_without_reading_the_target(self):
        segmented, silver = input_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SilverConfig(root / "silver")
            outside = root / "outside.json"
            outside.write_bytes(silver)
            with patch("league_knowledge_platform.extraction.canonical_silver_path", return_value=outside):
                with self.assertRaises(ExtractionIntegrityError):
                    load_extraction_section("A", "section-000001", silver_config=config)


if __name__ == "__main__":
    unittest.main()
