"""Offline durable outcomes, first-result reuse, immutable upstream and crash recovery."""

from copy import deepcopy
from dataclasses import replace
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
    BronzeConfig, SilverConfig, SegmentationConfig, SegmentationAttempt, SegmentationOutcome,
    SegmentationContractError, SegmentationFilesystemError, SegmentationInputError,
    SegmentationIntegrityError, SegmentationProviderError, SegmentationValidationError,
    canonical_bronze_path, canonical_silver_path, canonical_segmentation_path,
    segment_silver_source, load_segmentation_artifact, construct_segmentation_artifact,
    decode_segmentation_artifact, content_sections,
)
from league_knowledge_platform import _bronze_filesystem as filesystem
from segmentation_http_fixtures import METADATA, FakeSegmentation, bronze_bytes, proposal
from test_silver import bronze_fixture, silver_fixture


class SegmentationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bronze_config = BronzeConfig(self.root / "bronze")
        self.silver_config = SilverConfig(self.root / "silver")
        self.config = SegmentationConfig(root=self.root / "sections", retry_delays_seconds=(0, 0))
        bronze = bronze_fixture("Ahri mechanics", "", "Sponsor product")
        self.silver = silver_fixture(bronze)
        self.bronze_path = canonical_bronze_path("A", self.bronze_config)
        self.silver_path = canonical_silver_path("A", self.silver_config)
        self.path = canonical_segmentation_path("A", self.config)
        for path, artifact in ((self.bronze_path, bronze), (self.silver_path, self.silver)):
            path.parent.mkdir(parents=True)
            path.write_bytes(bronze_bytes(artifact))
        self.before_bronze, self.before_silver = self.bronze_path.read_bytes(), self.silver_path.read_bytes()
        self.addCleanup(self.assert_upstream_unchanged)

    def assert_upstream_unchanged(self):
        self.assertEqual(self.bronze_path.read_bytes(), self.before_bronze)
        self.assertEqual(self.silver_path.read_bytes(), self.before_silver)

    def process(self, fake=None):
        return segment_silver_source("A", capability=fake,
            silver_config=self.silver_config, bronze_config=self.bronze_config, config=self.config)

    def fake(self):
        return FakeSegmentation([proposal((0, 3, "CONTENT"), (6, 6, "ADVERTISEMENT"))])

    def test_complete_explicit_processing_has_no_acquisition_sql_or_live_network(self):
        fake = self.fake()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("No network")), \
                patch("sqlite3.connect", side_effect=AssertionError("No database")), \
                patch("league_knowledge_platform.YouTubeDiscovery.discover_page", side_effect=AssertionError("No discovery")), \
                patch("league_knowledge_platform.SQLiteSourceCatalog.register_integrated", side_effect=AssertionError("No registration")):
            result = self.process(fake)
        self.assertTrue(result.created)
        self.assertTrue(result.successful_segmentation)
        self.assertIs(result.outcome, SegmentationOutcome.SEGMENTED)
        self.assertEqual(len(fake.requests), 1)
        loaded = load_segmentation_artifact("A", silver_config=self.silver_config, config=self.config)
        self.assertEqual(result.artifact, loaded)
        self.assertEqual(len(content_sections(loaded)), 1)
        self.assertEqual(loaded["sections"][1]["kind"], "ADVERTISEMENT")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_result_is_byte_preserving_without_semantic_calls_or_key(self):
        first = self.process(self.fake())
        content = json.dumps(first.artifact, separators=(",", ":")).encode("utf-8")
        self.path.write_bytes(content)
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(filesystem, "stage", side_effect=AssertionError("No rewrite")):
            second = self.process()
        self.assertFalse(second.created)
        self.assertEqual(second.artifact, first.artifact)
        self.assertEqual(self.path.read_bytes(), content)

    def test_durable_empty_outcome_and_reprocessing_without_provider(self):
        empty = silver_fixture(bronze_fixture("", "", ""))
        self.silver_path.write_bytes(bronze_bytes(empty))
        before = self.silver_path.read_bytes()
        try:
            fake = FakeSegmentation([])
            with patch.dict("os.environ", {}, clear=True):
                first = self.process(fake)
                second = self.process(fake)
            self.assertTrue(first.created)
            self.assertFalse(first.successful_segmentation)
            self.assertIs(first.outcome, SegmentationOutcome.NO_SEGMENTABLE_CONTENT)
            self.assertEqual(first.artifact["sections"], [])
            self.assertEqual(first.artifact["attempts"], [])
            self.assertEqual(first.artifact["silver"]["sha256"], sha256(before).hexdigest())
            self.assertEqual(fake.requests, [])
            self.assertFalse(second.created)
            self.assertEqual(second.artifact, first.artifact)
            self.assertEqual(self.silver_path.read_bytes(), before)
        finally:
            self.silver_path.write_bytes(self.before_silver)

    def test_missing_provider_configuration_is_explicit_not_fake_success(self):
        with self.assertRaises(SegmentationProviderError) as caught:
            self.process()
        self.assertEqual(caught.exception.phase, "CONFIGURATION")
        self.assertEqual(caught.exception.attempts, ())
        self.assertFalse(self.path.exists())

    def test_exhausted_validation_leaves_no_canonical_or_fixed_size_fallback(self):
        fake = FakeSegmentation([proposal()] * 4)
        with self.assertRaises(SegmentationValidationError):
            self.process(fake)
        self.assertEqual(len(fake.requests), 3)
        self.assertFalse(self.path.exists())

    def test_corrupt_unsupported_or_incompatible_existing_artifacts_are_never_replaced(self):
        original = self.process(self.fake()).artifact
        unsupported = deepcopy(original)
        unsupported["segmentation_contract_version"] = 2
        conflict = deepcopy(original)
        conflict["silver"]["sha256"] = "0" * 64
        rewritten = deepcopy(original)
        rewritten["sections"][0]["text"] = "Rewritten by a model"
        for content in (b'{', bronze_bytes(unsupported), bronze_bytes(conflict), bronze_bytes(rewritten)):
            self.path.write_bytes(content)
            fake = FakeSegmentation([])
            with self.subTest(content=content[:35]), self.assertRaises((SegmentationContractError, SegmentationIntegrityError)):
                self.process(fake)
            self.assertEqual(self.path.read_bytes(), content)
            self.assertEqual(fake.requests, [])

    def test_incompatible_empty_outcome_never_overwrites_nonempty_result(self):
        self.process(self.fake())
        empty = silver_fixture(bronze_fixture("", "", ""))
        incompatible = construct_segmentation_artifact(empty, silver_sha256=sha256(self.before_silver).hexdigest())
        self.path.write_bytes(bronze_bytes(incompatible))
        before = self.path.read_bytes()
        with self.assertRaises(SegmentationIntegrityError):
            self.process()
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_missing_unsupported_and_wrong_identity_silver_do_not_call_provider(self):
        unsupported = deepcopy(self.silver)
        unsupported["schema_version"] = 2
        wrong = deepcopy(self.silver)
        wrong["source"].update(video_id="B", url="https://www.youtube.com/watch?v=B")
        wrong["bronze"]["relative_path"] = "youtube/id-42.json"
        try:
            for content in (None, b'{', b'\xff', bronze_bytes(unsupported), bronze_bytes(wrong)):
                if content is None:
                    self.silver_path.unlink()
                else:
                    self.silver_path.write_bytes(content)
                fake = FakeSegmentation([])
                with self.subTest(content=content), self.assertRaises((SegmentationInputError, SegmentationIntegrityError)):
                    self.process(fake)
                self.assertFalse(self.path.exists())
                self.assertEqual(fake.requests, [])
        finally:
            self.silver_path.write_bytes(self.before_silver)

    def test_read_only_loading_never_flushes_cleans_or_calls_a_provider(self):
        first = self.process(self.fake())
        with patch.object(filesystem, "confirm_durable", side_effect=AssertionError("No writes")), \
                patch.object(filesystem, "cleanup_temporary_files", side_effect=AssertionError("No cleanup")):
            self.assertEqual(load_segmentation_artifact("A", silver_config=self.silver_config, config=self.config), first.artifact)

    def test_first_durable_valid_result_wins_even_with_different_valid_boundaries(self):
        original_publish = filesystem.publish
        winning = construct_segmentation_artifact(self.silver, silver_sha256=sha256(self.before_silver).hexdigest(),
            proposal=proposal((0, 6, "CONTENT")), attempts=(SegmentationAttempt(1, metadata=METADATA),))
        def appear(temporary, final):
            final.write_bytes(bronze_bytes(winning))
            original_publish(temporary, final)
        with patch.object(filesystem, "publish", side_effect=appear):
            result = self.process(self.fake())
        self.assertFalse(result.created)
        self.assertEqual(result.artifact, winning)
        self.assertEqual(len(result.artifact["sections"]), 1)

    def test_appearing_incompatible_result_is_not_replaced(self):
        original_publish = filesystem.publish
        def appear(temporary, final):
            final.write_bytes(b'{"corrupt":')
            original_publish(temporary, final)
        with patch.object(filesystem, "publish", side_effect=appear), self.assertRaises(SegmentationContractError):
            self.process(self.fake())
        self.assertEqual(self.path.read_bytes(), b'{"corrupt":')

    def test_atomic_visibility_and_only_output_is_flushed(self):
        original = filesystem.publish
        def publish(temporary, final):
            self.assertFalse(final.exists())
            self.assertTrue(temporary.name.startswith(".segmentation-tmp-41-"))
            decode_segmentation_artifact(temporary.read_bytes())
            original(temporary, final)
            decode_segmentation_artifact(final.read_bytes())
        with patch.object(filesystem, "publish", side_effect=publish), \
                patch.object(filesystem, "confirm_durable", wraps=filesystem.confirm_durable) as durable:
            self.process(self.fake())
        durable.assert_called_once_with(self.path)

    def test_write_flush_serialization_and_staged_validation_failures_publish_nothing(self):
        for target in ("prepare_directory", "stage", "publish"):
            with self.subTest(target=target), patch.object(filesystem, target, side_effect=OSError("failure")), \
                    self.assertRaises(SegmentationFilesystemError):
                self.process(self.fake())
            self.assertFalse(self.path.exists())
            self.assertEqual(list(self.root.rglob("*.tmp")), [])
        with patch("league_knowledge_platform.segmentation.json.dumps", side_effect=ValueError("serialization")), \
                self.assertRaises(SegmentationContractError):
            self.process(self.fake())
        self.assertFalse(self.path.exists())
        original_stage = filesystem.stage
        def corrupt(*args, **kwargs):
            temporary = original_stage(*args, **kwargs)
            temporary.write_bytes(b'{')
            return temporary
        with patch.object(filesystem, "stage", side_effect=corrupt), self.assertRaises(SegmentationContractError):
            self.process(self.fake())
        self.assertFalse(self.path.exists())
        def fail_flush(*args, **kwargs):
            with patch.object(filesystem.os, "fsync", side_effect=OSError("flush")):
                return original_stage(*args, **kwargs)
        with patch.object(filesystem, "stage", side_effect=fail_flush), self.assertRaises(SegmentationFilesystemError):
            self.process(self.fake())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_failure_after_publication_keeps_complete_result_and_retry_skips_semantics(self):
        original = filesystem.publish
        def publish_then_fail(*args):
            original(*args)
            raise OSError("directory flush")
        with patch.object(filesystem, "publish", side_effect=publish_then_fail), self.assertRaises(SegmentationFilesystemError):
            self.process(self.fake())
        before = self.path.read_bytes()
        self.assertFalse(self.process().created)
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(filesystem, "confirm_durable", side_effect=OSError("durability")), self.assertRaises(SegmentationFilesystemError):
            self.process()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cleanup_only_matches_source_and_segmentation_namespace(self):
        self.path.parent.mkdir(parents=True)
        own = self.path.parent / (".segmentation-tmp-41-" + "a" * 32 + ".tmp")
        others = [self.path.parent / name for name in ("user.tmp", ".silver-tmp-41-" + "a" * 32 + ".tmp",
                                                       ".segmentation-tmp-42-" + "a" * 32 + ".tmp")]
        for path in (own, *others):
            path.write_bytes(b"partial")
        self.process(self.fake())
        self.assertFalse(own.exists())
        self.assertTrue(all(path.exists() for path in others))

    def test_safe_case_sensitive_naming_and_protected_roots(self):
        self.assertNotEqual(canonical_segmentation_path("A", self.config), canonical_segmentation_path("a", self.config))
        self.assertEqual(canonical_segmentation_path("CON", self.config).name, "id-434f4e.json")
        for invalid in ("", "../A", "A/B", "A\n", "é"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                canonical_segmentation_path(invalid, self.config)
        for root in (self.root, self.root / "silver", self.root / "bronze" / "nested"):
            with self.subTest(root=root), self.assertRaises(SegmentationIntegrityError):
                segment_silver_source("A", silver_config=self.silver_config, bronze_config=self.bronze_config,
                    config=replace(self.config, root=root), capability=self.fake())

    def test_directory_escape_is_rejected_before_semantics(self):
        Path(self.config.root).mkdir()
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.path.parent), str(self.silver_path.parent)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
        else:
            self.path.parent.symlink_to(self.silver_path.parent, target_is_directory=True)
        try:
            with self.assertRaises(SegmentationIntegrityError):
                self.process(self.fake())
        finally:
            if os.name == "nt":
                self.path.parent.rmdir()
            else:
                self.path.parent.unlink()

    def test_fresh_process_crash_recovery_before_during_and_after_publication(self):
        script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from league_knowledge_platform import BronzeConfig, SilverConfig, SegmentationConfig, segment_silver_source
from league_knowledge_platform import _bronze_filesystem as fs
from segmentation_http_fixtures import FakeSegmentation, proposal
root, boundary = Path(sys.argv[1]), sys.argv[2]
original = fs.publish
if boundary == "during_write":
    def stage(directory, encoded, content, **kwargs):
        (directory / (".segmentation-tmp-" + encoded + "-" + "a" * 32 + ".tmp")).write_bytes(content[:20])
        os._exit(71)
    fs.stage = stage
elif boundary == "before_publish":
    def publish(*args):
        os._exit(71)
    fs.publish = publish
elif boundary == "after_publish":
    def publish(*args):
        original(*args)
        os._exit(71)
    fs.publish = publish
capability = FakeSegmentation([] if boundary == "reuse" else [proposal((0, 6, "CONTENT"))])
segment_silver_source("A", capability=capability, silver_config=SilverConfig(root / "silver"),
    bronze_config=BronzeConfig(root / "bronze"), config=SegmentationConfig(root / "sections", retry_delays_seconds=(0, 0)))
'''
        for boundary in ("during_write", "before_publish", "after_publish"):
            with self.subTest(boundary=boundary):
                if self.path.exists():
                    self.path.unlink()
                crashed = subprocess.run([sys.executable, "-c", script, str(self.root), boundary], capture_output=True, timeout=30)
                self.assertEqual(crashed.returncode, 71, crashed.stderr.decode(errors="replace"))
                self.assertEqual(self.path.exists(), boundary == "after_publish")
                before = self.path.read_bytes() if self.path.exists() else None
                retry = subprocess.run([sys.executable, "-c", script, str(self.root), "reuse" if before else "retry"], capture_output=True, timeout=30)
                self.assertEqual(retry.returncode, 0, retry.stderr.decode(errors="replace"))
                decode_segmentation_artifact(self.path.read_bytes())
                if before:
                    self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
                self.assert_upstream_unchanged()


if __name__ == "__main__":
    unittest.main()
