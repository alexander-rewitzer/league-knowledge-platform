"""Canonical source compatibility, owned staging, races and fresh-process crashes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    SourceAuditInputError, SourceAuditIntegrityError, SourceKnowledgeContractError, SourceKnowledgeFilesystemError,
    canonical_section_knowledge_path, canonical_source_knowledge_path, load_source_knowledge, process_source_knowledge,
)
from league_knowledge_platform import _bronze_filesystem as fs
from league_knowledge_platform.source_knowledge import decode_source_artifact
from source_audit_fixtures import FakeAudit, FakeValidator, audit_response, validator_evaluation, workspace


class SourceKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.configs, self.inputs = workspace(self.temp.name)
        self.path = canonical_source_knowledge_path('A', self.configs['config'])

    def process(self, statement='Consolidated faithful statement', agent=None, validator=None):
        return process_source_knowledge('A', audit_capability=agent or FakeAudit([audit_response((statement, ('input-000001', 'input-000002')))]),
            validator_capability=validator or FakeValidator([validator_evaluation()]), **self.configs)

    def test_roundtrip_and_credential_free_provider_free_reuse_after_restart(self):
        created = self.process(); before = self.path.read_bytes()
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': '', 'DEEPSEEK_MODEL': 'changed'}):
            reused = process_source_knowledge('A', **self.configs)
        self.assertEqual(reused.artifact, created.artifact)
        self.assertEqual(reused.operation_rounds_used, 0)
        self.assertFalse(reused.created)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(decode_source_artifact(before, self.inputs), created.artifact)
        with patch.object(fs, 'confirm_durable', side_effect=AssertionError('Read-only loader')):
            self.assertEqual(load_source_knowledge('A', **self.configs), created.artifact)

    def test_recorded_model_prompt_versions_do_not_determine_compatibility(self):
        response = audit_response()
        response = replace(response, metadata=replace(response.metadata, requested_model='historic-model', prompt_version='historic-audit-prompt'))
        evaluation = validator_evaluation()
        evaluation = replace(evaluation, metadata=replace(evaluation.metadata, prompt_version='different-validator-prompt'))
        result = self.process(agent=FakeAudit([response]), validator=FakeValidator([evaluation]))
        before = self.path.read_bytes()
        reused = process_source_knowledge('A', **self.configs)
        self.assertEqual(reused.artifact, result.artifact)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(reused.operation_rounds_used, 0)

    def test_empty_canonical_is_durable_and_reusable_without_agent_or_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            result = process_source_knowledge('A', validator_capability=FakeValidator([validator_evaluation()]), **configs)
            d = json.loads(result.path.read_bytes())
            self.assertEqual((d['outcome'], d['items'], d['conflicts']), ('NO_SOURCE_KNOWLEDGE', [], []))
            self.assertEqual(len(d['validated_sections']), 2)
            self.assertEqual(d['rounds'][0]['generation'], 'DETERMINISTIC_EMPTY')
            self.assertIsNone(d['audit_metadata'])
            self.assertEqual(process_source_knowledge('A', **configs).operation_rounds_used, 0)

    def test_corrupt_unsupported_tampered_provenance_items_and_histories_never_overwritten(self):
        self.process(); original = self.path.read_bytes()
        values = [b'{', b'\xff', b'{"schema_version":1,"schema_version":1}']
        for field, value in (('schema_version', 2), ('source_audit_contract_version', 2), ('rounds_used', True),
                             ('outcome', 'NO_SOURCE_KNOWLEDGE'), ('created_at_utc', 'bad')):
            d = json.loads(original); d[field] = value; values.append(json.dumps(d).encode())
        for field in ('sha256', 'relative_path'):
            d = json.loads(original); d['validated_sections'][0][field] = 'wrong'; values.append(json.dumps(d).encode())
        d = json.loads(original); d['validated_sections'].pop(); values.append(json.dumps(d).encode())
        d = json.loads(original); d['items'][0]['statement'] = 'Tampered'; values.append(json.dumps(d).encode())
        d = json.loads(original); d['items'][0]['derived_from'].pop(); values.append(json.dumps(d).encode())
        d = json.loads(original); d['rounds'][0]['decision'] = 'REJECT'; values.append(json.dumps(d).encode())
        d = json.loads(original); d['rounds'][0]['number'] = True; values.append(json.dumps(d).encode())
        d = json.loads(original); d['rounds'][0]['generation'] = 'DETERMINISTIC_EMPTY'; values.append(json.dumps(d).encode())
        d = json.loads(original); d['provenance']['silver']['sha256'] = 'f' * 64; values.append(json.dumps(d).encode())
        for value in values:
            self.path.write_bytes(value)
            agent, validator = FakeAudit([]), FakeValidator([])
            with self.subTest(size=len(value)), self.assertRaises((SourceAuditIntegrityError, SourceKnowledgeContractError)):
                process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **self.configs)
            self.assertEqual((agent.calls, validator.calls), ([], []))
            self.assertEqual(self.path.read_bytes(), value)

    def test_changed_exact_checkpoint_bytes_invalidate_canonical_even_same_knowledge(self):
        self.process(); before = self.path.read_bytes()
        path = canonical_section_knowledge_path('A', 'section-000001', self.configs['section_validation_config'])
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(SourceAuditIntegrityError): process_source_knowledge('A', **self.configs)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_required_checkpoint_prevents_reuse_of_otherwise_valid_artifact(self):
        self.process(); before = self.path.read_bytes()
        canonical_section_knowledge_path('A', 'section-000001', self.configs['section_validation_config']).unlink()
        with self.assertRaises(SourceAuditInputError): process_source_knowledge('A', **self.configs)
        self.assertEqual(self.path.read_bytes(), before)

    def test_upstream_changes_during_inference_fail_before_publication(self):
        path = canonical_section_knowledge_path('A', 'section-000001', self.configs['section_validation_config'])
        def mutate(request):
            path.write_bytes(path.read_bytes() + b'\n')
            return audit_response()
        with self.assertRaises(SourceAuditIntegrityError): self.process(agent=FakeAudit([mutate]))
        self.assertFalse(self.path.exists())

    def test_owned_temporary_files_and_first_compatible_race_winner(self):
        barrier = Barrier(2, action=lambda: self.assertEqual(len(list(self.path.parent.glob('.source-knowledge-tmp-*'))), 2))
        original = fs.publish
        def publish(temporary, final):
            barrier.wait(timeout=10)
            return original(temporary, final)
        with patch.object(fs, 'publish', side_effect=publish), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.process, statement='Valid A'), executor.submit(self.process, statement='Valid B')]
            results, errors = [], []
            for future in futures:
                try:
                    results.append(future.result(timeout=20))
                except Exception as error:
                    errors.append(repr(error))
            self.assertEqual(errors, [])
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertEqual(results[0].artifact, results[1].artifact)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_appearing_corrupt_winner_is_never_overwritten(self):
        original = fs.publish
        def publish(temporary, final):
            final.write_bytes(b'corrupt winner')
            return original(temporary, final)
        with patch.object(fs, 'publish', side_effect=publish), self.assertRaises(SourceKnowledgeContractError): self.process()
        self.assertEqual(self.path.read_bytes(), b'corrupt winner')

    def test_equivalent_windows_extended_path_spelling_during_directory_creation(self):
        import os
        if os.name != 'nt':
            self.process()
            return
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.path.parent:
                return Path('\\\\?\\' + str(result))
            return result
        with patch.object(Path, 'resolve', resolve):
            self.assertTrue(self.process().created)
        self.assertEqual(process_source_knowledge('A', **self.configs).operation_rounds_used, 0)

    def test_publication_stage_serialization_and_flush_failures_are_explicit(self):
        for boundary in ('prepare_directory', 'stage', 'publish'):
            with self.subTest(boundary=boundary), patch.object(fs, boundary, side_effect=OSError('fault')):
                with self.assertRaises(SourceKnowledgeFilesystemError) as caught: self.process()
                self.assertEqual(caught.exception.semantic_rounds_used, 1)
                self.assertFalse(self.path.exists())
        with patch('league_knowledge_platform.source_knowledge.source_artifact_document', side_effect=ValueError('serialize')):
            with self.assertRaises(SourceKnowledgeFilesystemError): self.process()
        with patch.object(fs, 'confirm_durable', side_effect=OSError('flush')):
            with self.assertRaises(SourceKnowledgeFilesystemError): self.process()
        self.assertTrue(self.path.exists())
        self.assertEqual(process_source_knowledge('A', **self.configs).operation_rounds_used, 0)

    def test_fresh_process_crashes_during_stage_before_and_after_publication(self):
        script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
from league_knowledge_platform import (SourceAuditConfig, SectionValidationConfig, SegmentationConfig, SilverConfig, process_source_knowledge)
from league_knowledge_platform import _bronze_filesystem as fs
from source_audit_fixtures import FakeAudit, FakeValidator, audit_response, validator_evaluation
root, boundary = Path(sys.argv[1]), sys.argv[2]
original = fs.publish
if boundary == 'during_write':
    def stage(directory, encoded, content, **kwargs):
        (directory/('.source-knowledge-tmp-'+encoded+'-'+'a'*32+'.tmp')).write_bytes(content[:20])
        os._exit(72)
    fs.stage = stage
elif boundary == 'before_publish':
    fs.publish = lambda *args: os._exit(72)
elif boundary == 'after_publish':
    def publish(*args):
        original(*args)
        os._exit(72)
    fs.publish = publish
process_source_knowledge('A', silver_config=SilverConfig(root/'silver'), segmentation_config=SegmentationConfig(root/'sections'),
    section_validation_config=SectionValidationConfig(root/'validated'), config=SourceAuditConfig(root/'source-knowledge'),
    audit_capability=FakeAudit([] if boundary == 'reuse' else [audit_response()]),
    validator_capability=FakeValidator([] if boundary == 'reuse' else [validator_evaluation()]))
'''
        for boundary in ('during_write', 'before_publish', 'after_publish'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                configs, inputs = workspace(directory)
                path = canonical_source_knowledge_path('A', configs['config'])
                crashed = subprocess.run([sys.executable, '-c', script, directory, boundary], capture_output=True, timeout=30)
                self.assertEqual(crashed.returncode, 72, crashed.stderr.decode(errors='replace'))
                self.assertEqual(path.exists(), boundary == 'after_publish')
                before = path.read_bytes() if path.exists() else None
                retried = subprocess.run([sys.executable, '-c', script, directory, 'reuse' if before else 'retry'], capture_output=True, timeout=30)
                self.assertEqual(retried.returncode, 0, retried.stderr.decode(errors='replace'))
                self.assertEqual(decode_source_artifact(path.read_bytes(), inputs).rounds_used, 1)
                if before: self.assertEqual(path.read_bytes(), before)
                self.assertTrue(all(p.suffix == '.tmp' for p in path.parent.iterdir() if p != path))


if __name__ == '__main__':
    unittest.main()
