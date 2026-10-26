from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig, GoldContractState, GoldProvenance, GoldRelation, GoldRelationKind,
    GoldSnapshot, GoldStatus, initialize_gold_store, load_gold_snapshot, normalize_gold_structure,
    publish_gold_snapshot, stage_gold_snapshot, validate_gold_provenance, validate_gold_snapshot,
)
from gold_document_fixtures import layout, topic
from test_gold_models import identity, unit
from test_gold_provenance import source_workspace, verified_context


class GoldDocumentIntegrationTests(unittest.TestCase):
    def test_real_upstream_lineage_conflict_history_and_empty_container_pruning(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.create_connection", side_effect=AssertionError):
            source_config, path, _ = source_workspace(Path(directory) / "source", "A")
            source, reference, verified = verified_context("A", source_config, path)
            provenance = (GoldProvenance(reference, source.candidate.items[0].item_id),)
            a = unit(1, provenance=provenance,
                     relations=(GoldRelation(GoldRelationKind.CONTRADICTS, identity(2)),))
            b = unit(2, provenance=provenance)
            state = GoldContractState(1, 1, (a, b))
            snapshot = normalize_gold_structure(state, (layout(topics=(topic(ids=(1,)), topic(2, (2,), position=1))),))
            config = GoldConfig(Path(directory) / "gold")
            base = initialize_gold_store(config)
            stage = stage_gold_snapshot(snapshot, config=config, base=base)
            base = publish_gold_snapshot(stage, config=config, expected_base=base)
            before = {p: p.read_bytes() for p in (Path(directory) / "source").rglob("*") if p.is_file()}
            loaded = load_gold_snapshot(config)
            validate_gold_provenance(validate_gold_snapshot(loaded.snapshot), source_items_by_artifact=verified)
            # Simulate a caller-approved later mutation as explicit input data, not an Integration operation.
            historical = replace(a, status=GoldStatus.MERGED, merged_into=b.knowledge_id)
            next_state = GoldContractState(1, 1, (historical, b))
            next_snapshot = normalize_gold_structure(next_state, (
                layout(topics=(topic(ids=(1,)),)), layout(2, topics=(topic(2, (2,)),), domain="abilities")))
            stage = stage_gold_snapshot(next_snapshot, config=config, base=base)
            publish_gold_snapshot(stage, config=config, expected_base=base)
            result = load_gold_snapshot(config).snapshot
            self.assertEqual(len(result.documents), 1)
            self.assertEqual(result.documents[0].historical_units, (historical,))
            self.assertEqual(validate_gold_snapshot(result), next_state)
            self.assertEqual(before, {p: p.read_bytes() for p in (Path(directory) / "source").rglob("*") if p.is_file()})
            # An unrelated future event history is outside the Gold store and remains untouched.
            events = Path(directory) / "events.jsonl"
            events.write_bytes(b'{"synthetic":true}\n')
            load_gold_snapshot(config)
            self.assertEqual(events.read_bytes(), b'{"synthetic":true}\n')
