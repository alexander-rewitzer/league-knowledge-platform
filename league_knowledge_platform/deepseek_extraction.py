"""One HTTP invocation behind the extraction capability; no model-owned provenance."""

from dataclasses import asdict, dataclass
import json
import math
import os

from . import _deepseek_json as transport, defaults
from .extraction_models import (
    EvidenceSpan, ExtractionCapabilityError, ExtractionItemDraft, ExtractionOutcome,
    ExtractionRequest, ExtractionResponse,
)
from .extraction_prompts import (
    EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_REFINEMENT_PROMPT_VERSION, EXTRACTION_REFINEMENT_SYSTEM_PROMPT,
)
from .selection_models import SemanticCallMetadata


@dataclass(frozen=True, slots=True)
class DeepSeekExtractionConfig:
    model: str = defaults.EXTRACTION_MODEL
    connect_timeout_seconds: float = defaults.EXTRACTION_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.EXTRACTION_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.EXTRACTION_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip()
                or type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("Invalid extraction provider configuration")
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (self.connect_timeout_seconds, self.read_timeout_seconds))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Expected finite positive provider timeouts")


def _payload(request):
    # No identity, provenance, neighbouring sections, timing, title or ranking metadata.
    payload = {"language_code": request.language_code,
            "segments": [{"source_index": s.source_index, "text": s.text} for s in request.segments]}
    if request.refinement is not None:
        previous = request.refinement.previous_proposal
        payload["previous_proposal"] = {"outcome": previous.outcome.value, "items": [
            {"item_id": item.item_id, "statement": item.statement,
             "spans": [asdict(span) for evidence in item.evidence for span in evidence.spans]}
            for item in previous.items]}
        payload["feedback"] = [asdict(issue) for issue in request.refinement.feedback]
    return payload


def _decode(document, metadata):
    if type(document) is not dict or set(document) != {"outcome", "items"} or type(document["items"]) is not list:
        raise ValueError("Invalid extraction response")
    drafts = []
    for item in document["items"]:
        if type(item) is not dict or set(item) != {"statement", "spans"} or type(item["spans"]) is not list:
            raise ValueError("Invalid extraction item draft")
        spans = []
        for span in item["spans"]:
            if type(span) is not dict or set(span) != {"first_source_index", "last_source_index"}:
                raise ValueError("Invalid evidence span")
            spans.append(EvidenceSpan(**span))
        drafts.append(ExtractionItemDraft(item["statement"], tuple(spans)))
    return ExtractionResponse(ExtractionOutcome(document["outcome"]), tuple(drafts), metadata)


class DeepSeekExtractionAdapter:
    def __init__(self, *, config: DeepSeekExtractionConfig | None = None, api_key=None, http_adapter=None):
        if config is not None and not isinstance(config, DeepSeekExtractionConfig):
            raise ValueError("Expected extraction provider configuration")
        self.config, self._api_key, self._http_adapter = config, api_key, http_adapter
        self._session = None
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._session is not None:
            self._session.close()
        self._session, self._api_key, self._closed = None, None, True

    @staticmethod
    def _error(kind, retryable):
        return ExtractionCapabilityError(kind, retryable=retryable)

    def _ensure_client(self):
        if self._closed:
            raise self._error("CONFIGURATION", False)
        if self._session is not None:
            return
        try:
            import requests
            if self.config is None:
                self.config = DeepSeekExtractionConfig(model=os.environ.get("DEEPSEEK_MODEL", defaults.EXTRACTION_MODEL))
            self.config.__post_init__()
            key = self._api_key if self._api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
            if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n"):
                raise ValueError("Invalid runtime credential")
            key.encode("ascii")
        except (ImportError, ValueError, UnicodeError):
            raise self._error("CONFIGURATION", False) from None
        self._api_key, self._requests = key, requests
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", self._http_adapter if self._http_adapter is not None
                      else requests.adapters.HTTPAdapter(max_retries=0))
        self._session = session

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        if not isinstance(request, ExtractionRequest):
            raise ValueError("Expected complete extraction request")
        request.__post_init__()
        self._ensure_client()
        config = self.config
        prompt = EXTRACTION_SYSTEM_PROMPT if request.refinement is None else EXTRACTION_REFINEMENT_SYSTEM_PROMPT
        prompt_version = EXTRACTION_PROMPT_VERSION if request.refinement is None else EXTRACTION_REFINEMENT_PROMPT_VERSION
        body = {"model": config.model, "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(_payload(request), ensure_ascii=False)},
        ], "thinking": {"type": defaults.EXTRACTION_THINKING_MODE}, "temperature": defaults.EXTRACTION_TEMPERATURE,
                "response_format": {"type": "json_object"}, "max_tokens": request.output_limit, "stream": False}
        try:
            document, reported_model = transport.complete_json(
                self._session, defaults.SELECTION_API_BASE + "/chat/completions", body, self._api_key,
                (config.connect_timeout_seconds, config.read_timeout_seconds),
                read=lambda response: transport.read_bounded(response, config.max_response_bytes, self._error),
                on_http_failure=lambda status, content: transport.http_failure(status, content, self._error),
                error=self._error, length_kind="OUTPUT_TRUNCATED",
            )
            metadata = SemanticCallMetadata("deepseek", config.model, reported_model, prompt_version)
            return _decode(document, metadata)
        except ExtractionCapabilityError:
            raise
        except self._requests.exceptions.Timeout:
            raise self._error("TIMEOUT", True) from None
        except self._requests.exceptions.RequestException:
            raise self._error("NETWORK", True) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise self._error("INVALID_OUTPUT", True) from None
