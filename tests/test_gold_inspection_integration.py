"""Real canonical reader integration, immutable selections and zero write effects."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldContractError, GoldInspectionNotFoundError, GoldSnapshot,
    GoldStorageError, GoldVersionError, initialize_gold_store, open_gold_inspection,
)
from league_knowledge_platform import _gold_filesystem as fs
from league_knowledge_platform import gold_inspection as inspection_module
from gold_document_fixtures import document, topic
from test_gold_models import identity
from test_gold_inspection import InspectionFixture


class GoldInspectionIntegrationTests(InspectionFixture):
    def files(self):
        return {p.relative_to(self.config.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.config.root.rglob("*") if p.is_file()}

    def selected_directory(self):
        return self.config.root / "snapshots" / self.inspection.reference.storage_id

    def edit_pair(self, *, metadata_change=None, markdown_change=None):
        directory = self.selected_directory()
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        entry = manifest["documents"][0]
        if metadata_change:
            path = directory / entry["metadata_path"]
            value = json.loads(path.read_bytes())
            metadata_change(value)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if markdown_change:
            path = directory / entry["markdown_path"]
            path.write_bytes(markdown_change(path.read_bytes()))
        for key in ("markdown", "metadata"):
            entry[key + "_sha256"] = sha256((directory / entry[key + "_path"]).read_bytes()).hexdigest()
        raw = json.dumps(manifest).encode("utf-8")
        manifest_path.write_bytes(raw)
        selector = self.config.root / "CURRENT.json"
        value = json.loads(selector.read_bytes())
        value["manifest_sha256"] = sha256(raw).hexdigest()
        selector.write_text(json.dumps(value), encoding="utf-8")

    def test_open_and_all_inspection_methods_are_read_only_without_upstream_sql_or_provider(self):
        before = self.files()
        directories = sorted(str(p.relative_to(self.config.root)) for p in self.config.root.rglob("*"))
        with patch.object(fs, "write_exclusive", side_effect=AssertionError), \
             patch.object(fs, "prepare_directory", side_effect=AssertionError), \
             patch.object(fs, "publication_guard", side_effect=AssertionError), \
             patch.object(fs, "replace_selector", side_effect=AssertionError), \
             patch("sqlite3.connect", side_effect=AssertionError), \
             patch("socket.create_connection", side_effect=AssertionError):
            session = open_gold_inspection(self.config)
            with patch.object(fs, "read_bytes", side_effect=AssertionError("No subsequent file reads")):
                session.search_gold("Charm wave")
                session.get_gold_unit(identity())
                session.get_gold_document(document().document_id)
                session.get_gold_topic(document().document_id, topic().topic_id)
                session.get_gold_provenance(identity())
                session.inspect_gold_structure()
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(str(p.relative_to(self.config.root)) for p in self.config.root.rglob("*")), directories)

    def test_snapshot_is_loaded_once_and_every_result_is_bound_to_that_selection(self):
        with patch.object(inspection_module, "load_gold_snapshot", wraps=inspection_module.load_gold_snapshot) as load:
            session = open_gold_inspection(self.config)
            results = (session.search_gold("wave"), session.get_gold_unit(identity()),
                       session.get_gold_document(document().document_id),
                       session.get_gold_topic(document().document_id, topic().topic_id),
                       session.get_gold_provenance(identity()), session.inspect_gold_structure())
            self.assertEqual(load.call_count, 1)
            self.assertTrue(all(r.snapshot_reference == session.reference for r in results))
        with self.assertRaises(FrozenInstanceError):
            session._rows = ()

    def test_published_changes_are_visible_only_to_new_sessions(self):
        old = self.inspection
        doc = self.snapshot.documents[0]
        changed = replace(doc, version=replace(doc.version, document_version=2), title="Changed document",
            units=tuple(replace(u, content="Refreshed knowledge") if u.knowledge_id == identity() else u for u in doc.units))
        self.publish(GoldSnapshot((changed, *self.snapshot.documents[1:])))
        new = open_gold_inspection(self.config)
        self.assertNotEqual(old.reference, new.reference)
        self.assertEqual(old.get_gold_unit(identity()).value.document_version, 1)
        self.assertEqual(new.get_gold_unit(identity()).value.document_version, 2)
        self.assertEqual(old.search_gold("refreshed").matches, ())
        self.assertEqual(new.search_gold("refreshed").matches[0].unit.unit.knowledge_id, identity())
        self.assertEqual(old.get_gold_document(doc.document_id).value.title, doc.title)

    def test_relocation_changes_selection_without_changing_semantic_identity_or_version(self):
        self.publish(self.snapshot, routes={document().document_id: "relocated/context"})
        new = open_gold_inspection(self.config)
        self.assertNotEqual(new.reference, self.inspection.reference)
        self.assertEqual(new.get_gold_unit(identity()).value, self.inspection.get_gold_unit(identity()).value)

    def test_unselected_garbage_does_not_become_canonical(self):
        path = self.config.root / "snapshots" / "unselected"
        path.mkdir()
        (path / "bad.meta.json").write_bytes(b"{")
        self.assertEqual(open_gold_inspection(self.config).reference, self.inspection.reference)

    def test_hash_corruption_is_not_hidden_by_selective_search(self):
        path = self.selected_directory() / "champions" / "zed" / "laning.md"
        path.write_bytes(path.read_bytes() + b"invalid")
        with self.assertRaises(GoldStorageError):
            open_gold_inspection(self.config)

    def test_pair_validation_reuses_existing_canonical_parser(self):
        self.edit_pair(markdown_change=lambda b: b.replace(b"<!-- knowledge:", b"<!-- broken:", 1))
        before = self.files()
        with self.assertRaises(GoldContractError):
            open_gold_inspection(self.config)
        self.assertEqual(self.files(), before)

    def test_pair_metadata_mismatch_and_duplicate_ids_fail_explicitly(self):
        for change in (lambda d: d.update(title="Wrong title"), lambda d: d["units"].append(d["units"][0])):
            with self.subTest(change=change):
                self.edit_pair(metadata_change=change)
                with self.assertRaises(GoldContractError):
                    open_gold_inspection(self.config)
                # Restore through the fixture's exact canonical pair bytes, not inspection repair.
                self._restore_selected_snapshot()

    def _restore_selected_snapshot(self):
        from league_knowledge_platform import render_gold_document, encode_gold_document_metadata
        directory = self.selected_directory()
        selector = self.config.root / "CURRENT.json"
        manifest = json.loads((directory / "manifest.json").read_bytes())
        for entry, doc in zip(manifest["documents"], self.snapshot.documents):
            (directory / entry["markdown_path"]).write_bytes(render_gold_document(doc))
            (directory / entry["metadata_path"]).write_bytes(encode_gold_document_metadata(doc))
            for key in ("markdown", "metadata"):
                entry[key + "_sha256"] = sha256((directory / entry[key + "_path"]).read_bytes()).hexdigest()
        raw = json.dumps(manifest).encode()
        (directory / "manifest.json").write_bytes(raw)
        value = json.loads(selector.read_bytes())
        value["manifest_sha256"] = sha256(raw).hexdigest()
        selector.write_text(json.dumps(value), encoding="utf-8")

    def test_unsupported_store_and_document_versions_fail_without_migration(self):
        self.edit_pair(metadata_change=lambda d: d.update(gold_contract_version=99))
        with self.assertRaises(GoldVersionError):
            open_gold_inspection(self.config)
        self._restore_selected_snapshot()
        path = self.config.root / "CURRENT.json"
        value = json.loads(path.read_bytes())
        value["store_schema_version"] = 99
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(GoldVersionError):
            open_gold_inspection(self.config)

    def test_invalid_status_relations_and_reference_closure_are_rejected(self):
        changes = (lambda d: d["units"][0].update(status="DELETED"),
                   lambda d: d["units"][0].update(relations=[{"kind": "implies", "target_knowledge_id": identity(4)}]),
                   lambda d: d["units"][0].update(relations=[{"kind": "related_to", "target_knowledge_id": identity(99)}]))
        for change in changes:
            with self.subTest(change=change):
                self.edit_pair(metadata_change=change)
                with self.assertRaises(GoldContractError):
                    open_gold_inspection(self.config)
                self._restore_selected_snapshot()


class GoldInspectionEmptyTests(unittest.TestCase):
    def test_empty_canonical_gold_is_valid_and_does_not_fabricate_content(self):
        with tempfile.TemporaryDirectory() as directory:
            config = GoldConfig(Path(directory) / "gold")
            initialize_gold_store(config)
            session = open_gold_inspection(config)
            self.assertEqual(session.search_gold("Ahri").matches, ())
            self.assertEqual(session.inspect_gold_structure().value.entities, ())
            with self.assertRaises(GoldInspectionNotFoundError):
                session.get_gold_unit(identity())
            with self.assertRaises(GoldInspectionNotFoundError):
                session.get_gold_document(document().document_id)

    def test_missing_or_uninitialized_store_is_not_empty_and_is_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = GoldConfig(Path(directory) / "missing")
            with self.assertRaises(GoldStorageError):
                open_gold_inspection(missing)
            self.assertFalse(missing.root.exists())
            populated = GoldConfig(Path(directory) / "uninitialized")
            populated.root.mkdir()
            (populated.root / "orphan.md").write_text("synthetic", encoding="utf-8")
            with self.assertRaises(GoldStorageError):
                open_gold_inspection(populated)
            self.assertEqual(sorted(p.name for p in populated.root.iterdir()), ["orphan.md"])


if __name__ == "__main__":
    unittest.main()
