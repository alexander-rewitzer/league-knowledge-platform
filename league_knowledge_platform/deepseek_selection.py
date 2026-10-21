"""DeepSeek JSON adapter; no SDK objects, automatic retries, or durable output."""

from dataclasses import dataclass
import json
import math
import os

from . import defaults
from . import _deepseek_json as transport
from .selection_models import (
    CandidateQualityAssessment, ComparativeCandidateAssessment, ComparativeSelection,
    ComparativeSelectionRequest, QualityAssessmentBatch, QualityAssessmentRequest,
    QualityReasonCode, SelectionFailureKind, SemanticCallMetadata,
)
from .selection_prompts import (
    COMPARISON_FIELDS, COMPARISON_PROMPT_VERSION, COMPARISON_SYSTEM_PROMPT,
    QUALITY_FIELDS, QUALITY_PROMPT_VERSION, QUALITY_SYSTEM_PROMPT,
)
from .source_selection import SelectionCapabilityError


@dataclass(frozen=True, slots=True)
class DeepSeekSelectionConfig:
    model: str = defaults.SELECTION_MODEL
    quality_output_limit: int = defaults.SELECTION_QUALITY_OUTPUT_LIMIT
    comparison_output_limit: int = defaults.SELECTION_COMPARISON_OUTPUT_LIMIT
    connect_timeout_seconds: float = defaults.SELECTION_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.SELECTION_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.SELECTION_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Selection requires a configured model")
        if any(type(v) is not int or v <= 0 for v in (
            self.quality_output_limit, self.comparison_output_limit, self.max_response_bytes,
        )):
            raise ValueError("Selection output bounds must be positive integers")
        try:
            valid_timeouts = all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (
                self.connect_timeout_seconds, self.read_timeout_seconds,
            ))
        except OverflowError:
            valid_timeouts = False
        if not valid_timeouts:
            raise ValueError("Selection timeouts must be finite and positive")


def _invalid():
    return SelectionCapabilityError(SelectionFailureKind.INVALID_OUTPUT, retryable=True)


_json = transport.strict_json


def _keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= set(value)
            or not set(value) <= set(required) | set(optional)):
        raise ValueError("Structured artifact keys do not match its schema")


def _assessment_items(document, keys):
    _keys(document, keys)
    items = document["assessments"]
    if not isinstance(items, list) or not 1 <= len(items) <= defaults.RANKING_TOP_K:
        raise ValueError("Invalid assessment array")
    return items


def _payload(request):
    # Only these strings cross the boundary. No asdict(), vars(), object reference,
    # source title, track metadata, previous judgment, or previous message history.
    return {
        "target": {"id": request.target_id, "name": request.target_name},
        "candidates": [{"video_id": c.video_id, "transcript_text": c.transcript_text}
                       for c in request.candidates],
    }


class DeepSeekSelectionAdapter:
    """Lazy optional HTTP dependency and secrets; zero-input selection needs neither.

    HTTP connect/read timeouts are not a total wall-clock deadline. Provider
    keep-alive whitespace is accepted by the JSON parser; response bytes are bounded.
    Inject a requests HTTPAdapter for completely offline transport tests.
    """

    def __init__(self, *, config: DeepSeekSelectionConfig | None = None,
                 api_key: str | None = None, http_adapter=None):
        if config is not None and not isinstance(config, DeepSeekSelectionConfig):
            raise ValueError("Expected immutable DeepSeek selection configuration")
        self.config = config
        self._api_key = api_key
        self._http_adapter = http_adapter
        self._session = None
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._session is not None:
            self._session.close()
        self._session = None
        self._api_key = None
        self._closed = True

    def _ensure_client(self):
        if self._closed:
            raise SelectionCapabilityError(SelectionFailureKind.CONFIGURATION, retryable=False)
        if self._session is not None:
            return
        try:
            import requests
            if self.config is None:
                self.config = DeepSeekSelectionConfig(
                    model=os.environ.get("DEEPSEEK_MODEL", defaults.SELECTION_MODEL),
                )
            key = self._api_key if self._api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
            if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n"):
                raise ValueError("Invalid runtime credential")
            key.encode("ascii")
        except (ImportError, ValueError, UnicodeError):
            raise SelectionCapabilityError(SelectionFailureKind.CONFIGURATION,
                                           retryable=False) from None
        self._api_key = key
        self._requests = requests
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", self._http_adapter if self._http_adapter is not None
                      else requests.adapters.HTTPAdapter(max_retries=0))
        self._session = session

    @staticmethod
    def _transport_error(kind, retryable):
        return SelectionCapabilityError(SelectionFailureKind(kind), retryable=retryable)

    def _read(self, response):
        return transport.read_bounded(response, self.config.max_response_bytes, self._transport_error)

    def _http_failure(self, status, content):
        transport.http_failure(status, content, self._transport_error)

    def _call(self, request, system_prompt, version, output_limit):
        self._ensure_client()
        config = self.config
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(_payload(request), ensure_ascii=False)},
            ],
            "thinking": {"type": defaults.SELECTION_THINKING_MODE},
            "temperature": defaults.SELECTION_TEMPERATURE,
            "response_format": {"type": "json_object"},
            "max_tokens": output_limit,
            "stream": False,
        }
        try:
            artifact, reported_model = transport.complete_json(
                self._session, defaults.SELECTION_API_BASE + "/chat/completions", body,
                self._api_key, (config.connect_timeout_seconds, config.read_timeout_seconds),
                read=self._read, on_http_failure=self._http_failure, error=self._transport_error,
            )
            metadata = SemanticCallMetadata("deepseek", config.model, reported_model, version)
            return artifact, metadata
        except SelectionCapabilityError:
            raise
        except self._requests.exceptions.Timeout:
            raise SelectionCapabilityError(SelectionFailureKind.TIMEOUT, retryable=True) from None
        except self._requests.exceptions.RequestException:
            raise SelectionCapabilityError(SelectionFailureKind.NETWORK, retryable=True) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise _invalid() from None

    def assess_quality(self, request: QualityAssessmentRequest) -> QualityAssessmentBatch:
        if not isinstance(request, QualityAssessmentRequest):
            raise ValueError("Quality assessment requires its isolated input contract")
        # Resolve configurable output bounds lazily along with the model and key.
        self._ensure_client()
        document, metadata = self._call(request, QUALITY_SYSTEM_PROMPT, QUALITY_PROMPT_VERSION,
                                         self.config.quality_output_limit)
        try:
            items = _assessment_items(document, ("assessments",))
            assessments = []
            for item in items:
                _keys(item, QUALITY_FIELDS, ("reason_codes",))
                codes = item.get("reason_codes", [])
                if not isinstance(codes, list):
                    raise ValueError("Reason codes must be an array")
                assessments.append(CandidateQualityAssessment(
                    *(item[name] for name in QUALITY_FIELDS),
                    tuple(QualityReasonCode(code) for code in codes),
                ))
            result = QualityAssessmentBatch(tuple(assessments), metadata)
            if {a.video_id for a in result.assessments} != {c.video_id for c in request.candidates}:
                raise ValueError("Assessment does not cover the complete request")
            return result
        except (ValueError, TypeError, KeyError):
            raise _invalid() from None

    def compare_sources(self, request: ComparativeSelectionRequest) -> ComparativeSelection:
        if not isinstance(request, ComparativeSelectionRequest):
            raise ValueError("Comparison requires its isolated input contract")
        self._ensure_client()
        document, metadata = self._call(request, COMPARISON_SYSTEM_PROMPT, COMPARISON_PROMPT_VERSION,
                                         self.config.comparison_output_limit)
        try:
            items = _assessment_items(document, ("selected_video_id", "assessments", "selection_reason"))
            assessments = []
            for item in items:
                _keys(item, COMPARISON_FIELDS)
                assessments.append(ComparativeCandidateAssessment(*(item[name] for name in COMPARISON_FIELDS)))
            result = ComparativeSelection(document["selected_video_id"], tuple(assessments),
                                          document["selection_reason"], metadata)
            if {a.video_id for a in result.assessments} != {c.video_id for c in request.candidates}:
                raise ValueError("Comparison must cover only its supplied candidates")
            return result
        except (ValueError, TypeError, KeyError):
            raise _invalid() from None
