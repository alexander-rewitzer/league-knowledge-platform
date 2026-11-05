"""Strict transient provider projection into the existing governance contracts."""

from dataclasses import asdict
import json

from .gold_governance import decode_gold_operation, gold_operation_document
from .gold_governance_models import GOVERNANCE_CONTRACT_VERSION, OPERATION_SCHEMA_VERSION
from .gold_document_models import GoldDocumentLayout, GoldTopic, GoldUnitPlacement, GoldEntity
from .gold_models import GoldDocumentVersion
from .gold_integration_models import (IntegrationPlan, IntegrationIssue, IntegrationGateEvaluation,
    IntegrationEvaluation, IntegrationGate, IntegrationVerdict, require)


def keys(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "FIELDS")
    return value


def array(value):
    require(type(value) is list, "ARRAY")
    return value


def plan_document(plan):
    return {"schema_version": plan.schema_version, "integration_contract_version": plan.integration_contract_version,
            "source_reference": asdict(plan.source_reference), "base_reference": asdict(plan.base_reference),
            "operations": [gold_operation_document(o) for o in plan.operations],
            "proposed_layout": [asdict(d) for d in plan.proposed_layout]}


def proposal_to_plan(document, request, metadata):
    """Source/base bindings and new identities come exclusively from application state.

    Only identity positions are resolved; literal content/rationale is never rewritten.
    The operation payload still uses the exact existing governance JSON contract.
    """
    keys(document, "operations layouts")
    inspection, source = request.inspection, request.source

    def identity(value, kind):
        require(isinstance(value, str), "IDENTITY")
        if value.startswith("new-"):
            return inspection.allocate(kind, value)
        return value

    def resolve(value):
        if type(value) is list:
            return [resolve(v) for v in value]
        if type(value) is not dict:
            return value
        result = {}
        for name, member in value.items():
            if name in ("knowledge_id", "target_knowledge_id", "source_knowledge_id", "merged_into") and member is not None:
                result[name] = identity(member, "unit")
            elif name in ("target_knowledge_ids", "claim_knowledge_ids"):
                result[name] = [identity(v, "unit") for v in array(member)]
            elif name == "document_id":
                result[name] = identity(member, "document")
            elif name == "topic_id":
                result[name] = identity(member, "topic")
            else:
                result[name] = resolve(member)
        return result

    # Collect new identities only from declarations, never arbitrary invented stable IDs.
    for operation in array(document["operations"]):
        keys(operation, "operation_id kind source_item_ids rationale payload")
        payload = operation["payload"]
        if operation["kind"] == "CREATE":
            require(payload["new_unit"]["unit"]["knowledge_id"].startswith("new-unit-"), "NEW_ALIAS")
            identity(payload["new_unit"]["unit"]["knowledge_id"], "unit")
        elif operation["kind"] == "FLAG_CONFLICT":
            for claim in array(payload["new_claims"]):
                require(claim["unit"]["knowledge_id"].startswith("new-unit-"), "NEW_ALIAS")
                identity(claim["unit"]["knowledge_id"], "unit")
    operations = []
    for draft in document["operations"]:
        contributors = array(draft["source_item_ids"])
        require(len(contributors) == len(set(contributors)) and set(contributors) <= source.item_ids, "SOURCE_ITEMS")
        value = {k: v for k, v in draft.items() if k != "source_item_ids"}
        value = resolve(value)
        value.update(operation_schema_version=OPERATION_SCHEMA_VERSION, governance_contract_version=GOVERNANCE_CONTRACT_VERSION,
            source_refs=[{"source_knowledge_artifact": asdict(source.reference), "source_item_id": i} for i in contributors])
        operations.append(decode_gold_operation(json.dumps(value, ensure_ascii=False).encode("utf-8")))
    layouts, existing = [], {d.document_id for d in inspection.snapshot.documents}
    for layout in array(document["layouts"]):
        keys(layout, "document_id title entity domain_key topics")
        doc_id = identity(layout["document_id"], "document")
        require(doc_id in existing or layout["document_id"].startswith("new-document-"), "NEW_ALIAS")
        topics = []
        for topic in array(layout["topics"]):
            keys(topic, "topic_id title position units")
            topic_id = identity(topic["topic_id"], "topic")
            old_topic_ids = {t.topic_id for d in inspection.snapshot.documents if d.document_id == doc_id for t in d.topics}
            require(topic_id in old_topic_ids or topic["topic_id"].startswith("new-topic-"), "NEW_ALIAS")
            placements = []
            for p in array(topic["units"]):
                keys(p, "knowledge_id position")
                placements.append(GoldUnitPlacement(identity(p["knowledge_id"], "unit"), p["position"]))
            topics.append(GoldTopic(topic_id, topic["title"], topic["position"], tuple(placements)))
        entity = keys(layout["entity"], "namespace entity_key display_name")
        layouts.append(GoldDocumentLayout(GoldDocumentVersion(doc_id, 1), layout["title"], GoldEntity(**entity),
                                           layout["domain_key"], tuple(topics)))
    return IntegrationPlan(1, 1, source.reference, inspection.reference, tuple(operations), tuple(layouts), metadata)


def evaluation_document(evaluation):
    return {"gates": {g.gate.value: {"verdict": g.verdict.value, "issues": [asdict(i) for i in g.issues]}
                      for g in evaluation.gates}}


def decode_integration_evaluation(document, metadata):
    keys(document, "gates")
    require(type(document["gates"]) is dict and set(document["gates"]) == {g.value for g in IntegrationGate}, "GATES")
    values = []
    for gate in IntegrationGate:
        value = keys(document["gates"][gate.value], "verdict issues")
        issues = []
        for issue in array(value["issues"]):
            keys(issue, "reason actionable_feedback affected_operation_ids affected_source_item_ids affected_gold_ids")
            issues.append(IntegrationIssue(issue["reason"], issue["actionable_feedback"],
                *(tuple(array(issue[n])) for n in ("affected_operation_ids", "affected_source_item_ids", "affected_gold_ids"))))
        values.append(IntegrationGateEvaluation(gate, IntegrationVerdict(value["verdict"]), tuple(issues)))
    return IntegrationEvaluation(tuple(values), metadata)
