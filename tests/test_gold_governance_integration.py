"""Interoperability with canonical source readers and Gold without integration."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldCreatePayload, GoldGovernanceError, GoldIntegrationOperation, GoldNewUnitIntent,
    GoldOperationKind, GoldProvenance, GoldSnapshot, SourceKnowledgeOutcome,
    encode_gold_operation, decode_gold_operation, validate_gold_operation,
)
from gold_document_fixtures import layout, topic
from test_gold_models import unit
from test_gold_governance_models import placement
from test_gold_provenance import source_workspace, verified_context
from source_audit_fixtures import FakeValidator, validator_evaluation, workspace
from league_knowledge_platform import process_source_knowledge


class GoldGovernanceIntegrationTests(unittest.TestCase):
    def test_real_source_artifact_identity_hash_and_item_references_remain_read_only(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.create_connection", side_effect=AssertionError):
            configs, path, _ = source_workspace(directory, "A")
            source, ref, members = verified_context("A", configs, path)
            provenance = GoldProvenance(ref, source.candidate.items[0].item_id)
            new = GoldNewUnitIntent(unit(3, provenance=(provenance,)), placement(0))
            op = GoldIntegrationOperation("create-1", GoldOperationKind.CREATE, (provenance,),
                                          "New source-grounded knowledge.", GoldCreatePayload(new))
            before = {p: p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()}
            context = dict(base_snapshot=GoldSnapshot(), incoming_artifact=ref,
                incoming_item_ids=members[ref], proposed_layout=(layout(topics=(topic(ids=()),)),))
            self.assertEqual(validate_gold_operation(decode_gold_operation(encode_gold_operation(op)), **context), op)
            for invalid_ref in (replace(ref, sha256="f" * 64), replace(ref, relative_path="different.json")):
                with self.assertRaises(GoldGovernanceError):
                    validate_gold_operation(op, **{**context, "incoming_artifact": invalid_ref})
            self.assertEqual(before, {p: p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()})

    def test_canonical_empty_source_does_not_require_fake_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            result = process_source_knowledge("A", validator_capability=FakeValidator([validator_evaluation()]), **configs)
            source, ref, members = verified_context("A", configs, result.path)
            self.assertEqual(source.outcome, SourceKnowledgeOutcome.NO_SOURCE_KNOWLEDGE)
            self.assertEqual(members[ref], frozenset())
            # Empty-source success belongs downstream, not to a fabricated eighth operation.
            fake = GoldProvenance(ref, "source-item-000001")
            op = GoldIntegrationOperation("fake", GoldOperationKind.CREATE, (fake,), "Not legitimate.",
                GoldCreatePayload(GoldNewUnitIntent(unit(3, provenance=(fake,)), placement(0))))
            with self.assertRaises(GoldGovernanceError):
                validate_gold_operation(op, base_snapshot=GoldSnapshot(), incoming_artifact=ref,
                    incoming_item_ids=members[ref], proposed_layout=(layout(topics=(topic(ids=()),)),))


if __name__ == "__main__":
    unittest.main()
