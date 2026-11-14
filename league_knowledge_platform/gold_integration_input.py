"""Authoritative source admission and bounded observations of one pinned Gold session."""

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json

from .gold_inspection import open_gold_inspection
from .gold_inspection_models import GoldInspectionRequestError
from .gold_models import GoldSourceArtifactReference, GoldStatus
from .source_knowledge import canonical_source_knowledge_path, load_source_knowledge
from .source_audit_input import read_source_file
from .source_audit_models import DEFAULT_SOURCE_AUDIT_CONFIG
from .gold_integration_models import IntegrationOperationalError, require


def projected_gold_references(*projections):
    """Collect IDs and provenance from typed Gold fields, ignoring free text."""
    identities, topics, provenance = set(), set(), set()

    def visit(value, document_id=None):
        if type(value) in (list, tuple):
            for member in value:
                visit(member, document_id)
        elif type(value) is dict:
            document_id = value.get("document_id", document_id)
            if "version" in value and "topics" in value:
                # Full GoldDocument stores its identity in the sibling version object.
                document_id = value["version"]["document_id"]
            if document_id is not None and "topic_id" in value:
                topics.add((document_id, value["topic_id"]))
            if "source_knowledge_artifact" in value:
                provenance.add(json_value(value))
            for name, member in value.items():
                if name in ("knowledge_id", "target_knowledge_id", "source_knowledge_id", "merged_into", "document_id", "topic_id"):
                    if member is not None:
                        identities.add(member)
                elif name in ("knowledge_ids", "historical_knowledge_ids", "target_knowledge_ids", "claim_knowledge_ids"):
                    identities.update(member)
                else:
                    visit(member, document_id)

    for projection in projections:
        visit(projection)
    return frozenset(identities), frozenset(topics), frozenset(provenance)


def json_value(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class IntegrationSource:
    reference: GoldSourceArtifactReference
    artifact: object = field(repr=False)

    @property
    def item_ids(self):
        return frozenset(i.item_id for i in self.artifact.candidate.items)

    def semantic_document(self):
        return {"reference": asdict(self.reference),
                "outcome": self.artifact.outcome.value,
                "items": [asdict(i) for i in self.artifact.candidate.items],
                "conflicts": [asdict(c) for c in self.artifact.candidate.conflicts]}


def load_integration_source(video_id, **source_configs):
    """Load/validate, never prepare/repair any upstream artifact."""
    config = source_configs.get("config", DEFAULT_SOURCE_AUDIT_CONFIG)
    path = canonical_source_knowledge_path(video_id, config)
    before = read_source_file(path, config.root)
    artifact = load_source_knowledge(video_id, **source_configs)
    require(read_source_file(path, config.root) == before, "SOURCE_CHANGED")
    reference = GoldSourceArtifactReference("youtube", video_id, path.relative_to(config.root).as_posix(),
        sha256(before).hexdigest(), artifact.schema_version, artifact.source_audit_contract_version)
    return IntegrationSource(reference, artifact)


class IntegrationInspection:
    """No write capability. All observations refer to the same already loaded X.

    The package-internal _loaded capture is deliberately isolated here: opening
    another loader would risk inspecting X while applying to a different Y.
    Serialized observations are immutable strings and fail at capacity, never trim.
    """

    def __init__(self, gold_config, config):
        self.__session = open_gold_inspection(gold_config)
        self.__loaded = self.__session._loaded
        self.__config = config
        self.__observations = []
        self.__bytes = 0
        self.__documents = set()
        self.__calls = 0
        self.__aliases = {}

    @property
    def reference(self):
        return self.__loaded.reference

    @property
    def snapshot(self):
        return self.__loaded.snapshot

    @property
    def observations(self):
        return tuple(self.__observations)

    @property
    def calls(self):
        return self.__calls

    @property
    def declarations(self):
        return tuple((kind, alias, identity) for (kind, alias), identity in sorted(self.__aliases.items()))

    def allocate(self, kind, alias):
        from .gold_models import new_knowledge_id, new_document_id
        from .gold_document_models import new_topic_id
        import re
        require(kind in ("unit", "document", "topic") and isinstance(alias, str)
                and re.fullmatch(r"new-" + kind + r"-[1-9][0-9]*", alias) is not None, "NEW_ALIAS")
        key = kind, alias
        if key not in self.__aliases:
            self.__aliases[key] = {"unit": new_knowledge_id, "document": new_document_id, "topic": new_topic_id}[kind]()
        return self.__aliases[key]

    def query(self, name, **arguments):
        methods = {"search_gold", "get_gold_unit", "get_gold_document", "get_gold_topic",
                   "get_gold_provenance", "inspect_gold_structure"}
        require(name in methods, "INSPECTION_METHOD")
        if self.__calls >= self.__config.max_inspection_calls:
            raise IntegrationOperationalError("INSPECTION_LIMIT", phase="INSPECTION")
        self.__calls += 1
        if arguments.get("status") is not None:
            try:
                arguments = {**arguments, "status": GoldStatus(arguments["status"])}
            except (ValueError, TypeError):
                raise GoldInspectionRequestError("FILTER_STATUS") from None
        result = getattr(self.__session, name)(**arguments)
        encoded = json_value({"method": name, "arguments": arguments, "result": asdict(result)})
        size = len(encoded.encode("utf-8"))
        if self.__bytes + size > self.__config.max_inspection_bytes:
            raise IntegrationOperationalError("INSPECTION_LIMIT", phase="INSPECTION")
        self.__bytes += size
        self.__observations.append(encoded)
        if name == "get_gold_document":
            self.__documents.add(arguments["document_id"])
        return result

    def include_plan_context(self, plan):
        """Validator gets exact full before-state of every touched existing document."""
        from .gold_governance import gold_operation_document
        documents = {d.document_id for d in self.snapshot.documents}
        targets = {d.document_id for d in plan.proposed_layout} & documents
        by_unit = {u.knowledge_id: d.document_id for d in self.snapshot.documents
                   for u in d.units + d.historical_units}

        def visit(value):
            if type(value) is dict:
                for member in value.values():
                    visit(member)
            elif type(value) is list:
                for member in value:
                    visit(member)
            elif type(value) is str and value in by_unit:
                targets.add(by_unit[value])

        for operation in plan.operations:
            visit(gold_operation_document(operation))
        for identity in sorted(targets - self.__documents):
            self.query("get_gold_document", document_id=identity)
