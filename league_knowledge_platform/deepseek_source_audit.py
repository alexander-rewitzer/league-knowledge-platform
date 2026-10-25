"""Independent source-generation and validation adapters over existing JSON transport."""

from dataclasses import asdict, dataclass
import json
import math
import os

from . import _deepseek_json as transport, defaults
from .extraction import _validate_spans
from .selection_models import SemanticCallMetadata
from .silver import _keys
from .source_audit import validate_source_candidate, validate_source_evaluation
from .source_audit_models import (
    SourceAuditCapabilityError, SourceAuditDecision, SourceAuditRequest, SourceAuditResponse,
    SourceConflictDraft, SourceItemDraft, SourceValidatorRequest,
)
from .source_audit_prompts import (
    SOURCE_AUDIT_PROMPT_VERSION, SOURCE_AUDIT_SYSTEM_PROMPT,
    SOURCE_VALIDATOR_PROMPT_VERSION, SOURCE_VALIDATOR_SYSTEM_PROMPT,
)
from .source_knowledge import candidate_document, decode_source_evaluation, evaluation_document


@dataclass(frozen=True, slots=True)
class DeepSeekSourceAuditConfig:
    model: str = defaults.SOURCE_AUDIT_MODEL
    connect_timeout_seconds: float = defaults.SOURCE_AUDIT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.SOURCE_AUDIT_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.SOURCE_AUDIT_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip()
                or type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("Invalid source provider configuration")
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (self.connect_timeout_seconds, self.read_timeout_seconds))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Expected positive finite source provider timeouts")


def _semantic_candidate(candidate):
    return {k: v for k, v in candidate_document(candidate).items() if k != "audit_metadata"}


def _original_inputs(inputs):
    sections = {s.section_id: s for s in inputs.sections}
    result = []
    for item in inputs.items:
        supporting = []
        for evidence in item.knowledge.evidence:
            section = sections[evidence.section_id]
            _validate_spans(evidence.spans, section)
            positions = {s.source_index: p for p, s in enumerate(section.segments)}
            indices = set()
            for span in evidence.spans:
                indices.update(s.source_index for s in section.segments[positions[span.first_source_index]:positions[span.last_source_index]+1])
            supporting.append({"section_id": evidence.section_id, "spans": [asdict(s) for s in evidence.spans],
                "segments": [{"source_index": s.source_index, "text": s.text} for s in section.segments if s.source_index in indices]})
        result.append({"input_ref": item.token, **asdict(item.reference), "statement": item.knowledge.statement,
                       "evidence": supporting})
    return result


class _SourceJSONAdapter:
    """Only the two new roles share this small session/transport implementation."""

    def __init__(self, *, config=None, api_key=None, http_adapter=None):
        if config is not None and not isinstance(config, DeepSeekSourceAuditConfig):
            raise ValueError("Expected source provider configuration")
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
        return SourceAuditCapabilityError(kind, retryable=retryable)

    def _ensure_client(self):
        if self._closed:
            raise self._error("CONFIGURATION", False)
        if self._session is not None:
            return
        try:
            import requests
            if self.config is None:
                self.config = DeepSeekSourceAuditConfig(model=os.environ.get("DEEPSEEK_MODEL", defaults.SOURCE_AUDIT_MODEL))
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
        self._session.mount("https://", self._http_adapter if self._http_adapter is not None else requests.adapters.HTTPAdapter(max_retries=0))

    def _complete(self, payload, prompt, version, output_limit):
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("Invalid output allowance")
        self._ensure_client()
        config = self.config
        body = {"model": config.model, "messages": [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "thinking": {"type": "disabled"}, "temperature": 0,
                "response_format": {"type": "json_object"}, "max_tokens": output_limit, "stream": False}
        try:
            document, reported = transport.complete_json(self._session, defaults.SELECTION_API_BASE + "/chat/completions", body,
                self._api_key, (config.connect_timeout_seconds, config.read_timeout_seconds),
                read=lambda r: transport.read_bounded(r, config.max_response_bytes, self._error),
                on_http_failure=lambda status, content: transport.http_failure(status, content, self._error),
                error=self._error, length_kind="OUTPUT_TRUNCATED")
            return document, SemanticCallMetadata("deepseek", config.model, reported, version)
        except SourceAuditCapabilityError:
            raise
        except self._requests.exceptions.Timeout:
            raise self._error("TIMEOUT", True) from None
        except self._requests.exceptions.RequestException:
            raise self._error("NETWORK", True) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise self._error("INVALID_OUTPUT", True) from None


class DeepSeekSourceAuditAdapter(_SourceJSONAdapter):
    def propose(self, request: SourceAuditRequest) -> SourceAuditResponse:
        if not isinstance(request, SourceAuditRequest):
            raise ValueError("Expected source audit request")
        request.inputs.__post_init__()
        if not request.inputs.items:
            raise ValueError("Empty candidate is constructed by application, not provider")
        payload = {"inputs": _original_inputs(request.inputs)}
        if request.refinement is not None:
            previous = validate_source_candidate(request.refinement.previous_candidate, request.inputs)
            evaluation = validate_source_evaluation(request.refinement.evaluation, request.inputs, item_count=len(previous.items))
            if evaluation.decision is not SourceAuditDecision.REJECT:
                raise ValueError("Refinement requires REJECT")
            payload["refinement"] = {"previous_candidate": _semantic_candidate(previous),
                                     "validator_feedback": evaluation_document(evaluation)}
        document, metadata = self._complete(payload, SOURCE_AUDIT_SYSTEM_PROMPT, SOURCE_AUDIT_PROMPT_VERSION, request.output_limit)
        try:
            _keys(document, "items conflicts")
            if type(document["items"]) is not list or type(document["conflicts"]) is not list:
                raise ValueError("Expected complete candidate arrays")
            items, conflicts = [], []
            for draft in document["items"]:
                _keys(draft, "statement input_refs")
                if type(draft["input_refs"]) is not list:
                    raise ValueError("Expected contributor array")
                items.append(SourceItemDraft(draft["statement"], tuple(draft["input_refs"])))
            for conflict in document["conflicts"]:
                _keys(conflict, "item_positions reason")
                if type(conflict["item_positions"]) is not list:
                    raise ValueError("Expected conflict positions")
                conflicts.append(SourceConflictDraft(tuple(conflict["item_positions"]), conflict["reason"]))
            return SourceAuditResponse(tuple(items), tuple(conflicts), metadata)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            raise self._error("INVALID_OUTPUT", True) from None


class DeepSeekSourceValidatorAdapter(_SourceJSONAdapter):
    def evaluate(self, request: SourceValidatorRequest):
        if not isinstance(request, SourceValidatorRequest):
            raise ValueError("Expected complete source validation request")
        validate_source_candidate(request.candidate, request.inputs)
        payload = {"inputs": _original_inputs(request.inputs), "candidate": _semantic_candidate(request.candidate),
            "content_sections": [{"section_id": s.section_id, "language_code": s.language_code,
                "segments": [{"source_index": v.source_index, "text": v.text} for v in s.segments]} for s in request.inputs.sections]}
        document, metadata = self._complete(payload, SOURCE_VALIDATOR_SYSTEM_PROMPT, SOURCE_VALIDATOR_PROMPT_VERSION, request.output_limit)
        try:
            return decode_source_evaluation(document, metadata)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            raise self._error("INVALID_OUTPUT", True) from None
