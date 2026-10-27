from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldBusyError, GoldConfig, GoldContractError, GoldSnapshot, GoldSnapshotReference,
    GoldStaleBaseError, GoldStorageError, GoldVersionError, initialize_gold_store,
    load_gold_document, load_gold_snapshot, publish_gold_snapshot, stage_gold_snapshot,
)
from league_knowledge_platform import _gold_filesystem as fs
from gold_document_fixtures import document, topic
from test_gold_models import unit


class GoldStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = GoldConfig(Path(self.directory.name) / "gold")
        self.base = initialize_gold_store(self.config)

    def publish(self, snapshot, *, routes=None):
        base = load_gold_snapshot(self.config).reference
        staged = stage_gold_snapshot(snapshot, config=self.config, base=base, routes=routes)
        return publish_gold_snapshot(staged, config=self.config, expected_base=base)

    def files(self):
        return {p.relative_to(self.config.root).as_posix(): p.read_bytes()
                for p in self.config.root.rglob("*") if p.is_file()}

    def selected_directory(self):
        return self.config.root / "snapshots" / load_gold_snapshot(self.config).reference.storage_id

    def test_empty_initialization_is_explicit_and_idempotent(self):
        self.assertEqual(load_gold_snapshot(self.config).snapshot, GoldSnapshot())
        self.assertEqual(initialize_gold_store(self.config), self.base)
        self.assertFalse(any(self.config.root.rglob("*.md")))
        missing = GoldConfig(Path(self.directory.name) / "missing")
        with self.assertRaises(GoldStorageError):
            load_gold_snapshot(missing)
        self.assertFalse(missing.root.exists())

    def test_stage_is_unselected_and_load_is_read_only(self):
        snapshot = GoldSnapshot((document(),))
        staged = stage_gold_snapshot(snapshot, config=self.config, base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).snapshot, GoldSnapshot())
        ref = publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).reference, ref)
        before = self.files()
        with patch("sqlite3.connect", side_effect=AssertionError), \
                patch("socket.create_connection", side_effect=AssertionError), \
                patch.object(fs, "write_exclusive", side_effect=AssertionError):
            loaded = load_gold_snapshot(self.config)
            self.assertEqual(load_gold_document(loaded, document().document_id), document())
        self.assertEqual(self.files(), before)

    def test_equal_publication_reuses_current_reference(self):
        snapshot = GoldSnapshot((document(),))
        first = self.publish(snapshot)
        self.assertEqual(self.publish(snapshot), first)

    def test_relocation_preserves_identity_version_and_content(self):
        snapshot = GoldSnapshot((document(),))
        first = self.publish(snapshot)
        second = self.publish(snapshot, routes={document().document_id: "concepts/tempo/reset-timing"})
        self.assertNotEqual(first, second)
        self.assertTrue((self.selected_directory() / "concepts/tempo/reset-timing.md").is_file())
        self.assertEqual(load_gold_snapshot(self.config).snapshot, snapshot)

    def test_versioned_publication_and_old_snapshot_read_remains_consistent(self):
        first = GoldSnapshot((document(),))
        self.publish(first)
        loaded_before = load_gold_snapshot(self.config)
        changed = replace(document(), title="Updated title", version=replace(document().version, document_version=2))
        self.publish(GoldSnapshot((changed,)))
        self.assertEqual(load_gold_document(loaded_before, document().document_id), document())
        self.assertEqual(load_gold_document(load_gold_snapshot(self.config), changed.document_id), changed)

    def test_two_stages_against_same_base_cannot_overwrite_each_other(self):
        snapshot = GoldSnapshot((document(),))
        a = stage_gold_snapshot(snapshot, config=self.config, base=self.base)
        b = stage_gold_snapshot(snapshot, config=self.config, base=self.base)
        publish_gold_snapshot(a, config=self.config, expected_base=self.base)
        with self.assertRaises(GoldStaleBaseError):
            publish_gold_snapshot(b, config=self.config, expected_base=self.base)
        with self.assertRaises(GoldStaleBaseError):
            stage_gold_snapshot(snapshot, config=self.config, base=self.base)

    def test_nested_local_publisher_reports_busy(self):
        staged = stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        with fs.publication_guard(self.config.root), self.assertRaises(GoldBusyError):
            publish_gold_snapshot(staged, config=self.config, expected_base=self.base)

    def test_failure_during_pair_write_keeps_old_canonical_state(self):
        original = fs.write_exclusive
        def fail_metadata(path, content):
            if path.name.endswith(".meta.json"):
                raise OSError("synthetic disk failure")
            return original(path, content)
        with patch.object(fs, "write_exclusive", side_effect=fail_metadata), self.assertRaises(GoldStorageError):
            stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).reference, self.base)
        self.assertEqual(load_gold_snapshot(self.config).snapshot, GoldSnapshot())
        self.publish(GoldSnapshot((document(),)))

    def test_failure_before_selector_replacement_keeps_old_state(self):
        staged = stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        with patch.object(fs, "replace_selector", side_effect=OSError), self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).reference, self.base)

    def test_interrupted_selector_staging_does_not_damage_current(self):
        staged = stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        original = fs.write_exclusive
        def fail_temporary(path, content):
            if path.name.startswith(".current-"):
                path.write_bytes(content[:12])
                raise OSError("synthetic interrupted pointer write")
            return original(path, content)
        with patch.object(fs, "write_exclusive", side_effect=fail_temporary), self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).reference, self.base)
        publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).snapshot, GoldSnapshot((document(),)))

    def test_failure_after_selector_replacement_leaves_complete_new_state(self):
        snapshot = GoldSnapshot((document(),))
        staged = stage_gold_snapshot(snapshot, config=self.config, base=self.base)
        original = fs.replace_selector
        def fail_after(root, content):
            original(root, content)
            raise OSError("synthetic post-publication interruption")
        with patch.object(fs, "replace_selector", side_effect=fail_after), self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).snapshot, snapshot)
        self.assertEqual(load_gold_snapshot(self.config).reference, staged.reference)

    def test_missing_corrupt_or_unknown_selector_never_becomes_empty(self):
        path = self.config.root / "CURRENT.json"
        original = path.read_bytes()
        for bad in (b"{", original.replace(b'"store_schema_version": 1', b'"store_schema_version": 2'),
                    original.replace(b'"storage_id": "', b'"storage_id": "../', 1)):
            path.write_bytes(bad)
            with self.assertRaises(GoldContractError):
                load_gold_snapshot(self.config)
            with self.assertRaises(GoldContractError):
                initialize_gold_store(self.config)
        path.unlink()
        with self.assertRaises(GoldStorageError):
            initialize_gold_store(self.config)

    def test_missing_tampered_files_and_staged_corruption_rejected(self):
        staged = stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        md = next((self.config.root / "snapshots" / staged.reference.storage_id).rglob("*.md"))
        md.write_bytes(md.read_bytes() + b"tampered")
        with self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=self.config, expected_base=self.base)
        self.assertEqual(load_gold_snapshot(self.config).reference, self.base)
        self.publish(GoldSnapshot((document(),)))
        next(self.selected_directory().rglob("*.meta.json")).unlink()
        with self.assertRaises(GoldStorageError):
            load_gold_snapshot(self.config)

    def test_manifest_hash_and_document_mismatch_rejected_even_with_matching_file_hashes(self):
        self.publish(GoldSnapshot((document(),)))
        directory = self.selected_directory()
        manifest = directory / "manifest.json"
        raw = manifest.read_bytes()
        manifest.write_bytes(raw + b" ")
        with self.assertRaises(GoldStorageError):
            load_gold_snapshot(self.config)
        value = json.loads(raw)
        value["documents"][0]["document_version"] = 999
        new = json.dumps(value).encode()
        manifest.write_bytes(new)
        selector = self.config.root / "CURRENT.json"
        current = json.loads(selector.read_bytes())
        current["manifest_sha256"] = sha256(new).hexdigest()
        selector.write_text(json.dumps(current), encoding="utf-8")
        with self.assertRaises(GoldStorageError):
            load_gold_snapshot(self.config)

    def test_unsafe_routes_and_path_aliases_rejected(self):
        d = document()
        routes = ("../escape", "/absolute", "C:/escape", "a\\b", "a//b", "a/./b", "CON",
                  "com1", "a/aux.txt", "trailing.", "a/escape ", "a/escape:stream")
        for route in routes:
            with self.subTest(route=route), self.assertRaises(GoldStorageError):
                stage_gold_snapshot(GoldSnapshot((d,)), config=self.config, base=self.base,
                                    routes={d.document_id: route})
        self.assertEqual(load_gold_snapshot(self.config).reference, self.base)
        other = document(2, topics=(topic(2, (3,)),), units=(unit(3),), domain="abilities")
        with self.assertRaises(GoldStorageError):
            stage_gold_snapshot(GoldSnapshot((d, other)), config=self.config, base=self.base,
                                routes={d.document_id: "same", other.document_id: "SAME"})

    def test_linked_root_or_parent_is_rejected_without_writing_through_it(self):
        target = Path(self.directory.name) / "outside"
        target.mkdir()
        link = Path(self.directory.name) / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            # Windows commonly forbids unprivileged symlinks; use an actual junction.
            import os
            if os.name != "nt":
                raise
            import subprocess
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: link.rmdir() if link.exists() and not link.is_symlink() else link.unlink(missing_ok=True))
        for root in (link, link / "nested"):
            with self.subTest(root=root), self.assertRaises(GoldStorageError):
                initialize_gold_store(GoldConfig(root))
        self.assertEqual(list(target.iterdir()), [])

    def test_staged_reference_cannot_cross_store_or_base(self):
        staged = stage_gold_snapshot(GoldSnapshot((document(),)), config=self.config, base=self.base)
        other = GoldConfig(Path(self.directory.name) / "other")
        other_base = initialize_gold_store(other)
        with self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=other, expected_base=other_base)
        with self.assertRaises(GoldStorageError):
            publish_gold_snapshot(staged, config=self.config,
                                  expected_base=GoldSnapshotReference("a" * 32, "b" * 64))
