from dataclasses import replace
import unittest

from league_knowledge_platform import (
    GoldContractError, GoldContractState, GoldDocumentError, GoldRelation, GoldRelationKind,
    GoldSnapshot, GoldStatus, decode_gold_document_pair, encode_gold_document_metadata,
    get_gold_unit, normalize_gold_structure, render_gold_document, resolve_knowledge_id, validate_gold_snapshot,
)
from gold_document_fixtures import document, layout, topic
from test_gold_models import identity, unit


class GoldStructureTests(unittest.TestCase):
    def test_empty_corpus_without_placeholder(self):
        state = GoldContractState(1, 1)
        self.assertEqual(normalize_gold_structure(state, ()).documents, ())
        self.assertEqual(normalize_gold_structure(state, (layout(topics=()),)).documents, ())
        self.assertEqual(validate_gold_snapshot(GoldSnapshot()), state)

    def test_prune_old_topic_and_document_after_move_preserves_all_units(self):
        state = GoldContractState(1, 1, (unit(1), unit(2)))
        moved = normalize_gold_structure(state, (layout(topics=(topic(ids=()),)),
            layout(2, topics=(topic(2, (1, 2)), topic(3, (), position=1)), domain="wave-management")))
        self.assertEqual(len(moved.documents), 1)
        self.assertEqual(len(moved.documents[0].topics), 1)
        self.assertEqual(validate_gold_snapshot(moved), state)

    def test_merged_chain_is_full_metadata_in_survivor_document_without_empty_container(self):
        a = unit(1, status=GoldStatus.MERGED, merged_into=identity(2))
        b = unit(2, status=GoldStatus.MERGED, merged_into=identity(3))
        c = unit(3)
        state = GoldContractState(1, 1, (a, b, c))
        snapshot = normalize_gold_structure(state, (layout(topics=(topic(ids=(1, 2)),)),
            layout(2, topics=(topic(2, (3,)),), domain="abilities")))
        self.assertEqual(len(snapshot.documents), 1)
        d = snapshot.documents[0]
        self.assertEqual(d.historical_units, (a, b))
        md, meta = render_gold_document(d), encode_gold_document_metadata(d)
        self.assertNotIn(f"knowledge:{identity(1)}".encode(), md)
        loaded = decode_gold_document_pair(md, meta)
        loaded_state = validate_gold_snapshot(GoldSnapshot((loaded,)))
        self.assertEqual(get_gold_unit(loaded_state, identity(1)), a)
        self.assertEqual(resolve_knowledge_id(loaded_state, identity(1)), identity(3))
        self.assertEqual(loaded_state, state)

    def test_unknown_duplicate_unplaced_and_invalid_layout_fail(self):
        state = GoldContractState(1, 1, (unit(), unit(2)))
        for proposed in ((layout(topics=(topic(ids=(1,)),)),), (layout(), layout(2)),
                         (layout(topics=(topic(ids=(1, 2, 99)),)),), [layout()]):
            with self.subTest(proposed=proposed), self.assertRaises(GoldContractError):
                normalize_gold_structure(state, proposed)

    def test_complete_snapshot_checks_cross_document_reference_closure(self):
        relation = GoldRelation(GoldRelationKind.CONTRADICTS, identity(3))
        a = document(units=(unit(1, relations=(relation,)), unit(2)))
        b = document(2, topics=(topic(2, (3,)),), units=(unit(3),), domain="abilities")
        with self.assertRaises(GoldContractError):
            validate_gold_snapshot(GoldSnapshot((a,)))
        state = validate_gold_snapshot(GoldSnapshot((a, b)))
        self.assertEqual(get_gold_unit(state, identity(1)).relations, (relation,))
        self.assertEqual(get_gold_unit(state, identity(3)).relations, ())
        self.assertEqual(a.units[0].content, unit().content)

    def test_duplicate_identity_wrong_historical_owner_and_cycle_rejected(self):
        with self.assertRaises(GoldContractError):
            validate_gold_snapshot(GoldSnapshot((document(), document(2, domain="abilities"))))
        historical = unit(3, status=GoldStatus.MERGED, merged_into=identity(4))
        a = replace(document(), historical_units=(historical,))
        b = document(2, topics=(topic(2, (4,)),), units=(unit(4),), domain="abilities")
        with self.assertRaises(GoldDocumentError):
            validate_gold_snapshot(GoldSnapshot((a, b)))
        cycle = (unit(3, status=GoldStatus.MERGED, merged_into=identity(4)),
                 unit(4, status=GoldStatus.MERGED, merged_into=identity(3)))
        with self.assertRaises(GoldContractError):
            validate_gold_snapshot(GoldSnapshot((replace(document(), historical_units=cycle),)))
