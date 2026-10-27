"""Canonical Gold pair snapshots with explicit staging and atomic local selection."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from . import _gold_filesystem as fs
from .gold_document_models import (
    GoldConfig, GoldDocumentError, GoldSnapshot, GoldStaleBaseError, GoldStorageError,
)
from .gold_documents import (
    _array, _decode_json, _json_bytes, _keys, decode_gold_document_pair,
    encode_gold_document_metadata, render_gold_document, validate_document_transition,
    validate_gold_snapshot,
)
from .gold_models import GoldVersionError, _identity


@dataclass(frozen=True, slots=True)
class GoldSnapshotReference:
    storage_id: str
    manifest_sha256: str

    def __post_init__(self):
        if (not isinstance(self.storage_id, str) or re.fullmatch(r"[0-9a-f]{32}", self.storage_id) is None
                or not isinstance(self.manifest_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) is None):
            raise GoldStorageError("SNAPSHOT_REFERENCE")


@dataclass(frozen=True, slots=True)
class LoadedGoldSnapshot:
    reference: GoldSnapshotReference
    snapshot: GoldSnapshot


@dataclass(frozen=True, slots=True)
class StagedGoldSnapshot:
    reference: GoldSnapshotReference
    base: GoldSnapshotReference | None
    root: Path = field(repr=False)


@contextmanager
def _storage_errors():
    try:
        yield
    except OSError:
        raise GoldStorageError("FILESYSTEM") from None


def _version(value):
    if type(value) is not int or value != 1:
        raise GoldVersionError("STORE_VERSION")


def _config(config):
    if type(config) is not GoldConfig:
        raise GoldStorageError("CONFIG")
    fs.safe_path(config.root, "CURRENT.json")


def _selector(reference):
    return {"store_schema_version": 1, "storage_id": reference.storage_id,
            "manifest_sha256": reference.manifest_sha256}


def _current(config):
    value = _keys(_decode_json(fs.read_bytes(fs.safe_path(config.root, "CURRENT.json"))),
                  "store_schema_version storage_id manifest_sha256")
    _version(value["store_schema_version"])
    return GoldSnapshotReference(value["storage_id"], value["manifest_sha256"])


def _snapshot_directory(config, reference):
    if type(reference) is not GoldSnapshotReference:
        raise GoldStorageError("SNAPSHOT_REFERENCE")
    return fs.safe_path(config.root, f"snapshots/{reference.storage_id}")


def _manifest(config, reference):
    directory = _snapshot_directory(config, reference)
    content = fs.read_bytes(fs.safe_path(directory, "manifest.json"))
    if sha256(content).hexdigest() != reference.manifest_sha256:
        raise GoldStorageError("MANIFEST_HASH")
    value = _keys(_decode_json(content), "store_schema_version documents")
    _version(value["store_schema_version"])
    documents, seen = [], set()
    for entry in _array(value["documents"]):
        _keys(entry, "document_id document_version markdown_path metadata_path markdown_sha256 metadata_sha256")
        _identity(entry["document_id"], "gdoc")
        md, meta = entry["markdown_path"], entry["metadata_path"]
        if not isinstance(md, str) or not md.endswith(".md") or meta != md[:-3] + ".meta.json":
            raise GoldStorageError("PAIR_PATH")
        for path in (md, meta):
            fs.safe_path(directory, path)
            if path.casefold() in seen or path.casefold() == "manifest.json":
                raise GoldStorageError("PATH_COLLISION")
            seen.add(path.casefold())
        pair = []
        for path_key, hash_key in (("markdown_path", "markdown_sha256"), ("metadata_path", "metadata_sha256")):
            data = fs.read_bytes(fs.safe_path(directory, entry[path_key]))
            if sha256(data).hexdigest() != entry[hash_key]:
                raise GoldStorageError("FILE_HASH")
            pair.append(data)
        document = decode_gold_document_pair(*pair)
        if (document.document_id != entry["document_id"] or type(entry["document_version"]) is not int
                or document.document_version != entry["document_version"]):
            raise GoldStorageError("MANIFEST_DOCUMENT")
        documents.append(document)
    snapshot = GoldSnapshot(tuple(documents))
    validate_gold_snapshot(snapshot)
    return content, snapshot


def load_gold_snapshot(config=GoldConfig()) -> LoadedGoldSnapshot:
    """Read the selector once and validate the full selected, immutable snapshot."""
    with _storage_errors():
        _config(config)
        reference = _current(config)
        _, snapshot = _manifest(config, reference)
        return LoadedGoldSnapshot(reference, snapshot)


def load_gold_document(snapshot: LoadedGoldSnapshot, document_id: str):
    if type(snapshot) is not LoadedGoldSnapshot:
        raise GoldDocumentError("LOADED_SNAPSHOT")
    _identity(document_id, "gdoc")
    validate_gold_snapshot(snapshot.snapshot)
    for document in snapshot.snapshot.documents:
        if document.document_id == document_id:
            return document
    raise GoldDocumentError("UNKNOWN_DOCUMENT")


def stage_gold_snapshot(snapshot: GoldSnapshot, *, config=GoldConfig(), base: GoldSnapshotReference,
                        routes=None) -> StagedGoldSnapshot:
    """Write an unselected complete snapshot. No semantic decision or canonical change."""
    with _storage_errors():
        _config(config)
        current = load_gold_snapshot(config)
        if current.reference != base:
            raise GoldStaleBaseError("STALE_BASE")
        validate_document_transition(current.snapshot, snapshot)
        return _stage(snapshot, config, base, routes)


def _stage(snapshot, config, base, routes=None):
    validate_gold_snapshot(snapshot)
    ids = {d.document_id for d in snapshot.documents}
    if routes is None:
        routes = {}
    if type(routes) is not dict or set(routes) - ids:
        raise GoldStorageError("ROUTES")
    storage_id = uuid4().hex
    directory = fs.safe_path(config.root, f"snapshots/{storage_id}")
    # Exclusive directory ownership; never rewrite a previous immutable snapshot.
    fs.prepare_directory(directory.parent)
    directory.mkdir()
    fs.sync_directory(directory.parent)
    entries, seen = [], set()
    for document in snapshot.documents:
        stem = routes.get(document.document_id,
            f"{document.entity.namespace}/{document.entity.entity_key}/{document.domain_key}")
        if not isinstance(stem, str):
            raise GoldStorageError("ROUTES")
        fs.safe_path(directory, stem)
        paths = (stem + ".md", stem + ".meta.json")
        for path in paths:
            fs.safe_path(directory, path)
            if path.casefold() in seen:
                raise GoldStorageError("PATH_COLLISION")
            seen.add(path.casefold())
        markdown, metadata = render_gold_document(document), encode_gold_document_metadata(document)
        fs.write_exclusive(fs.safe_path(directory, paths[0]), markdown)
        fs.write_exclusive(fs.safe_path(directory, paths[1]), metadata)
        entries.append({"document_id": document.document_id, "document_version": document.document_version,
                        "markdown_path": paths[0], "metadata_path": paths[1],
                        "markdown_sha256": sha256(markdown).hexdigest(),
                        "metadata_sha256": sha256(metadata).hexdigest()})
    manifest = _json_bytes({"store_schema_version": 1, "documents": entries})
    fs.write_exclusive(fs.safe_path(directory, "manifest.json"), manifest)
    reference = GoldSnapshotReference(storage_id, sha256(manifest).hexdigest())
    _manifest(config, reference)
    return StagedGoldSnapshot(reference, base, config.root)


def publish_gold_snapshot(staged: StagedGoldSnapshot, *, config=GoldConfig(),
                          expected_base: GoldSnapshotReference) -> GoldSnapshotReference:
    with _storage_errors():
        _config(config)
        if (type(staged) is not StagedGoldSnapshot or staged.root != config.root
                or staged.base != expected_base):
            raise GoldStorageError("STAGED_CONTEXT")
        # All expensive file work is staged before taking the short publication guard.
        manifest, proposed = _manifest(config, staged.reference)
        current = load_gold_snapshot(config)
        validate_document_transition(current.snapshot, proposed)
        old_manifest, _ = _manifest(config, current.reference)
        with fs.publication_guard(config.root):
            if current.reference != expected_base or _current(config) != expected_base:
                raise GoldStaleBaseError("STALE_BASE")
            if manifest == old_manifest:
                return current.reference
            fs.replace_selector(config.root, _json_bytes(_selector(staged.reference)))
        return staged.reference


def initialize_gold_store(config=GoldConfig()) -> GoldSnapshotReference:
    """Explicitly initialize an empty Gold store; never infer emptiness during load."""
    with _storage_errors():
        _config(config)
        with fs.publication_guard(config.root):
            if fs.safe_path(config.root, "CURRENT.json").exists():
                return load_gold_snapshot(config).reference
            # A missing selector in a populated store is corruption, not initialization.
            allowed = {".publish.lock"}
            if any(path.name not in allowed for path in config.root.iterdir()):
                raise GoldStorageError("NONEMPTY_UNINITIALIZED_STORE")
            staged = _stage(GoldSnapshot(), config, None)
            fs.replace_selector(config.root, _json_bytes(_selector(staged.reference)))
            return staged.reference
