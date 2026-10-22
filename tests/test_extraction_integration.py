"""Silver and segmentation to transient proposals, without durable side effects."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeConfig, SegmentationConfig, SilverConfig, clean_bronze_source,
    canonical_bronze_path, load_extraction_section, segment_silver_source, extract_knowledge,
)
from extraction_http_fixtures import FakeExtraction, response, bronze_bytes
from segmentation_http_fixtures import FakeSegmentation, proposal
from test_silver import bronze_fixture


class ExtractionIntegrationTests(unittest.TestCase):
    def test_real_upstream_pipeline_to_extraction_is_read_only_and_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bronze, silver, sections = BronzeConfig(root / "bronze"), SilverConfig(root / "silver"), SegmentationConfig(root / "sections")
            path = canonical_bronze_path("A", bronze)
            path.parent.mkdir(parents=True)
            path.write_bytes(bronze_bytes(bronze_fixture("Usually trade only if the interrupt is unavailable.",
                                                       "Otherwise it can stop the approach.", "Buy a product")))
            clean_bronze_source("A", bronze_config=bronze, silver_config=silver)
            segment_silver_source("A", bronze_config=bronze, silver_config=silver, config=sections,
                                  capability=FakeSegmentation([proposal((0, 3, "CONTENT"), (6, 6, "ADVERTISEMENT"))]))
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch("sqlite3.connect", side_effect=AssertionError("No database access")), \
                    patch("league_knowledge_platform._bronze_filesystem.stage", side_effect=AssertionError("No publication")), \
                    patch("league_knowledge_platform.source_catalog.SQLiteSourceCatalog.register_integrated", side_effect=AssertionError):
                unit = load_extraction_section("A", "section-000001", silver_config=silver, segmentation_config=sections)
                provider = FakeExtraction([response("Usually trade only while the interrupt is unavailable, because it can stop an approach.")])
                result = extract_knowledge(unit, capability=provider)
            self.assertEqual(result.items[0].evidence[0].section_id, "section-000001")
            self.assertEqual([s.source_index for s in unit.segments], [0, 3])
            self.assertNotIn("Buy a product", unit.text)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
