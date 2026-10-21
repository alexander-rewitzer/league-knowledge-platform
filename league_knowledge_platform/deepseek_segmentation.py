"""DeepSeek boundary-only adapter; one injected HTTP invocation, no retries or storage."""

from dataclasses import asdict, dataclass
import json
import math
import os

from . import _deepseek_json as transport, defaults
from .segmentation_models import (
    SectionKind, SectionSpan, SegmentationCapabilityError, SegmentationProposal, SegmentationRequest,
)
from .segmentation_prompts import SEGMENTATION_PROMPT_VERSION, SEGMENTATION_SYSTEM_PROMPT, SPAN_FIELDS
from .selection_models import SemanticCallMetadata


@dataclass(frozen=True, slots=True)
class DeepSeekSegmentationConfig:
    model: str = defaults.SEGMENTATION_MODEL
    connect_timeout_seconds: float = defaults.SEGMENTATION_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.SEGMENTATION_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.SEGMENTATION_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip()
                or type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("Invalid segmentation provider configuration")
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (self.connect_timeout_seconds, self.read_timeout_seconds))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Expected finite positive provider timeouts")


def _payload(request):
    # Explicit allowlist: no source identity, title, rank, target/run, timing or selection assessments.
    return {"language_code": request.language_code,
            "segments": [{"source_index": s.source_index, "text": s.text} for s in request.segments],
            "validation_feedback": [asdict(issue) for issue in request.validation_feedback]}


class DeepSeekSegmentationAdapter:
    def __init__(self, *, config: DeepSeekSegmentationConfig | None = None, api_key=None, http_adapter=None):
        if config is not None and not isinstance(config, DeepSeekSegmentationConfig):
            raise ValueError("Expected immutable segmentation provider configuration")
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
        return SegmentationCapabilityError(kind, retryable=retryable)

    def _ensure_client(self):
        if self._closed:
            raise self._error("CONFIGURATION", False)
        if self._session is not None:
            return
        try:
            import requests
            if self.config is None:
                self.config = DeepSeekSegmentationConfig(model=os.environ.get("DEEPSEEK_MODEL", defaults.SEGMENTATION_MODEL))
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

    def propose_sections(self, request: SegmentationRequest) -> SegmentationProposal:
        if not isinstance(request, SegmentationRequest):
            raise ValueError("Expected complete segmentation request")
        request.__post_init__()
        self._ensure_client()
        config = self.config
        body = {"model": config.model, "messages": [
            {"role": "system", "content": SEGMENTATION_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_payload(request), ensure_ascii=False)},
        ], "thinking": {"type": "disabled"}, "temperature": 0,
                "response_format": {"type": "json_object"}, "max_tokens": request.output_limit, "stream": False}
        metadata = None
        try:
            document, reported_model = transport.complete_json(
                self._session, defaults.SELECTION_API_BASE + "/chat/completions", body,
                self._api_key, (config.connect_timeout_seconds, config.read_timeout_seconds),
                read=lambda response: transport.read_bounded(response, config.max_response_bytes, self._error),
                on_http_failure=lambda status, content: transport.http_failure(status, content, self._error),
                error=self._error, length_kind="OUTPUT_TRUNCATED",
            )
            metadata = SemanticCallMetadata("deepseek", config.model, reported_model, SEGMENTATION_PROMPT_VERSION)
            if type(document) is not dict or set(document) != {"sections"} or type(document["sections"]) is not list:
                raise ValueError("Invalid proposal schema")
            sections = []
            for item in document["sections"]:
                if type(item) is not dict or set(item) != SPAN_FIELDS:
                    raise ValueError("Invalid section schema")
                sections.append(SectionSpan(item["first_source_index"], item["last_source_index"], SectionKind(item["kind"])))
            return SegmentationProposal(tuple(sections), metadata)
        except SegmentationCapabilityError:
            raise
        except self._requests.exceptions.Timeout:
            raise self._error("TIMEOUT", True) from None
        except self._requests.exceptions.RequestException:
            raise self._error("NETWORK", True) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise SegmentationCapabilityError("INVALID_OUTPUT", retryable=True, metadata=metadata) from None
