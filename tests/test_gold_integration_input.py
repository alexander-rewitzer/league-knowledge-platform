"""Real canonical upstream admission and zero-publication end-to-end preparation."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldSnapshot, IntegrationOperationalError, canonical_silver_path, 
    initialize_gold_store, load_gold_snapshot, prepare_gold_integration, process_source_knowledge,
)
from league_knowledge_platform.gold_integration_input import load_integration_source
from gold_integration_fixtures import FakeAgent, FakeValidator, real_create
from test_gold_provenance import source_workspace
from source_audit_fixtures import workspace, FakeValidator as SourceValidator, validator_evaluation


class InputIntegrationTests(unittest.TestCase):
    def test_real_source_and_empty_gold_to_prepared_create_retains_exact_lineage_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            configs,path,_=source_workspace(Path(directory)/'source','B')
            gold=GoldConfig(Path(directory)/'gold');ref=initialize_gold_store(gold)
            before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in Path(directory).rglob('*') if p.is_file()}
            agent,validator=FakeAgent([real_create]),FakeValidator()
            with patch('socket.create_connection',side_effect=AssertionError),patch('sqlite3.connect',side_effect=AssertionError):
                result=prepare_gold_integration('B',agent=agent,validator=validator,gold_config=gold,**configs)
            self.assertEqual(result.outcome,'PREPARED_CHANGE')
            self.assertEqual(result.base_reference,ref)
            self.assertEqual(load_gold_snapshot(gold).snapshot,GoldSnapshot())
            self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in Path(directory).rglob('*') if p.is_file()})
            self.assertTrue(all(p.source_knowledge_artifact==result.source_reference
                for u in result.candidate_snapshot.documents[0].units for p in u.provenance))
            self.assertEqual(result.source_reference.sha256,load_integration_source('B',**configs).reference.sha256)

    def test_real_canonical_empty_source_deterministic_fast_path(self):
        with tempfile.TemporaryDirectory() as directory:
            configs,_=workspace(Path(directory)/'source',empty=True)
            process_source_knowledge('A',validator_capability=SourceValidator([validator_evaluation()]),**configs)
            gold=GoldConfig(Path(directory)/'gold');initialize_gold_store(gold)
            result=prepare_gold_integration('A',gold_config=gold,**configs)
            self.assertEqual(result.outcome,'NO_SOURCE_KNOWLEDGE')
            self.assertEqual(result.candidate_snapshot,GoldSnapshot())
            self.assertEqual(result.source_item_ids,())
            self.assertEqual(result.effects.units,())

    def test_canonical_corrupt_or_unsupported_source_fails_before_semantic_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            configs,path,_=source_workspace(Path(directory)/'source','B')
            original=path.read_bytes();gold=GoldConfig(Path(directory)/'gold');initialize_gold_store(gold)
            for content in (b'{broken',original.replace(b'"schema_version": 1',b'"schema_version": 99',1),
                            original.replace(b'"KNOWLEDGE_AVAILABLE"',b'"NO_SOURCE_KNOWLEDGE"')):
                path.write_bytes(content)
                agent=FakeAgent([real_create])
                with self.subTest(content=content[:15]),self.assertRaises(IntegrationOperationalError):
                    prepare_gold_integration('B',agent=agent,validator=FakeValidator(),gold_config=gold,**configs)
                self.assertEqual(agent.calls,[])
            path.write_bytes(original)

    def test_upstream_changed_after_source_audit_is_not_silently_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            configs,path,bronze_path=source_workspace(Path(directory)/'source','B')
            gold=GoldConfig(Path(directory)/'gold');initialize_gold_store(gold)
            silver=canonical_silver_path('B',configs['silver_config'])
            silver.write_bytes(silver.read_bytes()+b' ')
            with self.assertRaises(IntegrationOperationalError):prepare_gold_integration('B',gold_config=gold,**configs)

