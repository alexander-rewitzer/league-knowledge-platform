"""Operation-attributed canonical differences; no independent structural mutations."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldCreatePayload, GoldNoChangePayload,
    GoldOperationKind, GoldProvenanceAddition, GoldSnapshot, GoldStatus, GoldUnitPlacement,
    IntegrationError, IntegrationOperationalError, IntegrationRound, PreparedIntegrationResult,
    initialize_gold_store, load_gold_snapshot, prepare_gold_integration,
    publish_gold_snapshot, stage_gold_snapshot, validate_prepared_integration,
)
from league_knowledge_platform.gold_application import integration_effects, logical_gold_hash
from gold_integration_fixtures import BASE, BASE_REF, SUPPORT, FakeAgent, FakeValidator, evaluation, plan, source
from gold_document_fixtures import document, layout, topic
from test_gold_governance_models import examples, new, operation, placement
from test_gold_integration_application import apply
from test_gold_models import identity, unit


class StructuralDeltaTests(unittest.TestCase):
    def assert_unaccounted(self, proposal, base=BASE):
        with self.assertRaises(IntegrationError) as caught:
            apply(proposal, base)
        self.assertEqual(caught.exception.code, "UNACCOUNTED_CANONICAL_DELTA")

    def test_original_no_change_document_rename_and_topic_rename_fail(self):
        for override in (replace(layout(), title="Unauthorized rename"),
                         replace(layout(), topics=(replace(topic(), title="Unauthorized topic"),))):
            self.assert_unaccounted(replace(plan(), proposed_layout=(override,)))

    def test_no_operation_is_blanket_authority_to_rename_existing_structure(self):
        for index in range(7):
            p = plan(index)
            original = p.proposed_layout[0] if p.proposed_layout else layout()
            for override in (replace(original, title="Unrelated title"),
                             replace(original, topics=(replace(original.topics[0], title="Unrelated topic"),))):
                with self.subTest(operation=p.operations[0].kind):
                    self.assert_unaccounted(replace(p, proposed_layout=(override,)))

    def test_unrelated_entity_domain_and_display_metadata_fail(self):
        for override in (replace(layout(), domain_key="different-domain"),
                         replace(layout(), entity=replace(layout().entity, entity_key="other")),
                         replace(layout(), entity=replace(layout().entity, namespace="concepts")),
                         replace(layout(), entity=replace(layout().entity, display_name="Different label"))):
            self.assert_unaccounted(replace(plan(), proposed_layout=(override,)))

    def test_existing_unit_order_or_position_must_not_change(self):
        for placements in ((GoldUnitPlacement(identity(1), 1), GoldUnitPlacement(identity(2), 0)),
                           (GoldUnitPlacement(identity(1), 0), GoldUnitPlacement(identity(2), 3))):
            override = replace(layout(), topics=(replace(topic(), units=placements),))
            self.assert_unaccounted(replace(plan(), proposed_layout=(override,)))

    def test_existing_unit_movement_between_topics_fails(self):
        base = GoldSnapshot((document(topics=(topic(ids=(1,)), topic(2, ids=(2,), position=1)),
                                      units=(unit(), unit(2))),))
        override = layout(topics=(topic(ids=(2,)), topic(2, ids=(1,), position=1)))
        self.assert_unaccounted(replace(plan(), proposed_layout=(override,)), base)

    def test_existing_unit_movement_between_documents_fails(self):
        base = GoldSnapshot((document(topics=(topic(ids=(1,)),), units=(unit(),)),
                             document(2, topics=(topic(2, ids=(2,)),), units=(unit(2),), domain="macro")))
        overrides = (layout(topics=(topic(ids=(2,)),)),
                     layout(2, topics=(topic(2, ids=(1,)),), domain="macro"))
        self.assert_unaccounted(replace(plan(), proposed_layout=overrides), base)

    def test_existing_topic_reorder_and_hierarchy_cleanup_fail(self):
        base = GoldSnapshot((document(topics=(topic(ids=(1,)), topic(2, ids=(2,), position=1)),
                                      units=(unit(), unit(2))),))
        for override in (layout(topics=(topic(ids=(1,), position=1), topic(2, ids=(2,), position=0))),
                         layout(topics=(topic(ids=(1,2)),))):
            self.assert_unaccounted(replace(plan(), proposed_layout=(override,)), base)

    def test_pure_no_change_is_exact_snapshot_and_provenance_only_versions_affected_document(self):
        second = document(2, topics=(topic(2, ids=(4,)),), units=(unit(4),), domain="macro")
        base = GoldSnapshot(BASE.documents + (second,))
        self.assertEqual(apply(plan(), base), base)
        addition = GoldProvenanceAddition(identity(), SUPPORT)
        p = plan(operations=(operation(GoldNoChangePayload((identity(),), (addition,))),))
        candidate = apply(p, base)
        self.assertEqual(candidate.documents[1], second)
        first = candidate.documents[0]
        self.assertEqual(first.document_version, 2)
        self.assertEqual(first.title, base.documents[0].title)
        self.assertEqual(first.entity, base.documents[0].entity)
        self.assertEqual(first.topics, base.documents[0].topics)
        self.assertEqual(first.units[0], replace(base.documents[0].units[0], provenance=base.documents[0].units[0].provenance+(SUPPORT,)))
        self.assertEqual(first.units[1:], base.documents[0].units[1:])
        self.assertEqual(apply(p, candidate), candidate)

    def test_provenance_does_not_authorize_other_structure_changes(self):
        addition = GoldProvenanceAddition(identity(), SUPPORT)
        p = plan(operations=(operation(GoldNoChangePayload((identity(),), (addition,))),),
                 layouts=(replace(layout(), title="Unrelated rename"),))
        self.assert_unaccounted(p)

    def test_create_can_use_existing_topic_or_required_new_topic_or_document(self):
        self.assertEqual(len(apply(plan(1)).documents[0].units), 3)
        intent = replace(new(), placement=placement(0, topic=2))
        p = plan(operations=(operation(GoldCreatePayload(intent), GoldOperationKind.CREATE),),
                 layouts=(layout(topics=(topic(), topic(2, ids=(3,), position=1))),))
        self.assertEqual(len(apply(p).documents[0].topics), 2)
        intent = replace(new(), placement=placement(0, document=2, topic=2))
        p = plan(operations=(operation(GoldCreatePayload(intent), GoldOperationKind.CREATE),),
                 layouts=(layout(2, topics=(topic(2, ids=(3,)),), domain="macro"),))
        candidate = apply(p)
        self.assertEqual(candidate.documents[0], BASE.documents[0])
        self.assertEqual(candidate.documents[1].document_version, 1)

    def test_multiple_creates_may_share_required_new_container(self):
        first = replace(new(), placement=placement(0, document=2, topic=2))
        second = replace(new(4), placement=placement(1, document=2, topic=2))
        p = plan(operations=(operation(GoldCreatePayload(first), GoldOperationKind.CREATE),
                 replace(operation(GoldCreatePayload(second), GoldOperationKind.CREATE), operation_id="create-2")),
                 layouts=(layout(2, topics=(topic(2, ids=(3,4)),), domain="macro"),))
        self.assertEqual(len(apply(p).documents[1].units), 2)

    def test_flag_conflict_new_claim_required_placement_remains_valid(self):
        op = examples()[6]
        claim = replace(op.payload.new_claims[0], placement=placement(0, document=2, topic=2))
        op = replace(op, payload=replace(op.payload, new_claims=(claim,)))
        p = plan(operations=(op,), layouts=(layout(2, topics=(topic(2, ids=(3,)),), domain="macro"),))
        candidate = apply(p)
        self.assertEqual(len(candidate.documents), 2)
        self.assertEqual(candidate.documents[1].units[0].knowledge_id, identity(3))
        self.assertTrue(candidate.documents[0].units[0].relations)

    def test_merge_prunes_empty_topic_and_preserves_history(self):
        history = unit(4, status=GoldStatus.MERGED, merged_into=identity(2))
        base = GoldSnapshot((document(topics=(topic(ids=(1,)), topic(2, ids=(2,), position=1)),
                                      units=(unit(), unit(2)), historical=(history,)),))
        candidate = apply(plan(4), base)
        self.assertEqual(len(candidate.documents[0].topics), 1)
        self.assertEqual(candidate.documents[0].historical_units[-1], history)
        self.assertEqual(candidate.documents[0].historical_units[0].merged_into, identity())

    def test_create_and_merge_may_reuse_only_the_approved_retired_unit_position(self):
        create = operation(GoldCreatePayload(replace(new(), placement=placement(1))), GoldOperationKind.CREATE)
        merge = replace(examples()[4], operation_id="merge-2")
        p = plan(operations=(create, merge), layouts=(layout(topics=(topic(ids=(1,3)),)),))
        candidate = apply(p)
        self.assertEqual(tuple(p.knowledge_id for p in candidate.documents[0].topics[0].units), (identity(), identity(3)))
        self.assertEqual(candidate.documents[0].historical_units[0].knowledge_id, identity(2))

    def test_legacy_prepared_result_with_consistent_hashes_is_rejected(self):
        p = replace(plan(), proposed_layout=(replace(layout(), title="Legacy unaccounted title"),))
        changed = replace(BASE.documents[0], title="Legacy unaccounted title",
                          version=replace(BASE.documents[0].version, document_version=2))
        candidate = GoldSnapshot((changed,))
        result = PreparedIntegrationResult(1, 1, p.source_reference, ("source-item-000001",), BASE_REF,
            BASE, candidate, p, (IntegrationRound(1, p, evaluation()),), "PREPARED_CHANGE",
            integration_effects(BASE,candidate,p,("source-item-000001",)), logical_gold_hash(BASE), logical_gold_hash(candidate))
        with self.assertRaises(IntegrationError) as caught:
            validate_prepared_integration(result)
        self.assertEqual(caught.exception.code, "UNACCOUNTED_CANONICAL_DELTA")

    def test_semantic_pass_cannot_return_unaccounted_prepared_result_or_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            gold = GoldConfig(Path(directory)/"gold")
            base = initialize_gold_store(gold)
            publish_gold_snapshot(stage_gold_snapshot(BASE,config=gold,base=base),config=gold,expected_base=base)
            before = {p: p.read_bytes() for p in gold.root.rglob("*") if p.is_file()}
            def bad(request):
                return replace(plan(base_reference=request.inspection.reference),
                               proposed_layout=(replace(layout(), title="Forbidden"),))
            with patch("league_knowledge_platform.gold_integration.load_integration_source",return_value=source()):
                with self.assertRaises(IntegrationOperationalError) as caught:
                    prepare_gold_integration("B", agent=FakeAgent([bad]), validator=FakeValidator(), gold_config=gold)
            self.assertEqual(caught.exception.code, "UNACCOUNTED_CANONICAL_DELTA")
            self.assertEqual(caught.exception.phase, "APPLICATION")
            self.assertEqual(len(caught.exception.rounds), 1)
            self.assertEqual(load_gold_snapshot(gold).snapshot, BASE)
            self.assertEqual(before,{p: p.read_bytes() for p in gold.root.rglob("*") if p.is_file()})
