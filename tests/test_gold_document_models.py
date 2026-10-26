from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest
from unittest.mock import patch

import league_knowledge_platform as package
from league_knowledge_platform import (
    GoldConfig, GoldContractError, GoldDocumentError, GoldEntity, GoldSnapshot,
    GoldTopic, GoldUnitPlacement, new_topic_id,
)
from gold_document_fixtures import document, layout, topic
from test_gold_models import identity, unit


class GoldDocumentModelTests(unittest.TestCase):
    def test_exports_unique_and_models_immutable(self):
        self.assertEqual(len(package.__all__), len(set(package.__all__)))
        namespace = {}
        exec("from league_knowledge_platform import *", namespace)
        for name in ("GoldDocument", "GoldSnapshot", "stage_gold_snapshot", "decode_gold_document_pair"):
            self.assertIs(namespace[name], getattr(package, name))
        with self.assertRaises(FrozenInstanceError):
            document().title = "changed"

    def test_explicit_topic_generation_and_stable_document_identity(self):
        with patch("league_knowledge_platform.gold_document_models.uuid4") as generate:
            generate.return_value.hex = "a" * 32
            self.assertEqual(new_topic_id(), "topic-" + "a" * 32)
        before = document()
        after = replace(before, title="New title", domain_key="wave-management")
        self.assertEqual(before.document_id, after.document_id)
        self.assertEqual(before.units, after.units)

    def test_generic_entities_do_not_require_champion_or_fixed_domain(self):
        for entity in (GoldEntity("concepts", "tempo", "Tempo"), GoldEntity("items", "example", "Example")):
            self.assertEqual(replace(document(), entity=entity).entity, entity)

    def test_positions_normalize_input_order_without_changing_explicit_order(self):
        a = topic(position=4)
        b = topic(2, (3,), position=2, title="Trading")
        d = document(topics=(a, b), units=(unit(3), unit(2), unit(1)))
        self.assertEqual(d.topics, (b, a))
        self.assertEqual(tuple(u.knowledge_id for u in d.units), tuple(identity(n) for n in (1, 2, 3)))

    def test_duplicate_position_id_and_malformed_fields(self):
        factories = (
            lambda: topic(position=True), lambda: topic(position=-1),
            lambda: GoldUnitPlacement("invalid", 0),
            lambda: GoldTopic("topic-" + "a" * 32, "ok", 0, [GoldUnitPlacement(identity(), 0)]),
            lambda: replace(topic(), units=(GoldUnitPlacement(identity(), 1), GoldUnitPlacement(identity(2), 1))),
            lambda: layout(topics=(topic(), topic())),
            lambda: layout(topics=(topic(), topic(2, (3,), position=0))),
            lambda: replace(document(), title="two\nlines"),
            lambda: replace(document(), entity="Ahri"),
            lambda: GoldEntity("../escape", "ahri", "Ahri"),
            lambda: GoldSnapshot((document(), document())),
        )
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(GoldContractError):
                factory()

    def test_empty_only_allowed_in_draft_layout_and_entire_snapshot(self):
        self.assertEqual(GoldSnapshot().documents, ())
        self.assertEqual(layout(topics=()).topics, ())
        for changes in ({"topics": ()}, {"topics": (topic(ids=()),)}, {"units": ()}):
            with self.subTest(changes=changes), self.assertRaises(GoldDocumentError):
                replace(document(), **changes)

    def test_default_root_and_explicit_override_are_nonsecret(self):
        self.assertEqual(GoldConfig().root, Path("data/gold").absolute())
        self.assertEqual(GoldConfig("custom-gold").root, Path("custom-gold").absolute())

    def test_nested_model_tampering_is_revalidated_at_canonical_boundaries(self):
        from league_knowledge_platform import validate_gold_snapshot
        for mutate in (lambda d: object.__setattr__(d.entity, "namespace", "../escape"),
                       lambda d: object.__setattr__(d.topics[0].units[0], "position", True),
                       lambda d: object.__setattr__(d.version, "document_version", 0)):
            d = document()
            snapshot = GoldSnapshot((d,))
            mutate(d)
            with self.subTest(mutate=mutate), self.assertRaises(GoldContractError):
                validate_gold_snapshot(snapshot)
