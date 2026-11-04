"""Synthetic canonical Gold and direct inspection of its original identities."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from league_knowledge_platform import (
    GoldConfig, GoldEntity, GoldInspectionNotFoundError, GoldInspectionRequestError,
    GoldProvenance, GoldRelation, GoldRelationKind, GoldSnapshot, GoldStatus,
    initialize_gold_store, load_gold_snapshot, open_gold_inspection,
    publish_gold_snapshot, stage_gold_snapshot,
)
from gold_document_fixtures import document, topic
from test_gold_models import identity, reference, unit


def inspection_snapshot():
    a = unit(1, content="Save Charm when the enemy can engage.", relations=(
        GoldRelation(GoldRelationKind.RELATED_TO, identity(4)),
        GoldRelation(GoldRelationKind.CONTRADICTS, identity(7))))
    b = unit(2, content="Manage the wave before using Charm.")
    c = unit(3, content="Cafe\u0301 timing creates a reset window.")
    oldest = unit(6, content="Old Charm claim", status=GoldStatus.MERGED, merged_into=identity(7))
    old = unit(7, content="Prior claim", status=GoldStatus.MERGED, merged_into=identity(1),
               relations=(GoldRelation(GoldRelationKind.CONTRADICTS, identity(4)),))
    ahri = document(topics=(topic(1, (2, 1), position=1, title="Trading Windows"),
                           topic(2, (3,), position=0, title="Wave Control")),
                    units=(a, b, c), historical=(oldest, old))
    general = replace(document(2, topics=(topic(1, (4,), title="Reset Timing"),),
        units=(unit(4, content="The wave controls a safe reset window.",
                    provenance=(GoldProvenance(reference("B"), "source-item-000002"),)),), domain="tempo"),
        entity=GoldEntity("concepts", "wave-control", "Wave Control"), title="General Wave Concepts")
    zed = replace(document(3, topics=(topic(3, (5,), title="Trading"),),
        units=(unit(5, content="Track cooldowns before trading."),)),
        entity=GoldEntity("champions", "zed", "Zed"), title="Zed — Laning")
    return GoldSnapshot((zed, general, ahri))


class InspectionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = GoldConfig(Path(self.directory.name) / "gold")
        initialize_gold_store(self.config)
        self.snapshot = inspection_snapshot()
        self.publish(self.snapshot)
        self.inspection = open_gold_inspection(self.config)

    def publish(self, snapshot, *, routes=None):
        base = load_gold_snapshot(self.config).reference
        staged = stage_gold_snapshot(snapshot, config=self.config, base=base, routes=routes)
        return publish_gold_snapshot(staged, config=self.config, expected_base=base)


class GoldInspectionTests(InspectionFixture):
    def test_active_lookup_retains_exact_unit_document_and_topic_context(self):
        result = self.inspection.get_gold_unit(identity())
        self.assertEqual(result.snapshot_reference, self.inspection.reference)
        self.assertEqual(result.value.unit, self.snapshot.documents[0].units[0])
        self.assertEqual(result.value.document_id, self.snapshot.documents[0].document_id)
        self.assertEqual(result.value.document_version, 1)
        self.assertEqual(result.value.topic.topic_id, topic().topic_id)
        self.assertEqual(result.value.topic.unit_position, 1)
        self.assertEqual(result.value.topic.position, 1)

    def test_historical_lookup_does_not_replace_or_flatten_chained_ids(self):
        result = self.inspection.get_gold_unit(identity(6))
        self.assertEqual(result.value.unit.knowledge_id, identity(6))
        self.assertEqual(result.value.unit.status, GoldStatus.MERGED)
        self.assertEqual(result.value.unit.merged_into, identity(7))
        self.assertEqual(result.value.unit.content, "Old Charm claim")
        self.assertIsNone(result.value.topic)
        self.assertEqual(result.value.document_id, document().document_id)
        old = self.inspection.get_gold_unit(identity(7)).value.unit
        self.assertEqual(old.merged_into, identity())

    def test_document_and_document_local_topic_inspection_preserve_order(self):
        doc = self.inspection.get_gold_document(document().document_id)
        self.assertEqual(doc.value, self.snapshot.documents[0])
        view = self.inspection.get_gold_topic(document().document_id, topic().topic_id)
        self.assertEqual(tuple(u.knowledge_id for u in view.value.units), (identity(2), identity()))
        # Identical topic IDs in separate documents are intentionally valid.
        other = self.inspection.get_gold_topic(document(2).document_id, topic().topic_id)
        self.assertEqual(tuple(u.knowledge_id for u in other.value.units), (identity(4),))
        self.assertEqual(view.snapshot_reference, other.snapshot_reference)

    def test_provenance_relations_and_status_are_original_not_inferred(self):
        unit_view = self.inspection.get_gold_unit(identity()).value
        provenance = self.inspection.get_gold_provenance(identity())
        self.assertEqual(provenance.value.knowledge_id, identity())
        self.assertEqual(provenance.value.provenance, unit_view.unit.provenance)
        self.assertEqual(provenance.value.provenance[0].source_knowledge_artifact.sha256, "a" * 64)
        self.assertEqual({r.kind for r in unit_view.unit.relations}, set(GoldRelationKind))
        self.assertEqual(self.inspection.get_gold_unit(identity(4)).value.unit.relations, ())
        self.assertEqual(self.inspection.get_gold_unit(identity(7)).value.unit.relations[0].target_knowledge_id, identity(4))

    def test_structure_orders_entities_documents_topics_and_placements(self):
        result = self.inspection.inspect_gold_structure()
        entities = result.value.entities
        self.assertEqual([(e.entity.namespace, e.entity.entity_key) for e in entities],
                         [("champions", "ahri"), ("champions", "zed"), ("concepts", "wave-control")])
        ahri = entities[0].documents[0]
        self.assertEqual(tuple(t.position for t in ahri.topics), (0, 1))
        self.assertEqual(ahri.topics[1].knowledge_ids, (identity(2), identity()))
        self.assertEqual(ahri.historical_knowledge_ids, (identity(6), identity(7)))
        self.assertEqual(result.snapshot_reference, self.inspection.reference)
        self.assertFalse(hasattr(ahri.topics[0], "content"))

    def test_structure_filters_are_exact_and_combine(self):
        view = self.inspection.inspect_gold_structure(entity_namespace="concepts", domain_key="tempo")
        self.assertEqual(len(view.value.entities), 1)
        self.assertEqual(view.value.entities[0].entity.entity_key, "wave-control")
        self.assertEqual(self.inspection.inspect_gold_structure(entity_key="ahri", domain_key="tempo").value.entities, ())
        self.assertEqual(self.inspection.inspect_gold_structure(document_id=document(99).document_id).value.entities, ())

    def test_structure_preserves_differing_display_labels_in_valid_gold(self):
        other = replace(document(4, topics=(topic(4, (8,)),), units=(unit(8),), domain="teamfighting"),
                        entity=GoldEntity("champions", "ahri", "Alternate stored label"))
        self.publish(GoldSnapshot((*self.snapshot.documents, other)))
        session = open_gold_inspection(self.config)
        groups = session.inspect_gold_structure(entity_namespace="champions", entity_key="ahri").value.entities
        self.assertEqual({g.entity.display_name for g in groups}, {"Ahri", "Alternate stored label"})
        self.assertEqual(tuple(g.entity.display_name for g in groups), ("Ahri", "Alternate stored label"))

    def test_missing_ids_and_malformed_requests_have_distinct_errors(self):
        requests = ((self.inspection.get_gold_unit, (identity(99),), "UNIT_NOT_FOUND"),
                    (self.inspection.get_gold_document, (document(99).document_id,), "DOCUMENT_NOT_FOUND"),
                    (self.inspection.get_gold_topic, (document().document_id, topic(99).topic_id), "TOPIC_NOT_FOUND"))
        for method, args, code in requests:
            with self.subTest(code=code), self.assertRaises(GoldInspectionNotFoundError) as caught:
                method(*args)
            self.assertEqual(caught.exception.code, code)
        for invalid in (None, True, "README.md:12", "Ahri", "gku-" + "A" * 32):
            with self.subTest(invalid=invalid), self.assertRaises(GoldInspectionRequestError):
                self.inspection.get_gold_unit(invalid)
        with self.assertRaises(GoldInspectionRequestError):
            self.inspection.get_gold_topic(document().document_id, "heading")


if __name__ == "__main__":
    unittest.main()
