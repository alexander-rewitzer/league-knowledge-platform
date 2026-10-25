"""Canonical reuse, compatibility, publication races and fresh-process crashes."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    SectionJudgeCapabilityError, SectionKnowledgeContractError, SectionKnowledgeFilesystemError,
    SectionValidationIntegrityError, canonical_section_knowledge_path, load_validated_section_knowledge,
    process_section_knowledge,
)
from league_knowledge_platform import _bronze_filesystem as fs
from league_knowledge_platform.section_knowledge import checkpoint_document, decode_section_checkpoint, proposal_sha256
from section_validation_fixtures import FakeExtractor, FakeJudge, evaluation, initial_proposal, response, workspace


class SectionKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.configs, self.unit = workspace(self.root)
        self.path = canonical_section_knowledge_path("A", self.unit.section_id, self.configs["config"])

    def process(self, *, statement="Usually wait for the cooldown.", judge=None, **extra):
        return process_section_knowledge("A", self.unit.section_id, initial_proposal=initial_proposal(self.unit, statement),
                                         judge_capability=judge or FakeJudge([evaluation()]), **self.configs, **extra)

    def test_round_trip_and_compatible_reuse_without_capabilities_or_credentials(self):
        created = self.process()
        before = self.path.read_bytes()
        reused = process_section_knowledge("A", self.unit.section_id, **self.configs)
        self.assertFalse(reused.created)
        self.assertEqual(reused.operation_rounds_used, 0)
        self.assertEqual(reused.checkpoint, created.checkpoint)
        self.assertEqual(self.path.read_bytes(), before)
        loaded = load_validated_section_knowledge("A", self.unit.section_id, silver_config=self.configs["silver_config"],
                                                   segmentation_config=self.configs["segmentation_config"], config=self.configs["config"])
        self.assertEqual(loaded, created.checkpoint)
        self.assertEqual(loaded.rounds[0].proposal_sha256, proposal_sha256(loaded.accepted_proposal))

    def test_model_configuration_change_does_not_regenerate_compatible_checkpoint(self):
        self.process()
        extractor, judge = FakeExtractor([]), FakeJudge([])
        with patch.dict("os.environ", {"DEEPSEEK_MODEL": "different-model", "DEEPSEEK_API_KEY": ""}):
            result = process_section_knowledge("A", self.unit.section_id, extraction_capability=extractor, judge_capability=judge, **self.configs)
        self.assertEqual(result.operation_rounds_used, 0)
        self.assertEqual((extractor.calls, judge.calls), ([], []))

    def test_historical_feedback_ids_are_exact_and_validation_does_not_expand_untrusted_counts(self):
        from league_knowledge_platform.section_knowledge import validate_judge_references
        valid = evaluation('FAITHFULNESS')
        validate_judge_references(valid, self.unit, item_count=10 ** 100)
        invalid = evaluation('FAITHFULNESS', item_ids=('item-0000001',))
        with self.assertRaises(ValueError):
            validate_judge_references(invalid, self.unit, item_count=10)

    def test_zero_checkpoint_is_durable_and_distinct_from_missing_or_failed(self):
        result = process_section_knowledge("A", self.unit.section_id, extraction_capability=FakeExtractor([response()]),
                                           judge_capability=FakeJudge([evaluation()]), **self.configs)
        self.assertEqual(result.checkpoint.accepted_proposal.items, ())
        d = json.loads(self.path.read_bytes())
        self.assertEqual(d["accepted_proposal"]["outcome"], "NO_KNOWLEDGE_PROPOSED")
        self.assertEqual(d["semantic_rounds_used"], 1)
        self.assertEqual(len(d["rounds"][0]["gates"]), 5)
        self.assertEqual(process_section_knowledge("A", self.unit.section_id, **self.configs).checkpoint, result.checkpoint)

    def test_corrupt_unsupported_and_changed_provenance_never_overwrite_or_call_providers(self):
        self.process()
        original = self.path.read_bytes()
        alterations = []
        for key, value in (("schema_version", 9), ("validation_contract_version", 2), ("semantic_rounds_used", 3), ("section_id", "section-000002")):
            d = json.loads(original); d[key] = value; alterations.append(d)
        d = json.loads(original); d["provenance"]["silver"]["sha256"] = "f" * 64; alterations.append(d)
        d = json.loads(original); d["rounds"][0]["decision"] = "REJECT"; alterations.append(d)
        d = json.loads(original); d["accepted_proposal"]["items"][0]["statement"] = "Tampered"; alterations.append(d)
        alterations.extend((b"{", b'\xff', b'{"schema_version":1,"schema_version":1}'))
        for value in alterations:
            content = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.path.write_bytes(content)
            judge = FakeJudge([])
            with self.subTest(value=type(value)), self.assertRaises((SectionKnowledgeContractError, SectionValidationIntegrityError)):
                process_section_knowledge("A", self.unit.section_id, judge_capability=judge, **self.configs)
            self.assertEqual(self.path.read_bytes(), content)
            self.assertEqual(judge.calls, [])

    def test_different_exact_upstream_hash_even_same_semantics_conflicts(self):
        self.process()
        from league_knowledge_platform import canonical_segmentation_path
        segmented_path = canonical_segmentation_path("A", self.configs["segmentation_config"])
        segmented_path.write_bytes(json.dumps(json.loads(segmented_path.read_bytes())).encode())
        before = self.path.read_bytes()
        with self.assertRaises(SectionValidationIntegrityError):
            process_section_knowledge("A", self.unit.section_id, **self.configs)
        self.assertEqual(self.path.read_bytes(), before)

    def test_publication_faults_never_return_success_or_partial_canonical_output(self):
        for boundary in ("prepare_directory", "stage", "publish"):
            with self.subTest(boundary=boundary), patch.object(fs, boundary, side_effect=OSError("fault")):
                with self.assertRaises(SectionKnowledgeFilesystemError) as caught:
                    self.process()
                self.assertEqual(caught.exception.semantic_rounds_used, 1)
                self.assertFalse(self.path.exists())
        with patch("league_knowledge_platform.section_knowledge.checkpoint_document", side_effect=ValueError("serialization")):
            with self.assertRaises(SectionKnowledgeFilesystemError):
                self.process()
        self.assertFalse(self.path.exists())

    def test_failure_after_atomic_publication_reuses_complete_artifact_without_calls(self):
        with patch.object(fs, "confirm_durable", side_effect=OSError("flush")):
            with self.assertRaises(SectionKnowledgeFilesystemError):
                self.process()
        self.assertTrue(self.path.exists())
        before = self.path.read_bytes()
        result = process_section_knowledge("A", self.unit.section_id, **self.configs)
        self.assertEqual(result.operation_rounds_used, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_race_first_valid_result_wins_and_no_operation_cleans_another_staging_file(self):
        barrier = Barrier(2, action=lambda: self.assertEqual(len(list(self.path.parent.glob('.section-knowledge-tmp-*'))), 2))
        original = fs.publish
        def publish(temporary, final):
            barrier.wait(timeout=10)
            return original(temporary, final)
        # Both staging files must coexist before either publication.
        with patch.object(fs, "publish", side_effect=publish), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.process, statement=s) for s in ("Valid A", "Different valid B")]
            results = [f.result(timeout=20) for f in futures]
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertEqual(results[0].checkpoint, results[1].checkpoint)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_appearing_incompatible_race_winner_is_not_replaced(self):
        original = fs.publish
        def publish(temporary, final):
            final.write_bytes(b"corrupt winner")
            return original(temporary, final)
        with patch.object(fs, "publish", side_effect=publish), self.assertRaises(SectionKnowledgeContractError):
            self.process()
        self.assertEqual(self.path.read_bytes(), b"corrupt winner")

    def test_read_only_loading_does_not_flush_or_mutate_any_artifact(self):
        self.process()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(fs, "confirm_durable", side_effect=AssertionError):
            load_validated_section_knowledge("A", self.unit.section_id, silver_config=self.configs["silver_config"],
                                             segmentation_config=self.configs["segmentation_config"], config=self.configs["config"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_crash_before_during_after_publication_and_ignored_orphan_staging(self):
        script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from league_knowledge_platform import process_section_knowledge, BronzeConfig, SilverConfig, SegmentationConfig, SectionValidationConfig
from league_knowledge_platform import _bronze_filesystem as fs
from section_validation_fixtures import FakeExtractor, FakeJudge, response, evaluation
root, boundary = Path(sys.argv[1]), sys.argv[2]
original = fs.publish
if boundary == 'during_write':
    def stage(directory, encoded, content, **kwargs):
        (directory / ('.section-knowledge-tmp-' + encoded + '-' + 'a'*32 + '.tmp')).write_bytes(content[:30])
        os._exit(71)
    fs.stage = stage
elif boundary == 'before_publish':
    fs.publish = lambda *args: os._exit(71)
elif boundary == 'after_publish':
    def publish(*args):
        original(*args)
        os._exit(71)
    fs.publish = publish
process_section_knowledge('A', 'section-000001', bronze_config=BronzeConfig(root/'bronze'),
    silver_config=SilverConfig(root/'silver'), segmentation_config=SegmentationConfig(root/'sections'),
    config=SectionValidationConfig(root/'validated'),
    extraction_capability=FakeExtractor([] if boundary == 'reuse' else [response('Valid knowledge')]),
    judge_capability=FakeJudge([] if boundary == 'reuse' else [evaluation()]))
'''
        for boundary in ("during_write", "before_publish", "after_publish"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                configs, unit = workspace(directory)
                path = canonical_section_knowledge_path("A", unit.section_id, configs["config"])
                crashed = subprocess.run([sys.executable, '-c', script, directory, boundary], capture_output=True, timeout=30)
                self.assertEqual(crashed.returncode, 71, crashed.stderr.decode(errors='replace'))
                self.assertEqual(path.exists(), boundary == 'after_publish')
                before = path.read_bytes() if path.exists() else None
                retry = subprocess.run([sys.executable, '-c', script, directory, 'reuse' if before else 'retry'], capture_output=True, timeout=30)
                self.assertEqual(retry.returncode, 0, retry.stderr.decode(errors='replace'))
                loaded = decode_section_checkpoint(path.read_bytes(), unit)
                self.assertEqual(loaded.semantic_rounds_used, 1)
                if before:
                    self.assertEqual(path.read_bytes(), before)
                # Orphan temp files can remain, but never become accepted input or get swept unsafely.
                self.assertTrue(all(p.name.endswith('.tmp') for p in path.parent.iterdir() if p != path))


if __name__ == "__main__":
    unittest.main()
