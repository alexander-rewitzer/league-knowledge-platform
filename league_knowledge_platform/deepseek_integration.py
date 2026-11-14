"""Isolated Agent and Validator JSON adapters. No hidden HTTP/semantic retries."""

from dataclasses import asdict, dataclass, fields
import json
import math
import os

from . import _deepseek_json as transport, defaults
from .gold_governance import GOLD_GOVERNANCE_RULES
from . import gold_governance_models as governance
from .gold_inspection import _request_identity
from .gold_inspection_models import GoldInspectionNotFoundError, GoldInspectionRequestError
from .gold_integration_codec import (array, keys, plan_document, proposal_to_plan,
                                     evaluation_document, decode_integration_evaluation)
from .gold_integration_input import json_value, projected_gold_references
from .gold_integration_models import (IntegrationAgentRequest, IntegrationValidatorRequest,
                                      IntegrationOperationalError, require)
from .gold_integration_prompts import (
    INTEGRATION_AGENT_SYSTEM_PROMPT, INTEGRATION_AGENT_PROMPT_VERSION,
    INTEGRATION_VALIDATOR_SYSTEM_PROMPT, INTEGRATION_VALIDATOR_PROMPT_VERSION,
)
from .selection_models import SemanticCallMetadata


@dataclass(frozen=True, slots=True)
class DeepSeekIntegrationConfig:
    model: str = defaults.INTEGRATION_MODEL
    connect_timeout_seconds: float = defaults.INTEGRATION_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = defaults.INTEGRATION_READ_TIMEOUT_SECONDS
    max_response_bytes: int = defaults.INTEGRATION_MAX_RESPONSE_BYTES

    def __post_init__(self):
        require(isinstance(self.model, str) and bool(self.model.strip()), "CONFIGURATION")
        require(type(self.max_response_bytes) is int and self.max_response_bytes > 0, "CONFIGURATION")
        for value in (self.connect_timeout_seconds, self.read_timeout_seconds):
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            require(valid, "CONFIGURATION")


def _rules():
    return [asdict(rule) for rule in GOLD_GOVERNANCE_RULES]


_QUERY_ID_PREFIXES = {"document_id": "gdoc", "topic_id": "topic", "knowledge_id": "gku"}


def _contracts():
    names = ("GoldNoChangePayload", "GoldCreatePayload", "GoldEnrichPayload", "GoldRewritePayload",
             "GoldMergePayload", "GoldLinkPayload", "GoldFlagConflictPayload", "GoldNewUnitIntent",
             "GoldPlacementIntent", "GoldProvenanceAddition", "GoldRelationIntent")
    filters = {name: "null or routing key matching [a-z0-9][a-z0-9_-]{0,79}"
               for name in ("entity_namespace", "entity_key", "domain_key")}
    filters["document_id"] = "null or observed document_id"
    return {**{n: {f.name: str(f.type) for f in fields(getattr(governance, n))} for n in names},
            "GoldKnowledgeUnit": "knowledge_id,content,provenance[],status ACTIVE|MERGED,relations[],merged_into null|ID",
            "GoldProvenance": "source_knowledge_artifact {source_type,source_id,relative_path,sha256,schema_version,audit_contract_version},source_item_id",
            "GoldRelation": "kind related_to|contradicts,target_knowledge_id",
            "observed_id_formats": {name: prefix + "-[0-9a-f]{32}" for name, prefix in _QUERY_ID_PREFIXES.items()},
            "queries": {
                "search_gold": {"required": {"query": "string"}, "optional": {
                    **filters, "topic_id": "null or observed topic_id; with document_id use the observed pair",
                    "status": "null, ACTIVE or MERGED",
                    "limit": f"integer 1 through {defaults.GOLD_INSPECTION_MAX_PAGE_LIMIT}", "offset": "integer >= 0"}},
                "get_gold_unit": {"required": {"knowledge_id": "observed knowledge_id"}},
                "get_gold_document": {"required": {"document_id": "observed document_id"}},
                "get_gold_topic": {"required": {"document_id": "observed document_id",
                    "topic_id": "observed topic_id from the same document_id"}},
                "get_gold_provenance": {"required": {"knowledge_id": "observed knowledge_id"}},
                "inspect_gold_structure": {"required": {}, "optional": filters}}}


class _IntegrationJSONAdapter:
    def __init__(self, *, config=None, api_key=None, http_adapter=None):
        require(config is None or type(config) is DeepSeekIntegrationConfig, "CONFIGURATION")
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
    def _error(code, retryable=False):
        return IntegrationOperationalError(code, phase="PROVIDER")

    def _ensure_client(self):
        require(not self._closed, "CONFIGURATION")
        if self._session is not None:
            return
        try:
            import requests
            self.config = self.config or DeepSeekIntegrationConfig(model=os.environ.get("DEEPSEEK_MODEL", defaults.INTEGRATION_MODEL))
            self.config.__post_init__()
            key = self._api_key if self._api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
            require(isinstance(key, str) and bool(key.strip()) and not any(c in key for c in "\r\n"), "CONFIGURATION")
            key.encode("ascii")
        except (ImportError, ValueError, UnicodeError):
            raise self._error("CONFIGURATION") from None
        self._api_key, self._requests = key, requests
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.mount("https://", self._http_adapter if self._http_adapter is not None
                            else requests.adapters.HTTPAdapter(max_retries=0))

    def _complete(self, payload, prompt, version, limit):
        self._ensure_client()
        require(type(limit) is int and limit > 0, "OUTPUT_LIMIT")
        encoded = json_value(payload)
        body = {"model": self.config.model, "messages": [{"role": "system", "content": prompt},
                {"role": "user", "content": encoded}], "temperature": 0, "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"}, "max_tokens": limit, "stream": False}
        try:
            value, reported = transport.complete_json(self._session, defaults.SELECTION_API_BASE + "/chat/completions",
                body, self._api_key, (self.config.connect_timeout_seconds, self.config.read_timeout_seconds),
                read=lambda r: transport.read_bounded(r, self.config.max_response_bytes, self._error),
                on_http_failure=lambda status, content: transport.http_failure(status, content, self._error),
                error=self._error, length_kind="OUTPUT_TRUNCATED")
            return value, SemanticCallMetadata("deepseek", self.config.model, reported, version)
        except IntegrationOperationalError:
            raise
        except self._requests.exceptions.Timeout:
            raise self._error("TIMEOUT") from None
        except self._requests.exceptions.RequestException:
            raise self._error("NETWORK") from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            raise self._error("INVALID_OUTPUT") from None


class DeepSeekIntegrationAgent(_IntegrationJSONAdapter):
    def _inspect(self, inspection, method, arguments, query_contracts, observed_ids, observed_topics):
        require(isinstance(method, str) and method in query_contracts, "INSPECTION_METHOD")
        required = set(query_contracts[method]["required"])
        optional = set(query_contracts[method].get("optional", ()))
        if not required <= arguments.keys() <= required | optional:
            raise self._error("INSPECTION_INVALID_ARGUMENTS")
        try:
            for name, identity in arguments.items():
                if name not in _QUERY_ID_PREFIXES or (identity is None and name in optional):
                    continue
                _request_identity(identity, _QUERY_ID_PREFIXES[name])
                if identity not in observed_ids:
                    raise self._error("INSPECTION_UNKNOWN_ID")
            document_id, topic_id = arguments.get("document_id"), arguments.get("topic_id")
            if document_id is not None and topic_id is not None and (document_id, topic_id) not in observed_topics:
                raise self._error("INSPECTION_UNKNOWN_ID")
            inspection.query(method, **arguments)
        except GoldInspectionRequestError as error:
            code = "INSPECTION_INVALID_IDENTITY" if error.code == "IDENTITY" else "INSPECTION_INVALID_ARGUMENTS"
            raise self._error(code) from None
        except GoldInspectionNotFoundError:
            raise self._error("INSPECTION_UNKNOWN_ID") from None

    def propose(self, request: IntegrationAgentRequest):
        require(type(request) is IntegrationAgentRequest and bool(request.source.item_ids), "AGENT_REQUEST")
        require((request.previous_plan is None) == (request.feedback is None), "REFINEMENT")
        if request.feedback is not None:
            require(request.feedback.decision == "REJECT", "REFINEMENT")
        contracts = _contracts()
        for _ in range(request.config.max_agent_exchanges):
            observations = [json.loads(s) for s in request.inspection.observations]
            payload = {"source": request.source.semantic_document(), "base_reference": asdict(request.inspection.reference),
                       "governance": _rules(), "contracts": contracts,
                       "gold_has_documents": bool(request.inspection.snapshot.documents),
                       "application_declarations": request.inspection.declarations,
                       "inspected_gold": observations}
            if request.previous_plan is not None:
                payload["refinement"] = {"previous_complete_plan": plan_document(request.previous_plan),
                                         "feedback": evaluation_document(request.feedback)}
            document, metadata = self._complete(payload, INTEGRATION_AGENT_SYSTEM_PROMPT,
                INTEGRATION_AGENT_PROMPT_VERSION, request.config.agent_output_limit)
            try:
                observed_ids, observed_topics, observed_provenance = projected_gold_references(
                    *(observation["result"] for observation in observations))
                if type(document) is dict and set(document) == {"proposal"}:
                    return proposal_to_plan(document["proposal"], request, metadata,
                                            observed_ids, observed_topics, observed_provenance)
                keys(document, "queries")
                queries = array(document["queries"])
                require(bool(queries), "INSPECTION_QUERY")
                for query in queries:
                    keys(query, "method arguments")
                    require(type(query["arguments"]) is dict, "INSPECTION_QUERY")
                    self._inspect(request.inspection, query["method"], query["arguments"], contracts["queries"],
                                  observed_ids, observed_topics)
            except IntegrationOperationalError:
                raise
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                raise self._error("INVALID_OUTPUT") from None
        raise self._error("INSPECTION_EXCHANGES_EXHAUSTED")


class DeepSeekIntegrationValidator(_IntegrationJSONAdapter):
    def evaluate(self, request: IntegrationValidatorRequest):
        require(type(request) is IntegrationValidatorRequest, "VALIDATOR_REQUEST")
        payload = {"source": request.source.semantic_document(), "complete_plan": plan_document(request.plan),
                   "inspected_gold": [json.loads(s) for s in request.inspected_context], "governance": _rules()}
        document, metadata = self._complete(payload, INTEGRATION_VALIDATOR_SYSTEM_PROMPT,
            INTEGRATION_VALIDATOR_PROMPT_VERSION, request.output_limit)
        try:
            evaluation = decode_integration_evaluation(document, metadata)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise self._error("INVALID_OUTPUT") from None
        supplied_ids, _, _ = projected_gold_references(payload["complete_plan"],
            *(observation["result"] for observation in payload["inspected_gold"]))
        if any(not supplied_ids.issuperset(issue.affected_gold_ids)
               for gate in evaluation.gates for issue in gate.issues):
            raise self._error("FEEDBACK_REFERENCE")
        return evaluation
