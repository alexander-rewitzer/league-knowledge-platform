"""Read-only projection contracts and package exports, using synthetic values."""

from dataclasses import FrozenInstanceError, fields, replace
import unittest

from league_knowledge_platform import (
    GoldInspection, GoldInspectionError, GoldInspectionResult, GoldProvenanceView,
    GoldSearchPage, GoldSnapshotReference, GoldStatus, GoldTopicContext, GoldUnitView,
)
from gold_document_fixtures import document
from test_gold_models import identity, unit


def view():
    doc = document()
    return GoldUnitView(doc.units[0], doc.document_id, 1, doc.title, doc.entity, doc.domain_key,
                        GoldTopicContext(doc.topics[0].topic_id, doc.topics[0].title, 0, 0))


class GoldInspectionModelTests(unittest.TestCase):
    def test_models_are_frozen_and_contain_no_upstream_artifact_payload(self):
        value = view()
        with self.assertRaises(FrozenInstanceError):
            value.document_version = 2
        with self.assertRaises(FrozenInstanceError):
            value.unit.content = "Mutation"
        provenance = GoldProvenanceView(identity(), value.unit.provenance)
        self.assertEqual({f.name for f in fields(provenance)}, {"knowledge_id", "provenance"})
        self.assertFalse(hasattr(provenance, "transcript"))

    def test_historical_records_cannot_have_fake_active_placement(self):
        historical = unit(3, status=GoldStatus.MERGED, merged_into=identity())
        with self.assertRaises(GoldInspectionError):
            replace(view(), unit=historical)
        self.assertIsNone(replace(view(), unit=historical, topic=None).topic)
        with self.assertRaises(GoldInspectionError):
            replace(view(), topic=None)

    def test_result_envelope_has_original_snapshot_reference_and_strict_value_type(self):
        ref = GoldSnapshotReference("a" * 32, "b" * 64)
        result = GoldInspectionResult(ref, view())
        self.assertEqual(result.snapshot_reference, ref)
        with self.assertRaises(FrozenInstanceError):
            result.snapshot_reference = ref
        with self.assertRaises(GoldInspectionError):
            GoldInspectionResult(ref, {"content": "not a typed projection"})

    def test_empty_search_page_and_invalid_pagination_contract(self):
        ref = GoldSnapshotReference("a" * 32, "b" * 64)
        page = GoldSearchPage(ref, (), (), 0, 20, 0, False)
        for changes in ({"limit": True}, {"offset": -1}, {"total_matches": 1},
                        {"has_more": True}, {"matches": []}, {"query_tokens": []}):
            with self.subTest(changes=changes), self.assertRaises(GoldInspectionError):
                replace(page, **changes)

    def test_inspection_object_and_exports_have_no_writer_or_operation_selection_api(self):
        import league_knowledge_platform as package
        for name in ("GoldInspection", "GoldInspectionResult", "open_gold_inspection", "GoldUnitView",
                     "GoldSearchPage", "GoldStructureView", "GoldInspectionRequestError"):
            self.assertIn(name, package.__all__)
        self.assertEqual(len(package.__all__), len(set(package.__all__)))
        self.assertFalse(any(hasattr(GoldInspection, name) for name in
            ("publish", "write", "initialize", "select_operation", "integrate", "emit_event")))


if __name__ == "__main__":
    unittest.main()
