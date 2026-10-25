"""Single-request whole-section Judge; strict five-gate output without overall override."""

from dataclasses import asdict, dataclass
import json
import math
import os

from . import _deepseek_json as transport, defaults
from .section_validation_models import (
    SectionJudgeCapabilityError, SectionJudgeEvaluation, SectionJudgeRequest, decode_judge_document,
)
from .section_validation_prompts import SECTION_JUDGE_PROMPT_VERSION, SECTION_JUDGE_SYSTEM_PROMPT
from .selection_models import SemanticCallMetadata


@dataclass(frozen=True, slots=True)
class DeepSeekSectionJudgeConfig:
    model: str = defaults.SECTION_JUDGE_MODEL
    connect_timeout_seconds: float = defaults.SECTION_JUDGE_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.SECTION_JUDGE_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.SECTION_JUDGE_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip()
                or type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("Invalid Judge configuration")
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (self.connect_timeout_seconds, self.read_timeout_seconds))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Expected positive finite timeouts")


def _payload(request):
    return {"language_code": request.section.language_code,
            "segments": [{"source_index": s.source_index, "text": s.text} for s in request.section.segments],
            "proposal": {"outcome": request.proposal.outcome.value, "items": [
                {"item_id": item.item_id, "statement": item.statement,
                 "spans": [asdict(span) for evidence in item.evidence for span in evidence.spans]}
                for item in request.proposal.items]}}


class DeepSeekSectionJudgeAdapter:
    def __init__(self, *, config: DeepSeekSectionJudgeConfig | None = None, api_key=None, http_adapter=None):
        if config is not None and not isinstance(config, DeepSeekSectionJudgeConfig):
            raise ValueError("Expected Judge provider configuration")
        self.config, self._api_key, self._http_adapter = config, api_key, http_adapter
        self._session, self._closed = None, False

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
        return SectionJudgeCapabilityError(kind, retryable=retryable)

    def _ensure_client(self):
        if self._closed:
            raise self._error("CONFIGURATION", False)
        if self._session is not None:
            return
        try:
            import requests
            if self.config is None:
                self.config = DeepSeekSectionJudgeConfig(model=os.environ.get("DEEPSEEK_MODEL", defaults.SECTION_JUDGE_MODEL))
            self.config.__post_init__()
            key = self._api_key if self._api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
            if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n"):
                raise ValueError("Invalid runtime credential")
            key.encode("ascii")
        except (ImportError, ValueError, UnicodeError):
            raise self._error("CONFIGURATION", False) from None
        self._api_key, self._requests = key, requests
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.mount("https://", self._http_adapter if self._http_adapter is not None
                            else requests.adapters.HTTPAdapter(max_retries=0))

    def evaluate(self, request: SectionJudgeRequest) -> SectionJudgeEvaluation:
        if not isinstance(request, SectionJudgeRequest):
            raise ValueError("Expected complete section Judge request")
        request.__post_init__()
        self._ensure_client()
        config = self.config
        body = {"model": config.model, "messages": [
            {"role": "system", "content": SECTION_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_payload(request), ensure_ascii=False)},
        ], "thinking": {"type": "disabled"}, "temperature": 0, "response_format": {"type": "json_object"},
                "max_tokens": request.output_limit, "stream": False}
        try:
            document, reported = transport.complete_json(
                self._session, defaults.SELECTION_API_BASE + "/chat/completions", body, self._api_key,
                (config.connect_timeout_seconds, config.read_timeout_seconds),
                read=lambda r: transport.read_bounded(r, config.max_response_bytes, self._error),
                on_http_failure=lambda status, content: transport.http_failure(status, content, self._error),
                error=self._error, length_kind="OUTPUT_TRUNCATED")
            return decode_judge_document(document, SemanticCallMetadata("deepseek", config.model, reported, SECTION_JUDGE_PROMPT_VERSION))
        except SectionJudgeCapabilityError:
            raise
        except self._requests.exceptions.Timeout:
            raise self._error("TIMEOUT", True) from None
        except self._requests.exceptions.RequestException:
            raise self._error("NETWORK", True) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise self._error("INVALID_OUTPUT", True) from None
