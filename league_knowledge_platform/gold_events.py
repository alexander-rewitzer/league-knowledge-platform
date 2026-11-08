"""Pure effect/audit projection and deterministic eight-type event derivation."""

from dataclasses import replace
from types import SimpleNamespace

from .gold_application import integration_effects, _validate_candidate_delta
from .gold_documents import validate_gold_snapshot
from .gold_governance import _new_intents
from .gold_governance_models import (GoldOperationKind, GoldCreatePayload, GoldNewUnitIntent,
                                    GoldProvenanceAddition, GoldRelationIntent)
from .gold_models import GoldContractState, GoldStatus
from .gold_history_models import (
    GoldChangeEvent, GoldEventType as Event, GoldOperationAudit, GoldCreatedPlacement,
    check,
)


def operation_audits(result):
    if result.plan is None:
        return ()
    before = {u.knowledge_id: u for u in validate_gold_snapshot(result.base_snapshot).units}
    values = []
    for o in result.plan.operations:
        p, kind = o.payload, o.kind
        new = tuple(GoldCreatedPlacement(n.unit.knowledge_id, n.placement) for n in _new_intents(p))
        if kind is GoldOperationKind.NO_CHANGE:
            ids = p.target_knowledge_ids
        elif kind is GoldOperationKind.CREATE:
            ids = (p.new_unit.unit.knowledge_id,)
        elif kind in (GoldOperationKind.ENRICH, GoldOperationKind.REWRITE):
            ids = (p.target_knowledge_id,)
        elif kind is GoldOperationKind.MERGE:
            ids = p.target_knowledge_ids
        elif kind is GoldOperationKind.LINK:
            ids = (p.relation.source_knowledge_id, p.relation.relation.target_knowledge_id)
        else:
            ids = p.claim_knowledge_ids
        survivor = p.survivor_unit.knowledge_id if kind is GoldOperationKind.MERGE else None
        support = list(p.provenance_additions) if kind in (GoldOperationKind.NO_CHANGE, GoldOperationKind.FLAG_CONFLICT) else []
        relations = ([p.relation] if kind is GoldOperationKind.LINK else
                     list(p.contradictions) if kind is GoldOperationKind.FLAG_CONFLICT else [])
        resulting = [n.unit for n in _new_intents(p)]
        if kind in (GoldOperationKind.ENRICH, GoldOperationKind.REWRITE):
            resulting.append(p.resulting_unit)
        elif kind is GoldOperationKind.MERGE:
            resulting.append(p.survivor_unit)
        for unit in resulting:
            support.extend(GoldProvenanceAddition(unit.knowledge_id, v) for v in unit.provenance)
            relations.extend(GoldRelationIntent(unit.knowledge_id, v) for v in unit.relations)
        support = tuple(sorted({s for s in support if s.knowledge_id not in before
            or s.provenance not in before[s.knowledge_id].provenance}, key=GoldProvenanceAddition._sort_key))
        relations = tuple(sorted({r for r in relations if r.source_knowledge_id not in before
            or r.relation not in before[r.source_knowledge_id].relations}, key=GoldRelationIntent._sort_key))
        values.append(GoldOperationAudit(o.operation_id, kind, tuple(sorted(r.source_item_id for r in o.source_refs)),
            tuple(sorted(set(ids))), new, support, relations, survivor,
            tuple(i for i in ids if i != survivor) if survivor else (),
            p.conflict_reason if kind is GoldOperationKind.FLAG_CONFLICT else None, o.rationale))
    return tuple(values)


def audited_effects(base, candidate, operations, item_ids):
    effects = integration_effects(base, candidate, None, ())
    return replace(effects,
        merges=tuple((o.operation_id, o.survivor_id, o.merged_ids) for o in operations if o.kind is GoldOperationKind.MERGE),
        conflicts=tuple((o.operation_id, o.affected_knowledge_ids, o.conflict_reason)
                        for o in operations if o.kind is GoldOperationKind.FLAG_CONFLICT),
        source_item_accounting=tuple((i, tuple(o.operation_id for o in operations if i in o.source_item_ids))
                                     for i in sorted(item_ids)))


def validate_audited_state(record, base, candidate):
    """Check persistent effects against actual immutable Gold, never invoke AI."""
    before = {u.knowledge_id: u for u in validate_gold_snapshot(base).units}
    after = {u.knowledge_id: u for u in validate_gold_snapshot(candidate).units}
    check(len({o.operation_id for o in record.operations}) == len(record.operations), "OPERATION_IDS")
    items = set(record.source_item_ids)
    check({i for o in record.operations for i in o.source_item_ids} == items, "SOURCE_ACCOUNTING")
    check(all(set(o.source_item_ids) <= items for o in record.operations), "SOURCE_ACCOUNTING")
    check(set(before) <= set(after), "KNOWLEDGE_LOSS")
    check(all(set(o.affected_knowledge_ids) <= set(after) for o in record.operations), "OPERATION_TARGET")
    placements = tuple(n for o in record.operations for n in o.new_placements)
    check(len({n.knowledge_id for n in placements}) == len(placements)
          and {n.knowledge_id for n in placements} == set(after) - set(before), "CREATION_ACCOUNTING")
    for identity, old in before.items():
        new = after[identity]
        origins = tuple(o for o in record.operations if identity in o.affected_knowledge_ids)
        content_kinds = (GoldOperationKind.ENRICH, GoldOperationKind.REWRITE)
        check(old.content == new.content or any(o.kind in content_kinds
              or o.kind is GoldOperationKind.MERGE and o.survivor_id == identity for o in origins), "CONTENT_AUTHORITY")
        check(old.status == new.status or any(identity in o.merged_ids for o in origins), "STATUS_AUTHORITY")
        check(old.provenance == new.provenance or any(o.kind is not GoldOperationKind.LINK for o in origins), "SUPPORT_AUTHORITY")
        check(old.relations == new.relations or any(o.kind is not GoldOperationKind.NO_CHANGE for o in origins), "RELATION_AUTHORITY")
        check(set(old.provenance) <= set(new.provenance) and set(old.relations) <= set(new.relations), "KNOWLEDGE_PRESERVATION")
        if old.status is GoldStatus.MERGED:
            check(old.content == new.content and old.status == new.status and old.merged_into == new.merged_into, "MERGED_HISTORY")
    retired = {i for i in before if before[i].status is GoldStatus.ACTIVE and after[i].status is GoldStatus.MERGED}
    check(retired == {i for o in record.operations for i in o.merged_ids}, "MERGE_ACCOUNTING")
    for o in record.operations:
        check(set(n.knowledge_id for n in o.new_placements) <= set(o.affected_knowledge_ids), "CREATION_ACCOUNTING")
        check(all(s.knowledge_id in o.affected_knowledge_ids for s in o.support), "SUPPORT_AUTHORITY")
        check(o.kind is not GoldOperationKind.LINK or not o.support, "SUPPORT_AUTHORITY")
        check(o.kind is not GoldOperationKind.NO_CHANGE or not o.relations, "RELATION_AUTHORITY")
        check(all(r.source_knowledge_id in o.affected_knowledge_ids for r in o.relations), "RELATION_AUTHORITY")
        check(all(s.provenance.source_knowledge_artifact != record.source_reference
                  or s.provenance.source_item_id in o.source_item_ids for s in o.support), "SUPPORT_SOURCE")
        if o.kind is GoldOperationKind.CREATE:
            check(len(o.new_placements) == 1 and o.affected_knowledge_ids == (o.new_placements[0].knowledge_id,), "CREATION_ACCOUNTING")
        if o.kind in (GoldOperationKind.ENRICH, GoldOperationKind.REWRITE):
            check(len(o.affected_knowledge_ids) == 1, "OPERATION_TARGET")
        if o.kind is GoldOperationKind.MERGE:
            check(set(o.affected_knowledge_ids) == {o.survivor_id, *o.merged_ids}, "MERGE_ACCOUNTING")
            check(o.survivor_id in before and after[o.survivor_id].status is GoldStatus.ACTIVE, "MERGE_SURVIVOR")
            check(all(after[i].merged_into == o.survivor_id and after[i].content == before[i].content for i in o.merged_ids), "MERGE_HISTORY")
    # Reuse the merged corrective structural invariant with only audited placement
    # facts. This projection contains no second semantic plan or decision logic.
    declarations = tuple(SimpleNamespace(payload=GoldCreatePayload(GoldNewUnitIntent(after[n.knowledge_id], n.placement)))
                         for n in placements)
    from .gold_application import document_layout
    projection = SimpleNamespace(operations=declarations,
        proposed_layout=tuple(document_layout(d) for d in candidate.documents))
    _validate_candidate_delta(projection, base, GoldContractState(1, 1, tuple(after.values())), candidate)
    check(record.effects == audited_effects(base, candidate, record.operations, record.source_item_ids), "EFFECTS")
    check({s for o in record.operations for s in o.support} == set(record.effects.provenance_additions), "SUPPORT_ACCOUNTING")
    check({r for o in record.operations for r in o.relations} == set(record.effects.relation_additions), "RELATION_ACCOUNTING")
    changed = {e.knowledge_id for e in record.effects.units}
    check(changed <= {i for o in record.operations for i in o.affected_knowledge_ids}, "UNACCOUNTED_UNIT_DELTA")
    if record.outcome == "NO_SOURCE_KNOWLEDGE":
        check(not items and not record.operations and not record.rounds and base == candidate, "EMPTY_RECORD")
    else:
        check(bool(items) and bool(record.operations) and 1 <= len(record.rounds) <= 3, "SEMANTIC_RECORD")
        for number, r in enumerate(record.rounds, 1):
            check(r.number == number and r.decision == ("PASS" if number == len(record.rounds) else "REJECT"), "ROUND_HISTORY")
        check(record.outcome == ("PREPARED_NO_CHANGE" if base == candidate else "PREPARED_CHANGE"), "RECORD_OUTCOME")


def derive_gold_events(record, base, candidate):
    before_owner = {u.knowledge_id: d.document_id for d in base.documents for u in d.units + d.historical_units}
    after_owner = {u.knowledge_id: d.document_id for d in candidate.documents for u in d.units + d.historical_units}
    changed = {u.knowledge_id for u in record.effects.units}
    events = []

    def add(kind, ids, origins, *, index=None, survivor=None, merged=(), reason=None, source_items=None):
        ids = tuple(sorted(set(ids)))
        origins = tuple(o for o in record.operations if o.operation_id in set(origins))
        documents = {owner[i] for owner in (before_owner, after_owner) for i in ids if i in owner}
        events.append(GoldChangeEvent(1, "event-" + record.integration_id.split("-", 1)[1] + f"-{len(events)+1:06d}",
            record.integration_id, kind, record.timestamp, record.source_reference,
            tuple(sorted({i for o in origins for i in o.source_item_ids})) if source_items is None else source_items,
            tuple(o.operation_id for o in origins),
            ids, tuple(i for i in ids if i in changed),
            tuple(d for d in record.effects.documents if d.document_id in documents), index, survivor,
            merged, reason, tuple(sorted({o.rationale for o in origins}))))

    if base == candidate:
        add(Event.NO_CHANGE_RECORDED, {i for o in record.operations for i in o.affected_knowledge_ids},
            {o.operation_id for o in record.operations},
            reason="NO_SOURCE_KNOWLEDGE" if record.outcome == "NO_SOURCE_KNOWLEDGE" else "NO_CANONICAL_MUTATION")
        return tuple(events)
    for o in record.operations:
        for n in o.new_placements:
            add(Event.KNOWLEDGE_CREATED, (n.knowledge_id,), (o.operation_id,))
        if o.kind in (GoldOperationKind.ENRICH, GoldOperationKind.REWRITE) and set(o.affected_knowledge_ids) & changed:
            add(Event.KNOWLEDGE_ENRICHED if o.kind is GoldOperationKind.ENRICH else Event.KNOWLEDGE_REWRITTEN,
                o.affected_knowledge_ids, (o.operation_id,))
        if o.kind is GoldOperationKind.MERGE:
            for retired in o.merged_ids:
                add(Event.KNOWLEDGE_MERGED, (o.survivor_id, retired), (o.operation_id,), survivor=o.survivor_id, merged=(retired,))
        if o.kind is GoldOperationKind.FLAG_CONFLICT:
            add(Event.CONFLICT_FLAGGED, o.affected_knowledge_ids, (o.operation_id,), reason=o.conflict_reason)
    for index, support in enumerate(record.effects.provenance_additions):
        origins = tuple(o.operation_id for o in record.operations if support in o.support)
        items = ((support.provenance.source_item_id,) if support.provenance.source_knowledge_artifact == record.source_reference else None)
        add(Event.PROVENANCE_ADDED, (support.knowledge_id,), origins, index=index, source_items=items)
    for index, edge in enumerate(record.effects.relation_additions):
        ids = (edge.source_knowledge_id, edge.relation.target_knowledge_id)
        origins = tuple(o.operation_id for o in record.operations if edge in o.relations)
        add(Event.RELATION_ADDED, ids, origins, index=index)
    check(bool(events), "EVENT_DERIVATION")
    return tuple(events)
