"""Synthetic source/Gold and scripted semantic roles; no live provider fallback."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

from league_knowledge_platform import (
    IntegrationPlan, IntegrationEvaluation, IntegrationGate, IntegrationGateEvaluation,
    IntegrationVerdict, IntegrationIssue, SemanticCallMetadata, GoldSnapshot,
    GoldSnapshotReference, GoldOperationKind, GoldCreatePayload, GoldNewUnitIntent,
    GoldProvenance, GoldUnitPlacement, SourceKnowledgeOutcome,
)
from league_knowledge_platform.gold_integration_input import IntegrationSource
from gold_document_fixtures import document, layout, topic
from test_gold_governance_models import SUPPORT, examples, operation, placement
from test_gold_models import reference, unit

META = SemanticCallMetadata('fixture', 'model', 'reported', 'fixture-v1')
BASE_REF = GoldSnapshotReference('a' * 32, 'b' * 64)
BASE = GoldSnapshot((document(units=(unit(), unit(2))),))


@dataclass(frozen=True)
class Item:
    item_id: str
    statement: str
    evidence: tuple = ()
    derived_from: tuple = ()


def source(*ids):
    ids = ids or ('source-item-000001',)
    return IntegrationSource(reference('B'), SimpleNamespace(outcome=SourceKnowledgeOutcome.KNOWLEDGE_AVAILABLE,
        candidate=SimpleNamespace(items=tuple(Item(i, 'Preserve conditions about reset timing.') for i in ids), conflicts=())))


def plan(index=0, *, base_reference=BASE_REF, source_reference=SUPPORT.source_knowledge_artifact,
         operations=None, layouts=None):
    if operations is None:
        operations = (examples()[index],)
    if layouts is None:
        layouts = (layout(topics=(topic(ids=(1, 2, 3)),)),) if index in (1, 6) else ()
    return IntegrationPlan(1, 1, source_reference, base_reference, operations, layouts, META)


def evaluation(fail=None, **issue_changes):
    values = []
    for gate in IntegrationGate:
        issues = ()
        if gate.value == fail:
            issue = IntegrationIssue('Missing condition.', 'Restore the stated exception.', ('op-1',),
                                     ('source-item-000001',), ())
            issues = (replace(issue, **issue_changes),)
        values.append(IntegrationGateEvaluation(gate, IntegrationVerdict.FAIL if issues else IntegrationVerdict.PASS, issues))
    return IntegrationEvaluation(tuple(values), META)


class FakeAgent:
    def __init__(self, script=None):
        self.script = list(script or [0])
        self.calls = []

    def propose(self, request):
        self.calls.append(request)
        value = self.script.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(request)
        return plan(value, source_reference=request.source.reference, base_reference=request.inspection.reference)


class FakeValidator:
    def __init__(self, script=None):
        self.script = list(script or [evaluation()])
        self.calls = []

    def evaluate(self, request):
        self.calls.append(request)
        value = self.script.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def real_create(request):
    support = tuple(GoldProvenance(request.source.reference, i) for i in sorted(request.source.item_ids))
    new = GoldNewUnitIntent(unit(3, provenance=support), placement(0))
    op = operation(GoldCreatePayload(new), GoldOperationKind.CREATE, refs=support)
    return plan(operations=(op,), layouts=(layout(topics=(topic(ids=(3,)),)),),
                source_reference=request.source.reference, base_reference=request.inspection.reference)
