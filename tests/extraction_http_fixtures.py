"""Synthetic extraction inputs and providers; all HTTP is injected offline."""

from league_knowledge_platform import (
    EvidenceSpan, ExtractionItemDraft, ExtractionOutcome, ExtractionResponse, SemanticCallMetadata,
    extraction_section_from_artifacts,
)
from league_knowledge_platform.extraction_prompts import EXTRACTION_PROMPT_VERSION
from segmentation_http_fixtures import bronze_bytes, proposal, source
from selection_http_fixtures import SelectionHTTPAdapter, envelope, requests
from test_segmentation_artifacts import artifact

METADATA = SemanticCallMetadata("fixture", "requested-model", "reported-model", EXTRACTION_PROMPT_VERSION)


def input_bytes(*texts, spans=None):
    silver = source(*(texts or ("Usually trade only if the ability is unavailable.",
                               "Otherwise it can interrupt the approach.", "Sponsor product")))
    if spans is None:
        spans = proposal((0, 3, "CONTENT"), (6, 6, "ADVERTISEMENT"))
    segmented = artifact(silver, spans)
    return bronze_bytes(segmented), bronze_bytes(silver)


def section(*texts, **kwargs):
    segmented, silver = input_bytes(*texts, **kwargs)
    return extraction_section_from_artifacts(segmentation_content=segmented, silver_content=silver,
                                             section_id="section-000001")


def response(*statements, spans=((0, 3),)):
    items = tuple(ExtractionItemDraft(text, tuple(EvidenceSpan(*s) for s in spans)) for text in statements)
    return ExtractionResponse(ExtractionOutcome.ITEMS_PROPOSED if items else ExtractionOutcome.NO_KNOWLEDGE_PROPOSED,
                              items, METADATA)


def document(*statements, spans=((0, 3),)):
    return {"outcome": "ITEMS_PROPOSED" if statements else "NO_KNOWLEDGE_PROPOSED", "items": [
        {"statement": text, "spans": [{"first_source_index": first, "last_source_index": last}
                                      for first, last in spans]} for text in statements]}


class FakeExtraction:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def extract(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("Unexpected capability invocation")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry
