"""Synthetic documents; no provider output or private source material."""

from league_knowledge_platform import (
    GoldDocument, GoldDocumentLayout, GoldDocumentVersion, GoldEntity,
    GoldTopic, GoldUnitPlacement,
)
from test_gold_models import identity, unit


def topic(number=1, ids=(1, 2), *, position=0, title="Reset Timing"):
    return GoldTopic(f"topic-{number:032x}", title, position,
                     tuple(GoldUnitPlacement(identity(n), i) for i, n in enumerate(ids)))


def layout(number=1, *, topics=None, version=1, domain="laning"):
    return GoldDocumentLayout(GoldDocumentVersion(f"gdoc-{number:032x}", version), "Ahri — Laning",
        GoldEntity("champions", "ahri", "Ahri"), domain, (topic(),) if topics is None else topics)


def document(number=1, *, topics=None, units=None, historical=(), version=1, domain="laning"):
    l = layout(number, topics=topics, version=version, domain=domain)
    return GoldDocument(l.version, l.title, l.entity, l.domain_key, l.topics,
        (unit(1, content="Prepare the wave before taking a reset."),
         unit(2, content="Evaluate the next wave before committing to the recall.")) if units is None else units,
        historical)
