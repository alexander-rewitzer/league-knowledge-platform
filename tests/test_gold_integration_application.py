"""Complete deterministic operation application and preparation integrity."""

from dataclasses import replace
import unittest

from league_knowledge_platform import (
    GoldContractError, GoldEnrichPayload, GoldLinkPayload, GoldNoChangePayload, GoldOperationKind,
    GoldProvenanceAddition, GoldRelation, GoldRelationIntent, GoldRelationKind, GoldRewritePayload,
    GoldSnapshot, GoldStatus, IntegrationError, validate_gold_snapshot,
)
from league_knowledge_platform.gold_application import apply_integration_plan, integration_effects, logical_gold_hash
from gold_integration_fixtures import BASE, BASE_REF, META, SUPPORT, plan
from gold_document_fixtures import document, layout, topic
from test_gold_models import identity, unit
from test_gold_governance_models import examples, operation, edge


def apply(p, base=BASE):
    return apply_integration_plan(p, base=base, source_reference=SUPPORT.source_knowledge_artifact,
        source_item_ids=frozenset({'source-item-000001'}), base_reference=BASE_REF)


class ApplicationTests(unittest.TestCase):
    def test_all_seven_operations_construct_complete_valid_candidates(self):
        for n in range(7):
            with self.subTest(operation=n):
                p = plan(n)
                candidate = apply(p)
                validate_gold_snapshot(candidate)
                self.assertEqual(candidate, apply(p))
                self.assertEqual(logical_gold_hash(candidate), logical_gold_hash(apply(p)))
                self.assertEqual(len(candidate.documents), 1)
                self.assertEqual(candidate.documents[0].document_version, 1 if n == 0 else 2)

    def test_enrich_rewrite_complete_content_id_support_and_relation_preservation(self):
        old = unit(relations=(GoldRelation(GoldRelationKind.RELATED_TO, identity(2)),))
        base = GoldSnapshot((document(units=(old, unit(2))),))
        for kind, payload in ((GoldOperationKind.ENRICH, GoldEnrichPayload), (GoldOperationKind.REWRITE, GoldRewritePayload)):
            desired = replace(old, content='Full conditions and explanation.', provenance=old.provenance+(SUPPORT,))
            op = operation(payload(identity(), desired), kind)
            p = plan(operations=(op,))
            self.assertEqual(apply(p, base).documents[0].units[0], desired)
            for bad in (replace(desired, provenance=(SUPPORT,)), replace(desired, relations=())):
                with self.assertRaises(GoldContractError):
                    apply(replace(p, operations=(replace(op, payload=payload(identity(), bad)),)), base)

    def test_no_change_new_support_differs_from_pure_noop_without_duplicate_support(self):
        addition = GoldProvenanceAddition(identity(), SUPPORT)
        op = operation(GoldNoChangePayload((identity(),), (addition,)))
        p = plan(operations=(op,))
        candidate = apply(p)
        effects = integration_effects(BASE, candidate, p, ('source-item-000001',))
        self.assertEqual(effects.provenance_additions, (addition,))
        self.assertEqual(effects.source_item_accounting, (('source-item-000001', ('op-1',)),))
        self.assertEqual(apply(p, candidate), candidate)
        self.assertEqual(apply(plan()), BASE)

    def test_merge_preserves_original_history_chains_and_prunes_empty_document(self):
        history = unit(3, status=GoldStatus.MERGED, merged_into=identity(2))
        first = document(topics=(topic(ids=(1,)),), units=(unit(),))
        second = document(2, topics=(topic(2, ids=(2,)),), units=(unit(2),), historical=(history,), domain='macro')
        base = GoldSnapshot((first, second))
        result = apply(plan(4), base)
        self.assertEqual(len(result.documents), 1)
        histories = {u.knowledge_id:u for u in result.documents[0].historical_units}
        self.assertEqual(histories[identity(3)], history)
        self.assertEqual(histories[identity(2)].content, unit(2).content)
        self.assertEqual(histories[identity(2)].merged_into, identity())
        effects = integration_effects(base, result, plan(4), ('source-item-000001',))
        self.assertEqual(effects.merges[0][1:], (identity(), (identity(2),)))
        self.assertTrue(any(d.proposed_version is None for d in effects.documents))

    def test_link_is_directional_idempotent_and_conflict_retains_claims(self):
        candidate = apply(plan(5))
        by_id = {u.knowledge_id:u for u in candidate.documents[0].units}
        self.assertEqual(by_id[identity()].relations, (edge().relation,))
        self.assertEqual(by_id[identity(2)].relations, ())
        self.assertEqual(apply(plan(5), candidate), candidate)
        conflict = apply(plan(6))
        self.assertEqual(len(conflict.documents[0].units), 3)
        self.assertEqual(conflict.documents[0].units[0].relations[0].kind, GoldRelationKind.CONTRADICTS)
        self.assertTrue(integration_effects(BASE, conflict, plan(6), ('source-item-000001',)).conflicts)

    def test_missing_duplicate_and_inconsistent_accounting_fails(self):
        from league_knowledge_platform.gold_application import validate_integration_plan
        p = plan()
        with self.assertRaises(IntegrationError):
            validate_integration_plan(p, base=BASE, source_reference=p.source_reference,
                source_item_ids=frozenset({'source-item-000001','source-item-000002'}),base_reference=BASE_REF)
        with self.assertRaises(IntegrationError): replace(p, operations=p.operations*2)
        op = examples()[2]
        with self.assertRaises(IntegrationError): apply(plan(operations=(op,replace(op,operation_id='op-2'))))
        with self.assertRaises(GoldContractError): replace(op, source_refs=op.source_refs*2)

    def test_multiple_items_per_operation_and_same_item_supporting_several_units_are_allowed(self):
        extra = replace(SUPPORT,source_item_id='source-item-000002')
        op = replace(operation(), source_refs=(SUPPORT,extra))
        p = plan(operations=(op,))
        result = apply_integration_plan(p, base=BASE, source_reference=p.source_reference,
            source_item_ids=frozenset({'source-item-000001','source-item-000002'}),base_reference=BASE_REF)
        self.assertEqual(result,BASE)
        self.assertEqual(apply(plan(operations=(operation(),replace(operation(),operation_id='op-2')))),BASE)

    def test_new_placement_omission_or_existing_unit_loss_is_rejected(self):
        for override in ((), (layout(topics=(topic(ids=(1,2)),)),), (layout(topics=(topic(ids=(3,)),)),)):
            with self.subTest(override=override), self.assertRaises(GoldContractError):
                apply(replace(plan(1), proposed_layout=override))

    def test_wrong_binding_unknown_target_and_fabricated_provenance_fail(self):
        for p in (replace(plan(),base_reference=replace(BASE_REF,storage_id='c'*32)),
                  replace(plan(),source_reference=replace(SUPPORT.source_knowledge_artifact,sha256='f'*64)),
                  plan(operations=(operation(GoldNoChangePayload((identity(99),))),))):
            with self.assertRaises(GoldContractError): apply(p)

    def test_complete_candidate_validation_rejects_empty_and_missing_reference(self):
        with self.assertRaises(GoldContractError):
            apply(replace(plan(1),proposed_layout=(layout(topics=()),)))
        op=operation(GoldLinkPayload(GoldRelationIntent(identity(),GoldRelation(GoldRelationKind.RELATED_TO,identity(99)))),GoldOperationKind.LINK)
        with self.assertRaises(GoldContractError): apply(plan(operations=(op,)))

    def test_layout_reorganization_versions_only_affected_documents(self):
        second=document(2,topics=(topic(2,ids=(4,)),),units=(unit(4),),domain='macro')
        base=GoldSnapshot(BASE.documents+(second,))
        p=replace(plan(),proposed_layout=(replace(layout(),title='Updated organization'),))
        candidate=apply(p,base)
        self.assertEqual(candidate.documents[0].document_version,2)
        self.assertEqual(candidate.documents[1],second)
