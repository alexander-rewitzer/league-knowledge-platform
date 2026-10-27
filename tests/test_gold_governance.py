"""Read-only operation checks against explicit source, unit and layout context."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldCreatePayload, GoldEnrichPayload, GoldFlagConflictPayload, GoldGovernanceError,
    GoldLinkPayload, GoldMergePayload, GoldNewUnitIntent, GoldNoChangePayload,
    GoldOperationKind, GoldProvenance, GoldProvenanceAddition, GoldRelation,
    GoldRelationKind, GoldRewritePayload, GoldSnapshot, GoldStatus,
    encode_gold_document_metadata, render_gold_document, validate_gold_operation,
)
from gold_document_fixtures import document, layout, topic
from test_gold_models import identity, reference, unit
from test_gold_governance_models import SUPPORT, edge, examples, new, operation, placement


def context(**changes):
    result = dict(base_snapshot=GoldSnapshot((document(units=(unit(), unit(2))),)),
                  incoming_artifact=reference("B"), incoming_item_ids=frozenset({SUPPORT.source_item_id}))
    result.update(changes)
    return result


def validate(op, **changes):
    return validate_gold_operation(op, **context(**changes))


class GoldGovernanceTests(unittest.TestCase):
    def test_all_operations_validate_against_real_model_context(self):
        for op in examples():
            with self.subTest(kind=op.kind):
                self.assertIs(validate(op), op)

    def test_invalid_source_context_and_exact_artifact_membership(self):
        for changes in ({"incoming_item_ids": set()}, {"incoming_item_ids": frozenset({None})},
                        {"incoming_item_ids": frozenset()}, {"incoming_artifact": reference("C")},
                        {"incoming_artifact": reference("B", sha256="c" * 64)}, {"base_snapshot": None}):
            with self.subTest(changes=changes), self.assertRaises(GoldGovernanceError):
                validate(operation(), **changes)

    def test_no_change_support_absent_existing_or_empty_does_not_derive_effects(self):
        addition = GoldProvenanceAddition(identity(), SUPPORT)
        op = operation(GoldNoChangePayload((identity(),), (addition,)))
        self.assertIs(validate(op), op)
        supported = replace(unit(), provenance=unit().provenance + (SUPPORT,))
        snapshot = GoldSnapshot((document(units=(supported, unit(2))),))
        self.assertIs(validate(op, base_snapshot=snapshot), op)
        self.assertIs(validate(operation(), base_snapshot=snapshot).payload.__class__, GoldNoChangePayload)
        self.assertEqual(snapshot.documents[0].units[0], supported)

    def test_no_change_requires_existing_active_target_and_declared_support(self):
        for target in (identity(99), identity(3)):
            op = operation(GoldNoChangePayload((target,)))
            with self.assertRaises(GoldGovernanceError):
                validate(op, declared_new_units=(new().unit,))
        op = operation(GoldNoChangePayload((identity(),),
            (GoldProvenanceAddition(identity(), GoldProvenance(reference("B"), "unknown")),)))
        with self.assertRaises(GoldGovernanceError):
            validate(op)

    def test_create_never_overwrites_existing_identity_or_loses_support(self):
        for intent in (replace(new(), unit=unit()), replace(new(), unit=unit(3))):
            with self.subTest(intent=intent), self.assertRaises(GoldGovernanceError):
                validate(operation(GoldCreatePayload(intent), GoldOperationKind.CREATE))

    def test_create_supports_empty_gold_new_document_and_independent_concept(self):
        op = examples()[1]
        proposed = layout(topics=(topic(ids=()),))
        self.assertIs(validate(op, base_snapshot=GoldSnapshot(), proposed_layout=(proposed,)), op)
        concept = replace(proposed, entity=replace(proposed.entity, namespace="concepts", entity_key="wave-control"))
        self.assertIs(validate(op, base_snapshot=GoldSnapshot(), proposed_layout=(concept,)), op)

    def test_create_existing_topic_new_topic_and_new_document(self):
        op = examples()[1]
        self.assertIs(validate(op), op)
        intent = replace(new(), placement=placement(0, topic=2))
        proposed = layout(topics=(topic(), topic(2, (), position=1)))
        self.assertIs(validate(operation(GoldCreatePayload(intent), GoldOperationKind.CREATE),
                               proposed_layout=(proposed,)).payload.new_unit, intent)
        intent = replace(new(), placement=placement(0, document=2))
        self.assertIs(validate(operation(GoldCreatePayload(intent), GoldOperationKind.CREATE),
                               proposed_layout=(layout(2, topics=(topic(ids=()),)),)).payload.new_unit, intent)

    def test_unknown_colliding_and_inconsistent_placements_are_rejected(self):
        for p in (placement(0), placement(document=99), placement(topic=99)):
            with self.subTest(p=p), self.assertRaises(GoldGovernanceError):
                validate(operation(GoldCreatePayload(replace(new(), placement=p)), GoldOperationKind.CREATE))
        for layouts in ((layout(), layout()), (layout(topics=(topic(ids=(99,)),)),)):
            with self.subTest(layouts=layouts), self.assertRaises(GoldGovernanceError):
                validate(examples()[1], proposed_layout=layouts)
        with self.assertRaises(GoldGovernanceError):
            validate(examples()[1], proposed_layout=(layout(topics=(topic(ids=(3,)),)),))

    def test_enrich_and_rewrite_preserve_original_provenance_relations_and_identity(self):
        old = unit(relations=(GoldRelation(GoldRelationKind.RELATED_TO, identity(2)),))
        snapshot = GoldSnapshot((document(units=(old, unit(2))),))
        for cls, kind in ((GoldEnrichPayload, GoldOperationKind.ENRICH), (GoldRewritePayload, GoldOperationKind.REWRITE)):
            complete = replace(old, content="Complete resulting integrated content.", provenance=old.provenance + (SUPPORT,))
            op = operation(cls(identity(), complete), kind)
            self.assertIs(validate(op, base_snapshot=snapshot), op)
            for invalid in (replace(complete, provenance=(SUPPORT,)), replace(complete, relations=()),
                            replace(complete, provenance=old.provenance),
                            replace(complete, provenance=complete.provenance + (GoldProvenance(reference("C"), "one"),))):
                with self.subTest(cls=cls, invalid=invalid), self.assertRaises(GoldGovernanceError):
                    validate(operation(cls(identity(), invalid), kind), base_snapshot=snapshot)

    def test_structural_validation_does_not_judge_cosmetic_or_semantic_changes(self):
        # The later semantic Validator must reject an inappropriate but structurally valid rewrite.
        result = replace(unit(), content=" " + unit().content, provenance=unit().provenance + (SUPPORT,))
        self.assertIs(validate(operation(GoldRewritePayload(identity(), result), GoldOperationKind.REWRITE)).payload.resulting_unit, result)

    def test_merge_requires_existing_active_targets_and_union_of_provenance(self):
        other = unit(2, provenance=(GoldProvenance(reference("C"), "other"),))
        snapshot = GoldSnapshot((document(units=(unit(), other)),))
        survivor = replace(unit(), provenance=unit().provenance + other.provenance + (SUPPORT,))
        op = operation(GoldMergePayload((identity(2), identity()), survivor), GoldOperationKind.MERGE)
        self.assertIs(validate(op, base_snapshot=snapshot), op)
        self.assertEqual(snapshot.documents[0].units[1], other)
        with self.assertRaises(GoldGovernanceError):
            validate(replace(op, payload=replace(op.payload, survivor_unit=replace(survivor, provenance=(SUPPORT,)))), base_snapshot=snapshot)
        with self.assertRaises(GoldGovernanceError):
            validate(replace(op, payload=replace(op.payload, target_knowledge_ids=(identity(), identity(99)))))

    def test_historical_chains_remain_unchanged_and_cannot_be_mutation_targets(self):
        history = unit(3, status=GoldStatus.MERGED, merged_into=identity(2))
        snapshot = GoldSnapshot((document(units=(unit(), unit(2)), historical=(history,)),))
        validate(examples()[4], base_snapshot=snapshot)
        self.assertEqual(snapshot.documents[0].historical_units, (history,))
        for op in (operation(GoldNoChangePayload((identity(3),))),
                   operation(GoldEnrichPayload(identity(3), replace(new().unit, provenance=(SUPPORT,))), GoldOperationKind.ENRICH),
                   operation(GoldMergePayload((identity(), identity(3)), examples()[4].payload.survivor_unit), GoldOperationKind.MERGE)):
            with self.subTest(kind=op.kind), self.assertRaises(GoldGovernanceError):
                validate(op, base_snapshot=snapshot)

    def test_link_preserves_direction_existing_edges_and_historical_addressing(self):
        history = unit(3, status=GoldStatus.MERGED, merged_into=identity(2))
        old = unit(relations=(GoldRelation(GoldRelationKind.CONTRADICTS, identity(3)),))
        snapshot = GoldSnapshot((document(units=(old, unit(2)), historical=(history,)),))
        op = operation(GoldLinkPayload(edge(1, 3, GoldRelationKind.CONTRADICTS)), GoldOperationKind.LINK)
        self.assertIs(validate(op, base_snapshot=snapshot), op)
        self.assertEqual(snapshot.documents[0].units[1].relations, ())
        self.assertEqual(op.payload.relation.relation.target_knowledge_id, identity(3))
        with self.assertRaises(GoldGovernanceError):
            validate(operation(GoldLinkPayload(edge(1, 99)), GoldOperationKind.LINK))

    def test_link_can_reference_explicit_new_declarations_without_treating_them_as_existing(self):
        op = operation(GoldLinkPayload(edge(1, 3)), GoldOperationKind.LINK)
        self.assertIs(validate(op, declared_new_units=(new().unit,)), op)
        for declarations in ([new().unit], (new().unit, new().unit), (unit(),),
                             (unit(3, provenance=(GoldProvenance(reference("C"), "unknown"),)),)):
            with self.subTest(declarations=declarations), self.assertRaises(GoldGovernanceError):
                validate(op, declared_new_units=declarations)

    def test_conflict_preserves_existing_and_new_claims_without_winner(self):
        op = examples()[-1]
        self.assertIs(validate(op), op)
        with self.assertRaises(GoldGovernanceError):
            validate(op, declared_new_units=(replace(new().unit, content="Different declaration"),))
        self.assertIs(validate(op, declared_new_units=(new().unit,)), op)
        unknown = replace(op.payload, new_claims=())
        with self.assertRaises(GoldGovernanceError):
            validate(replace(op, payload=unknown))

    def test_conflicting_incoming_claims_are_representable_in_empty_gold(self):
        a, b = new(3, position=0), new(4, position=1)
        payload = GoldFlagConflictPayload((identity(3), identity(4)), (a, b),
            (edge(3, 4, GoldRelationKind.CONTRADICTS),), (), "Source preserves incompatible claims.")
        op = operation(payload, GoldOperationKind.FLAG_CONFLICT)
        self.assertIs(validate(op, base_snapshot=GoldSnapshot(), proposed_layout=(layout(topics=(topic(ids=()),)),)), op)
        with self.assertRaises(GoldGovernanceError):
            validate(replace(op, payload=replace(payload, new_claims=(a, replace(b, placement=a.placement)))),
                     base_snapshot=GoldSnapshot(), proposed_layout=(layout(topics=(topic(ids=()),)),))

    def test_invalid_nested_context_is_sanitized(self):
        snapshot = context()["base_snapshot"]
        object.__setattr__(snapshot.documents[0].units[0], "status", "bad")
        with self.assertRaises(GoldGovernanceError) as caught:
            validate(operation(), base_snapshot=snapshot)
        self.assertNotIn("Preserve", str(caught.exception))

    def test_validation_has_no_storage_provider_events_or_mutation_side_effects(self):
        ctx = context()
        before = tuple((render_gold_document(d), encode_gold_document_metadata(d)) for d in ctx["base_snapshot"].documents)
        with patch("builtins.open", side_effect=AssertionError("No files")), \
             patch("sqlite3.connect", side_effect=AssertionError("No SQL")), \
             patch("socket.create_connection", side_effect=AssertionError("No providers")), \
             patch("league_knowledge_platform.gold_store.stage_gold_snapshot", side_effect=AssertionError("No staging")), \
             patch("league_knowledge_platform.gold_store.publish_gold_snapshot", side_effect=AssertionError("No publication")):
            for op in examples():
                self.assertIs(validate_gold_operation(op, **ctx), op)
        after = tuple((render_gold_document(d), encode_gold_document_metadata(d)) for d in ctx["base_snapshot"].documents)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
