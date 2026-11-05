"""Pure deterministic complete-state construction; never stages or publishes Gold."""

from dataclasses import dataclass, field, replace
from hashlib import sha256

from .gold_contract import validate_gold_state
from .gold_document_models import GoldDocumentLayout, GoldSnapshot
from .gold_documents import (normalize_gold_structure, validate_gold_snapshot, validate_document_transition,
    gold_document_metadata, render_gold_document, encode_gold_document_metadata, decode_gold_document_pair)
from .gold_governance import validate_gold_operation, _new_intents
from .gold_governance_models import (GoldCreatePayload, GoldEnrichPayload, GoldRewritePayload, GoldMergePayload,
    GoldLinkPayload, GoldNoChangePayload, GoldFlagConflictPayload, GoldProvenanceAddition, GoldRelationIntent)
from .gold_models import GoldContractState, GoldKnowledgeUnit, GoldStatus
from .gold_integration_models import IntegrationPlan, require


def logical_gold_hash(snapshot):
    # Pair encodings can also hash one affected document whose references resolve
    # only in the complete corpus. Global closure is validated at the caller.
    require(type(snapshot) is GoldSnapshot, "SNAPSHOT")
    digest = sha256()
    for document in snapshot.documents:
        for content in (render_gold_document(document), encode_gold_document_metadata(document)):
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def document_layout(document):
    return GoldDocumentLayout(document.version, document.title, document.entity, document.domain_key, document.topics)


def validate_integration_plan(plan, *, base, source_reference, source_item_ids, base_reference):
    require(type(plan) is IntegrationPlan, "PLAN")
    replace(plan)
    require(plan.source_reference == source_reference and plan.base_reference == base_reference, "PLAN_BINDING")
    require(type(source_item_ids) is frozenset and bool(source_item_ids), "SOURCE_ITEMS")
    base_state = validate_gold_snapshot(base)
    new = tuple(n.unit for o in plan.operations for n in _new_intents(o.payload))
    require(len({u.knowledge_id for u in new}) == len(new), "DUPLICATE_NEW_IDENTITY")
    covered = set()
    writers = set()
    new_ids = {u.knowledge_id for u in new}
    for operation in plan.operations:
        validate_gold_operation(operation, base_snapshot=base, incoming_artifact=source_reference,
            incoming_item_ids=source_item_ids, declared_new_units=new, proposed_layout=plan.proposed_layout)
        covered.update(p.source_item_id for p in operation.source_refs)
        p = operation.payload
        changed = ({p.target_knowledge_id} if type(p) in (GoldEnrichPayload, GoldRewritePayload)
                   else set(p.target_knowledge_ids) if type(p) is GoldMergePayload else set())
        require(not writers & changed, "OVERLAPPING_RESULTS")
        writers.update(changed)
    require(covered == set(source_item_ids), "SOURCE_ITEM_COVERAGE")
    layouts = {d.document_id: document_layout(d) for d in base.documents}
    layouts.update({d.document_id: d for d in plan.proposed_layout})
    # A new declaration must be placed exactly where its operation explicitly says.
    placed = [(d.document_id, t.topic_id, p.position, p.knowledge_id)
              for d in layouts.values() for t in d.topics for p in t.units]
    for operation in plan.operations:
        for intent in _new_intents(operation.payload):
            p = intent.placement
            require((p.document_id, p.topic_id, p.position, intent.unit.knowledge_id) in placed, "NEW_PLACEMENT")
    require({p[3] for p in placed} <= {u.knowledge_id for u in base_state.units} | new_ids, "LAYOUT_REFERENCE")
    return plan


def apply_integration_plan(plan, *, base, source_reference, source_item_ids, base_reference):
    """The caller obtains semantic PASS first. This function makes no semantic judgment."""
    validate_integration_plan(plan, base=base, source_reference=source_reference,
        source_item_ids=source_item_ids, base_reference=base_reference)
    units = {u.knowledge_id: u for u in validate_gold_snapshot(base).units}
    for operation in plan.operations:
        for intent in _new_intents(operation.payload):
            units[intent.unit.knowledge_id] = intent.unit
    # Complete replacements are simultaneous against X, not sequential semantic patches.
    for operation in plan.operations:
        p = operation.payload
        if type(p) in (GoldEnrichPayload, GoldRewritePayload):
            units[p.target_knowledge_id] = p.resulting_unit
        elif type(p) is GoldMergePayload:
            survivor = p.survivor_unit.knowledge_id
            units[survivor] = p.survivor_unit
            for identity in p.target_knowledge_ids:
                if identity != survivor:
                    units[identity] = replace(units[identity], status=GoldStatus.MERGED, merged_into=survivor)
    for operation in plan.operations:
        p = operation.payload
        if type(p) in (GoldNoChangePayload, GoldFlagConflictPayload):
            for addition in p.provenance_additions:
                old = units[addition.knowledge_id]
                # Reject support to a simultaneously retired target instead of improvising a survivor.
                require(old.status is GoldStatus.ACTIVE, "RETIRED_SUPPORT_TARGET")
                units[old.knowledge_id] = replace(old, provenance=tuple(set(old.provenance) | {addition.provenance}))
        edges = ((p.relation,) if type(p) is GoldLinkPayload
                 else p.contradictions if type(p) is GoldFlagConflictPayload else ())
        for edge in edges:
            old = units[edge.source_knowledge_id]
            units[old.knowledge_id] = replace(old, relations=tuple(set(old.relations) | {edge.relation}))
    state = GoldContractState(1, 1, tuple(units.values()))
    validate_gold_state(state)
    layouts = {d.document_id: document_layout(d) for d in base.documents}
    layouts.update({d.document_id: d for d in plan.proposed_layout})
    # Provider never selects document versions. Compare material state after pruning.
    old_docs = {d.document_id: d for d in base.documents}
    layouts = {identity: replace(layout, version=replace(layout.version,
        document_version=old_docs[identity].document_version if identity in old_docs else 1))
        for identity, layout in layouts.items()}
    candidate = normalize_gold_structure(state, tuple(layouts.values()))
    documents = []
    for document in candidate.documents:
        old = old_docs.get(document.document_id)
        version = 1
        if old is not None:
            before, after = gold_document_metadata(old), gold_document_metadata(document)
            before.pop("document_version")
            after.pop("document_version")
            version = old.document_version + (before != after)
        documents.append(replace(document, version=replace(document.version, document_version=version)))
    candidate = GoldSnapshot(tuple(documents))
    validate_document_transition(base, candidate)
    for document in candidate.documents:
        require(decode_gold_document_pair(render_gold_document(document), encode_gold_document_metadata(document))
                == document, "CANDIDATE_PAIR")
    return candidate


@dataclass(frozen=True, slots=True)
class UnitEffect:
    knowledge_id: str
    before: GoldKnowledgeUnit | None = field(repr=False)
    after: GoldKnowledgeUnit = field(repr=False)


@dataclass(frozen=True, slots=True)
class DocumentEffect:
    document_id: str
    previous_version: int | None
    proposed_version: int | None
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True, slots=True)
class IntegrationEffects:
    units: tuple[UnitEffect, ...]
    documents: tuple[DocumentEffect, ...]
    provenance_additions: tuple[GoldProvenanceAddition, ...]
    relation_additions: tuple[GoldRelationIntent, ...]
    merges: tuple[tuple[str, str, tuple[str, ...]], ...]
    conflicts: tuple[tuple[str, tuple[str, ...], str], ...] = field(repr=False)
    source_item_accounting: tuple[tuple[str, tuple[str, ...]], ...]


def integration_effects(base, candidate, plan, item_ids):
    before = {u.knowledge_id: u for u in validate_gold_snapshot(base).units}
    after = {u.knowledge_id: u for u in validate_gold_snapshot(candidate).units}
    require(set(before) <= set(after), "KNOWLEDGE_LOSS")
    changed = tuple(UnitEffect(i, before.get(i), after[i]) for i in sorted(after) if before.get(i) != after[i])
    provenance, relations = [], []
    for change in changed:
        old = change.before
        provenance.extend(GoldProvenanceAddition(change.knowledge_id, p) for p in change.after.provenance
                          if old is None or p not in old.provenance)
        relations.extend(GoldRelationIntent(change.knowledge_id, r) for r in change.after.relations
                         if old is None or r not in old.relations)
    old_docs, new_docs = ({d.document_id: d for d in s.documents} for s in (base, candidate))
    documents = tuple(DocumentEffect(i, old_docs[i].document_version if i in old_docs else None,
        new_docs[i].document_version if i in new_docs else None,
        logical_gold_hash(GoldSnapshot((old_docs[i],))) if i in old_docs else None,
        logical_gold_hash(GoldSnapshot((new_docs[i],))) if i in new_docs else None)
        for i in sorted(set(old_docs) | set(new_docs)) if old_docs.get(i) != new_docs.get(i))
    operations = () if plan is None else plan.operations
    return IntegrationEffects(changed, documents, tuple(provenance), tuple(relations),
        tuple((o.operation_id, o.payload.survivor_unit.knowledge_id,
               tuple(i for i in o.payload.target_knowledge_ids if i != o.payload.survivor_unit.knowledge_id))
              for o in operations if type(o.payload) is GoldMergePayload),
        tuple((o.operation_id, o.payload.claim_knowledge_ids, o.payload.conflict_reason)
              for o in operations if type(o.payload) is GoldFlagConflictPayload),
        tuple((i, tuple(o.operation_id for o in operations if any(p.source_item_id == i for p in o.source_refs)))
              for i in sorted(item_ids)))
