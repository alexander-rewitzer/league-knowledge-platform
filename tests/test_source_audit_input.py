"""Complete authoritative checkpoint admission and read-only input snapshots."""

from dataclasses import replace
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    SourceAuditInputError, SourceAuditIntegrityError, canonical_section_knowledge_path,
    canonical_segmentation_path, canonical_silver_path, load_source_audit_input, process_source_knowledge,
)
from league_knowledge_platform.source_audit_input import resolved_source_path, verify_source_snapshot
from source_audit_fixtures import FakeAudit, FakeValidator, workspace


class SourceAuditInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.configs, self.inputs = workspace(self.temp.name)
        self.loading = {k: v for k, v in self.configs.items() if k != "config"}

    def test_complete_ordered_content_set_and_distinct_same_local_item_ids(self):
        self.assertEqual([s.section_id for s in self.inputs.sections], ["section-000001", "section-000002"])
        self.assertEqual([i.token for i in self.inputs.items], ["input-000001", "input-000002"])
        self.assertEqual([i.reference.item_id for i in self.inputs.items], ["item-000001", "item-000001"])
        self.assertEqual(len({i.reference for i in self.inputs.items}), 2)
        self.assertEqual(len(self.inputs.snapshots), 4)
        self.assertNotIn("AD_ONLY", repr(self.inputs))
        self.assertTrue(all(s.provenance == self.inputs.provenance for s in self.inputs.sections))

    def test_missing_corrupt_unsupported_unvalidated_required_checkpoint_fails_before_providers(self):
        path = canonical_section_knowledge_path("A", "section-000002", self.configs["section_validation_config"])
        original = path.read_bytes()
        variants = [None, b"{", b'{"schema_version":1,"schema_version":1}']
        for field, value in (("schema_version", 2), ("validation_contract_version", 2), ("section_id", "section-000003")):
            d = json.loads(original); d[field] = value; variants.append(json.dumps(d).encode())
        d = json.loads(original); d["rounds"][-1]["gates"]["COMPLETENESS"]["verdict"] = "FAIL"; variants.append(json.dumps(d).encode())
        d = json.loads(original); d["provenance"]["silver"]["sha256"] = "f" * 64; variants.append(json.dumps(d).encode())
        for content in variants:
            if path.exists(): path.unlink()
            if content is not None: path.write_bytes(content)
            agent, validator = FakeAudit([]), FakeValidator([])
            with self.subTest(content=content is None), self.assertRaises(SourceAuditInputError):
                process_source_knowledge("A", audit_capability=agent, validator_capability=validator, **self.configs)
            self.assertEqual((agent.calls, validator.calls), ([], []))
            self.assertFalse(Path(self.configs["config"].root).exists())
        path.write_bytes(original)

    def test_snapshot_detects_reserialization_or_removal_without_writes(self):
        path = canonical_silver_path("A", self.configs["silver_config"])
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaises(SourceAuditIntegrityError): verify_source_snapshot(self.inputs)
        path.unlink()
        with self.assertRaises(SourceAuditIntegrityError): verify_source_snapshot(self.inputs)

    def test_loading_never_calls_provider_flush_catalog_or_upstream_processing(self):
        from league_knowledge_platform import _bronze_filesystem as fs
        before = {p: p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        with patch.object(fs, 'confirm_durable', side_effect=AssertionError), patch('sqlite3.connect', side_effect=AssertionError), \
                patch('league_knowledge_platform.section_validation.process_section_knowledge', side_effect=AssertionError):
            loaded = load_source_audit_input("A", **self.loading)
        self.assertEqual(loaded, self.inputs)
        self.assertEqual(before, {p: p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()})

    def test_advertisement_requires_no_checkpoint_and_empty_state_is_explicit(self):
        for upstream in ('ads', 'empty'):
            with self.subTest(upstream=upstream), tempfile.TemporaryDirectory() as directory:
                configs, inputs = workspace(directory, upstream=upstream)
                self.assertEqual((inputs.sections, inputs.items, inputs.validated_sections), ((), (), ()))
                self.assertEqual(inputs.upstream_segmentation_outcome, 'SEGMENTED' if upstream == 'ads' else 'NO_SEGMENTABLE_CONTENT')
                self.assertFalse(Path(configs['section_validation_config'].root).exists())

    def test_source_identity_and_changed_authoritative_segmentation_are_not_empty(self):
        path = canonical_segmentation_path('A', self.configs['segmentation_config'])
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(SourceAuditInputError): load_source_audit_input('A', **self.loading)
        with self.assertRaises(SourceAuditInputError): load_source_audit_input('B', **self.loading)

    def test_escaping_symlink_and_overlapping_output_root_rejected_before_calls(self):
        with self.assertRaises(SourceAuditIntegrityError):
            process_source_knowledge('A', **{**self.configs, 'config': replace(self.configs['config'], root=self.configs['silver_config'].root)})
        path = canonical_section_knowledge_path('A', 'section-000001', self.configs['section_validation_config'])
        original = path.read_bytes()
        outside = Path(self.temp.name) / 'outside.json'; outside.write_bytes(original)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            path.write_bytes(original)
            # The path guard is independently exercised even without Windows symlink privilege.
            with patch('pathlib.Path.is_symlink', return_value=True), self.assertRaises(SourceAuditIntegrityError):
                load_source_audit_input('A', **self.loading)
        else:
            with self.assertRaises(SourceAuditIntegrityError): load_source_audit_input('A', **self.loading)

    def test_real_escaping_output_directory_junction_is_rejected_before_providers(self):
        output = Path(self.configs['config'].root)
        outside = Path(self.temp.name) / 'outside'; outside.mkdir()
        output.mkdir()
        junction = output / 'youtube'
        if os.name == 'nt':
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True, timeout=15)
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors='replace'))
        else:
            junction.symlink_to(outside, target_is_directory=True)
        agent, validator = FakeAudit([]), FakeValidator([])
        with self.assertRaises(SourceAuditIntegrityError):
            process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **self.configs)
        self.assertEqual((agent.calls, validator.calls), ([], []))
        self.assertEqual(list(outside.iterdir()), [])

    def test_windows_drive_and_unc_spellings_normalize_after_resolving_links(self):
        if os.name != 'nt':
            self.assertEqual(resolved_source_path(self.temp.name), Path(self.temp.name).resolve())
            return
        for extended, normal in (('\\\\?\\C:\\example\\child', 'C:\\example\\child'),
                                 ('\\\\?\\UNC\\server\\share\\child', '\\\\server\\share\\child')):
            with patch.object(Path, 'resolve', return_value=Path(extended)):
                self.assertEqual(resolved_source_path('unused'), Path(normal))


if __name__ == '__main__':
    unittest.main()
