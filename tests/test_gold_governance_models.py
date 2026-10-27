"""Synthetic transient governance fixtures and immutable operation contracts."""

from dataclasses import FrozenInstanceError, replace
import unittest

from league_knowledge_platform import (
    GOLD_GOVERNANCE_RULES, GoldCreatePayload, GoldEnrichPayload, GoldFlagConflictPayload,
    GoldGovernanceError, GoldIntegrationOperation, GoldLinkPayload, GoldMergePayload,
    GoldNewUnitIntent, GoldNoChangePayload, GoldOperationKind, GoldPlacementIntent,
    GoldProvenance, GoldProvenanceAddition, GoldRelation, GoldRelationIntent,
    GoldRelationKind, GoldRewritePayload, GoldStatus, validate_gold_operation_shape,
)
from test_gold_models import identity, reference, unit


SUPPORT = GoldProvenance(reference("B"), "source-item-000001")


def placement(position=2, document=1, topic=1):
    return GoldPlacementIntent(f"gdoc-{document:032x}", f"topic-{topic:032x}", position)


def new(number=3, *, support=SUPPORT, position=2):
    return GoldNewUnitIntent(unit(number, provenance=(support,)), placement(position))


def edge(a=1, b=2, kind=GoldRelationKind.RELATED_TO):
    return GoldRelationIntent(identity(a), GoldRelation(kind, identity(b)))


def operation(payload=None, kind=GoldOperationKind.NO_CHANGE, refs=(SUPPORT,)):
    return GoldIntegrationOperation("op-1", kind, refs, "Synthetic integration rationale.",
                                    GoldNoChangePayload((identity(),)) if payload is None else payload)


def examples():
    a = unit(provenance=unit().provenance + (SUPPORT,))
    return (
        operation(),
        operation(GoldCreatePayload(new()), GoldOperationKind.CREATE),
        operation(GoldEnrichPayload(identity(), a), GoldOperationKind.ENRICH),
        operation(GoldRewritePayload(identity(), a), GoldOperationKind.REWRITE),
        operation(GoldMergePayload((identity(), identity(2)), a), GoldOperationKind.MERGE),
        operation(GoldLinkPayload(edge()), GoldOperationKind.LINK),
        operation(GoldFlagConflictPayload((identity(), identity(3)), (new(),),
            (edge(1, 3, GoldRelationKind.CONTRADICTS),), (), "Unresolved incompatible claims."),
            GoldOperationKind.FLAG_CONFLICT),
    )


class GoldGovernanceModelTests(unittest.TestCase):
    def test_exact_vocabulary_and_frozen_appropriateness_catalogue(self):
        self.assertEqual({k.value for k in GoldOperationKind},
            {"NO_CHANGE", "CREATE", "ENRICH", "REWRITE", "MERGE", "LINK", "FLAG_CONFLICT"})
        self.assertEqual(tuple(r.kind for r in GOLD_GOVERNANCE_RULES), tuple(GoldOperationKind))
        self.assertTrue(all(r.applies_when and r.preserve and r.prohibited and r.ownership for r in GOLD_GOVERNANCE_RULES))
        with self.assertRaises(FrozenInstanceError):
            GOLD_GOVERNANCE_RULES[0].prohibited = ""
        for changes in ({"preserve": []}, {"kind": "NO_CHANGE"}, {"ownership": " "}):
            with self.subTest(changes=changes), self.assertRaises(GoldGovernanceError):
                replace(GOLD_GOVERNANCE_RULES[0], **changes)
        for bad in ("DELETE", "REMOVE", "DROP", "PURGE", "NO_CHANGE", None, True):
            with self.subTest(bad=bad), self.assertRaises(GoldGovernanceError):
                replace(operation(), kind=bad)

    def test_all_seven_payloads_revalidate_and_public_exports_are_unique(self):
        import league_knowledge_platform as package
        for op in examples():
            self.assertIs(validate_gold_operation_shape(op), op)
        self.assertEqual(len(package.__all__), len(set(package.__all__)))
        for name in ("GoldIntegrationOperation", "GoldGovernanceError", "validate_gold_operation", "encode_gold_operation"):
            self.assertIn(name, package.__all__)

    def test_wrong_payload_unknown_fields_and_empty_common_fields_fail(self):
        for changes in ({"payload": {}}, {"kind": GoldOperationKind.CREATE}, {"operation_id": " "},
                        {"rationale": ""}, {"source_refs": ()}, {"source_refs": [SUPPORT]},
                        {"source_refs": (SUPPORT, SUPPORT)}, {"source_refs": (None,)}):
            with self.subTest(changes=changes), self.assertRaises(GoldGovernanceError):
                replace(operation(), **changes)
        with self.assertRaises(TypeError):
            GoldNoChangePayload((identity(),), winner=identity())

    def test_no_change_support_target_and_duplicates(self):
        add = GoldProvenanceAddition(identity(), SUPPORT)
        self.assertEqual(GoldNoChangePayload((identity(),), (add,)).provenance_additions, (add,))
        for args in (((),), ((identity(), identity()),), ((identity(),), (add, add)),
                     ((identity(2),), (add,))):
            with self.subTest(args=args), self.assertRaises(GoldGovernanceError):
                GoldNoChangePayload(*args)

    def test_stable_ids_and_placement_are_strict(self):
        for invalid in ("README.md:12", "gdoc-" + "a" * 32, "Heading", 1, None, "gku-" + "F" * 32):
            with self.subTest(invalid=invalid), self.assertRaises(GoldGovernanceError):
                GoldNoChangePayload((invalid,))
        for changes in ({"document_id": "path/file.md"}, {"topic_id": "heading"},
                        {"position": True}, {"position": -1}, {"position": 1.0}):
            with self.subTest(changes=changes), self.assertRaises(GoldGovernanceError):
                replace(placement(), **changes)

    def test_new_and_replacement_units_require_active_status_and_identity(self):
        historical = unit(3, status=GoldStatus.MERGED, merged_into=identity())
        with self.assertRaises(GoldGovernanceError):
            GoldNewUnitIntent(historical, placement())
        for cls in (GoldEnrichPayload, GoldRewritePayload):
            for result in (unit(2), historical, None):
                with self.subTest(cls=cls, result=result), self.assertRaises(GoldGovernanceError):
                    cls(identity(), result)

    def test_merge_requires_distinct_members_and_one_member_survivor(self):
        for targets, survivor in (((identity(),), unit()), ((identity(), identity()), unit()),
            ((identity(), identity(2)), unit(3)), ((identity(), identity(2)),
             unit(status=GoldStatus.MERGED, merged_into=identity(3)))):
            with self.subTest(targets=targets), self.assertRaises(GoldGovernanceError):
                GoldMergePayload(targets, survivor)

    def test_relations_and_conflicts_have_distinct_supported_endpoints(self):
        with self.assertRaises(GoldGovernanceError):
            edge(1, 1)
        valid = examples()[-1].payload
        for changes in ({"claim_knowledge_ids": (identity(),)}, {"contradictions": ()},
                        {"contradictions": (edge(1, 3),)},
                        {"contradictions": (edge(1, 3, GoldRelationKind.CONTRADICTS),) * 2},
                        {"claim_knowledge_ids": (identity(), identity(2), identity(3))},
                        {"conflict_reason": " "}, {"new_claims": (new(), new())}):
            with self.subTest(changes=changes), self.assertRaises(GoldGovernanceError):
                replace(valid, **changes)

    def test_immutable_values_and_nested_tampering_are_checked_without_content_leak(self):
        op = examples()[1]
        with self.assertRaises(FrozenInstanceError):
            op.kind = GoldOperationKind.LINK
        object.__setattr__(op.payload.new_unit.unit, "content", "")
        with self.assertRaises(GoldGovernanceError) as caught:
            validate_gold_operation_shape(op)
        self.assertNotIn("Synthetic", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
