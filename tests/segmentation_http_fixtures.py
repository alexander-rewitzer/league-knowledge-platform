"""Synthetic semantic decisions and the existing no-network HTTP test boundary."""

from league_knowledge_platform import SectionKind, SectionSpan, SegmentationProposal, SemanticCallMetadata
from league_knowledge_platform.segmentation_prompts import SEGMENTATION_PROMPT_VERSION
from selection_http_fixtures import SelectionHTTPAdapter, envelope, requests
from test_silver import bronze_bytes, bronze_fixture, silver_fixture

METADATA = SemanticCallMetadata("fixture", "requested-model", "reported-model", SEGMENTATION_PROMPT_VERSION)


def source(*texts):
    return silver_fixture(bronze_fixture(*(texts or ("Ahri mechanics", "More mechanics", "Sponsor product"))))


def proposal(*spans):
    return SegmentationProposal(tuple(SectionSpan(first, last, SectionKind(kind)) for first, last, kind in spans), METADATA)


def document(*spans):
    return {"sections": [{"first_source_index": first, "last_source_index": last, "kind": kind}
                         for first, last, kind in spans]}


class FakeSegmentation:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def propose_sections(self, request):
        self.requests.append(request)
        if not self.script:
            raise AssertionError("Unexpected semantic invocation")
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response
