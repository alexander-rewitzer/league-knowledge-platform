"""Offline Silver filesystem, crash/retry and Bronze immutability tests."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeConfig, SilverConfig, SilverContractError, SilverFilesystemError,
    SilverInputError, SilverIntegrityError, canonical_bronze_path,
    canonical_silver_path, clean_bronze_source, decode_silver_artifact,
    validate_silver_against_bronze,
)
from league_knowledge_platform import _bronze_filesystem as filesystem
from test_silver import bronze_bytes, bronze_fixture


class SilverPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bronze_config = BronzeConfig(self.root / "bronze")
        self.silver_config = SilverConfig(self.root / "silver")
        self.bronze = bronze_fixture("  Cafe\u0301\ttext ", "repeat", "repeat")
        self.bronze_path = canonical_bronze_path("A", self.bronze_config)
        self.bronze_path.parent.mkdir(parents=True)
        self.bronze_path.write_bytes(bronze_bytes(self.bronze))
        self.path = canonical_silver_path("A", self.silver_config)
        self.original_bronze = self.bronze_path.read_bytes()
        self.addCleanup(self.assert_bronze_unchanged)

    def assert_bronze_unchanged(self):
        self.assertTrue(self.bronze_path.exists())
        self.assertEqual(self.bronze_path.read_bytes(), self.original_bronze)

    def clean(self):
        return clean_bronze_source("A", bronze_config=self.bronze_config, silver_config=self.silver_config)

    def test_durable_standalone_result_with_no_network_or_database(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("No network")), \
                patch("sqlite3.connect", side_effect=AssertionError("No database")), \
                patch("league_knowledge_platform.silver.filesystem.confirm_durable",
                      wraps=filesystem.confirm_durable) as durable:
            result = self.clean()
        self.assertTrue(result.created)
        self.assertEqual((result.video_id, result.path), ("A", self.path))
        durable.assert_called_once_with(self.path)
        artifact = decode_silver_artifact(self.path.read_bytes())
        validate_silver_against_bronze(artifact, self.bronze, bronze_sha256=sha256(self.original_bronze).hexdigest())
        self.assertEqual(artifact["transcript"]["text"], "Café text repeat repeat")
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_compatible_content_keeps_bytes_including_diagnostic_version(self):
        self.clean()
        artifact = decode_silver_artifact(self.path.read_bytes())
        artifact["cleaning"]["unicode_version"] = "previous-runtime"
        original = json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.path.write_bytes(original)
        with patch.object(filesystem, "stage", side_effect=AssertionError("No rewrite")), \
                patch("league_knowledge_platform.silver.unicodedata.unidata_version", "next-runtime"):
            result = self.clean()
        self.assertFalse(result.created)
        self.assertEqual(self.path.read_bytes(), original)

    def test_changed_text_conflicts_with_same_or_different_unicode_version(self):
        self.clean()
        artifact = decode_silver_artifact(self.path.read_bytes())
        for change_version in (False, True):
            changed = deepcopy(artifact)
            if change_version:
                changed["cleaning"]["unicode_version"] = "different-runtime"
            changed["transcript"]["segments"][0]["text"] = "changed"
            changed["transcript"]["text"] = "changed repeat repeat"
            before = bronze_bytes(changed)
            self.path.write_bytes(before)
            with self.subTest(change_version=change_version), self.assertRaises(SilverIntegrityError):
                self.clean()
            self.assertEqual(self.path.read_bytes(), before)

    def test_future_normalization_output_cannot_silently_redefine_rule_one(self):
        self.clean()
        before = self.path.read_bytes()
        original_normalize = __import__("unicodedata").normalize
        def different_normalize(form, text):
            return original_normalize(form, text).replace("Café", "Cafe")
        with patch("league_knowledge_platform.silver.unicodedata.normalize", side_effect=different_normalize), \
                self.assertRaises((SilverContractError, SilverIntegrityError)):
            self.clean()
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_and_unsupported_existing_silver_are_never_overwritten(self):
        self.clean()
        valid = decode_silver_artifact(self.path.read_bytes())
        unsupported = deepcopy(valid)
        unsupported["cleaning"]["rule_version"] = 2
        unknown_schema = deepcopy(valid)
        unknown_schema["schema_version"] = 2
        for content in (b'{', bronze_bytes(unsupported), bronze_bytes(unknown_schema)):
            self.path.write_bytes(content)
            with self.subTest(content=content[:40]), self.assertRaises(SilverContractError):
                self.clean()
            self.assertEqual(self.path.read_bytes(), content)

    def test_missing_corrupt_unsupported_and_inconsistent_bronze_publish_nothing(self):
        unsupported = deepcopy(self.bronze)
        unsupported["schema_version"] = 2
        inconsistent = deepcopy(self.bronze)
        inconsistent["transcript"]["text"] = "truncated"
        try:
            for content in (None, b'{', b'\xff', bronze_bytes(unsupported), bronze_bytes(inconsistent)):
                if content is None:
                    self.bronze_path.unlink()
                else:
                    self.bronze_path.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(SilverInputError):
                    self.clean()
                self.assertFalse(self.path.exists())
                if content is not None:
                    self.assertEqual(self.bronze_path.read_bytes(), content)
        finally:
            self.bronze_path.write_bytes(self.original_bronze)

    def test_wrong_bronze_identity_fails_explicitly(self):
        other = deepcopy(self.bronze)
        other["source"].update(video_id="B", url="https://www.youtube.com/watch?v=B")
        other["selection"]["winner_quality"]["video_id"] = "B"
        content = bronze_bytes(other)
        self.bronze_path.write_bytes(content)
        try:
            with self.assertRaises(SilverIntegrityError):
                self.clean()
            self.assertEqual(self.bronze_path.read_bytes(), content)
            self.assertFalse(self.path.exists())
        finally:
            self.bronze_path.write_bytes(self.original_bronze)

    def test_overlapping_roots_are_rejected_before_any_write(self):
        for root in (self.root / "bronze", self.root, self.root / "bronze" / "nested"):
            with self.subTest(root=root), self.assertRaises(SilverIntegrityError):
                clean_bronze_source("A", bronze_config=self.bronze_config, silver_config=SilverConfig(root))
        self.assertFalse(self.path.exists())

    def test_read_error_has_safe_phase_without_source_contents(self):
        with patch("pathlib.Path.read_bytes", side_effect=OSError("secret transcript contents")), \
                self.assertRaises(SilverFilesystemError) as caught:
            self.clean()
        self.assertEqual(caught.exception.phase, "bronze_read")
        self.assertNotIn("secret", str(caught.exception))

    def test_atomic_visibility_and_bronze_is_never_flushed(self):
        original = filesystem.publish
        def publish(temporary, final):
            self.assertFalse(final.exists())
            self.assertEqual(decode_silver_artifact(temporary.read_bytes())["source"]["video_id"], "A")
            self.assertTrue(temporary.name.startswith(".silver-tmp-41-"))
            original(temporary, final)
            self.assertEqual(decode_silver_artifact(final.read_bytes())["source"]["video_id"], "A")
        with patch.object(filesystem, "publish", side_effect=publish), \
                patch.object(filesystem, "confirm_durable", wraps=filesystem.confirm_durable) as durable:
            self.clean()
        durable.assert_called_once_with(self.path)

    def test_prepublication_faults_leave_no_partial_canonical_and_allow_retry(self):
        for target in ("prepare_directory", "stage", "publish"):
            with self.subTest(target=target):
                with patch.object(filesystem, target, side_effect=OSError("failure")), \
                        self.assertRaises(SilverFilesystemError):
                    self.clean()
                self.assertFalse(self.path.exists())
                self.assertEqual(list(self.root.rglob("*.tmp")), [])
                self.assert_bronze_unchanged()
        self.assertTrue(self.clean().created)

    def test_serialization_failure_has_no_publication(self):
        with patch("league_knowledge_platform.silver.json.dumps", side_effect=ValueError("bad")), \
                self.assertRaises(SilverContractError) as caught:
            self.clean()
        self.assertEqual(caught.exception.phase, "serialization")
        self.assertFalse(self.path.exists())

    def test_flush_failure_removes_own_temporary_file(self):
        original = filesystem.stage
        def fail_flush(*args, **kwargs):
            with patch.object(filesystem.os, "fsync", side_effect=OSError("flush")):
                return original(*args, **kwargs)
        with patch.object(filesystem, "stage", side_effect=fail_flush), self.assertRaises(SilverFilesystemError):
            self.clean()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_temporary_validation_failure_never_publishes(self):
        original = filesystem.stage
        def corrupt(*args, **kwargs):
            temporary = original(*args, **kwargs)
            temporary.write_bytes(b'{"partial":')
            return temporary
        with patch.object(filesystem, "stage", side_effect=corrupt), \
                patch.object(filesystem, "publish") as publish, self.assertRaises(SilverContractError):
            self.clean()
        publish.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_failure_after_publication_keeps_complete_canonical_for_retry(self):
        original = filesystem.publish
        def publish_then_fail(*args):
            original(*args)
            raise OSError("sync failure")
        with patch.object(filesystem, "publish", side_effect=publish_then_fail), \
                self.assertRaises(SilverFilesystemError):
            self.clean()
        before = self.path.read_bytes()
        decode_silver_artifact(before)
        self.assertFalse(self.clean().created)
        self.assertEqual(self.path.read_bytes(), before)

    def test_final_validation_and_durability_failures_are_not_reported_as_success(self):
        for target in ("_read_silver", "filesystem.confirm_durable"):
            with self.subTest(target=target), \
                    patch("league_knowledge_platform.silver." + target, side_effect=OSError("failure")), \
                    self.assertRaises(SilverFilesystemError):
                self.clean()
            before = self.path.read_bytes()
            decode_silver_artifact(before)
            self.assertFalse(self.clean().created)
            self.assertEqual(self.path.read_bytes(), before)

    def test_cleanup_is_source_and_namespace_specific(self):
        self.path.parent.mkdir(parents=True)
        own = self.path.parent / (".silver-tmp-41-" + "a" * 32 + ".tmp")
        others = [self.path.parent / name for name in (
            ".bronze-tmp-41-" + "a" * 32 + ".tmp",
            ".silver-tmp-42-" + "a" * 32 + ".tmp", "user.tmp")]
        for path in (own, *others):
            path.write_bytes(b"partial")
        self.clean()
        self.assertFalse(own.exists())
        self.assertTrue(all(path.exists() for path in others))
        alias = self.path.parent / (".silver-tmp-41-" + "b" * 32 + ".tmp")
        os.link(self.path, alias)
        before = self.path.read_bytes()
        self.clean()
        self.assertFalse(alias.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_overwriting_publication_handles_compatible_or_conflicting_arrival(self):
        original = filesystem.publish
        def compatible_arrival(temporary, final):
            final.write_bytes(temporary.read_bytes())
            original(temporary, final)
        with patch.object(filesystem, "publish", side_effect=compatible_arrival):
            self.assertFalse(self.clean().created)
        self.path.unlink()
        def conflicting_arrival(temporary, final):
            other = decode_silver_artifact(temporary.read_bytes())
            other["source"]["title"] = "conflict"
            final.write_bytes(bronze_bytes(other))
            original(temporary, final)
        with patch.object(filesystem, "publish", side_effect=conflicting_arrival), \
                self.assertRaises(SilverIntegrityError):
            self.clean()
        self.assertEqual(decode_silver_artifact(self.path.read_bytes())["source"]["title"], "conflict")

    def test_symlinked_canonical_file_and_escaping_directory_are_rejected(self):
        # Windows directory junctions do not require developer-mode symlink privileges.
        if os.name == "nt":
            self.silver_config.root.mkdir()
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.path.parent),
                                     str(self.bronze_path.parent)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            try:
                with self.assertRaises(SilverIntegrityError):
                    self.clean()
            finally:
                self.path.parent.rmdir()
        else:
            self.silver_config.root.mkdir()
            self.path.parent.symlink_to(self.bronze_path.parent, target_is_directory=True)
            with self.assertRaises(SilverIntegrityError):
                self.clean()
            self.path.parent.unlink()
            self.path.parent.mkdir()
            self.path.symlink_to(self.bronze_path)
            with self.assertRaises(SilverIntegrityError):
                self.clean()

    def test_invalid_namespace_cannot_escape_temporary_directory(self):
        self.path.parent.mkdir(parents=True)
        for namespace in ("../elsewhere", "", "silver/../../bronze"):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                filesystem.stage(self.path.parent, "41", b"test", namespace=namespace)
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                filesystem.cleanup_temporary_files(self.path.parent, "41", namespace=namespace)

    def test_fresh_process_crash_and_retry_boundaries(self):
        script = r'''
import os, sys
from pathlib import Path
from league_knowledge_platform import BronzeConfig, SilverConfig, clean_bronze_source
from league_knowledge_platform import _bronze_filesystem as fs
root, boundary = Path(sys.argv[1]), sys.argv[2]
original_publish = fs.publish
if boundary == "during_write":
    def stage(directory, encoded_id, content, **kwargs):
        (directory / (".silver-tmp-" + encoded_id + "-" + "a" * 32 + ".tmp")).write_bytes(content[:20])
        os._exit(71)
    fs.stage = stage
elif boundary == "before_publish":
    def publish(*args):
        os._exit(71)
    fs.publish = publish
elif boundary == "after_publish":
    def publish(*args):
        original_publish(*args)
        os._exit(71)
    fs.publish = publish
clean_bronze_source("A", bronze_config=BronzeConfig(root / "bronze"), silver_config=SilverConfig(root / "silver"))
'''
        for boundary in ("during_write", "before_publish", "after_publish"):
            with self.subTest(boundary=boundary):
                if self.path.exists():
                    self.path.unlink()
                result = subprocess.run([sys.executable, "-c", script, str(self.root), boundary],
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 71, result.stderr.decode(errors="replace"))
                self.assertEqual(self.path.exists(), boundary == "after_publish")
                before = self.path.read_bytes() if self.path.exists() else None
                recovered = subprocess.run([sys.executable, "-c", script, str(self.root), "retry"],
                                           capture_output=True, timeout=30)
                self.assertEqual(recovered.returncode, 0, recovered.stderr.decode(errors="replace"))
                decode_silver_artifact(self.path.read_bytes())
                if before is not None:
                    self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
                self.assert_bronze_unchanged()


if __name__ == "__main__":
    unittest.main()
