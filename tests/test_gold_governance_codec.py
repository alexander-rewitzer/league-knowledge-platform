"""Strict transient operation interchange; no canonical operation persistence."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest

from league_knowledge_platform import (
    GoldGovernanceError, GoldNoChangePayload, GoldProvenance, GoldProvenanceAddition,
    decode_gold_operation, encode_gold_operation, gold_operation_document,
)
from test_gold_models import identity, reference
from test_gold_governance_models import SUPPORT, examples, operation


class GoldGovernanceCodecTests(unittest.TestCase):
    def test_all_operations_roundtrip_without_context_or_storage(self):
        for op in examples():
            with self.subTest(kind=op.kind):
                encoded = encode_gold_operation(op)
                self.assertEqual(decode_gold_operation(encoded), op)
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertNotIn(b"\r\n", encoded)
                self.assertEqual(json.loads(encoded), gold_operation_document(op))

    def test_permutations_have_equal_bytes_without_silently_deduplicating(self):
        other = GoldProvenance(reference("B"), "source-item-000002")
        adds = (GoldProvenanceAddition(identity(), SUPPORT), GoldProvenanceAddition(identity(2), other))
        a = operation(GoldNoChangePayload((identity(), identity(2)), adds), refs=(SUPPORT, other))
        b = replace(a, source_refs=a.source_refs[::-1], payload=GoldNoChangePayload(
            a.payload.target_knowledge_ids[::-1], adds[::-1]))
        self.assertEqual(encode_gold_operation(a), encode_gold_operation(b))
        value = gold_operation_document(a)
        value["source_refs"] *= 2
        with self.assertRaises(GoldGovernanceError):
            decode_gold_operation(json.dumps(value).encode())

    def test_content_unicode_and_significant_whitespace_remain_exact(self):
        op = examples()[1]
        text = "  Cafe\u0301 条件\n\r\tExact content.  "
        value = replace(op, payload=replace(op.payload,
            new_unit=replace(op.payload.new_unit, unit=replace(op.payload.new_unit.unit, content=text))))
        self.assertEqual(decode_gold_operation(encode_gold_operation(value)), value)
        self.assertEqual(decode_gold_operation(encode_gold_operation(value)).payload.new_unit.unit.content, text)

    def test_unknown_missing_fields_are_rejected_at_every_nested_boundary(self):
        original = gold_operation_document(examples()[1])
        paths = ((), ("payload",), ("payload", "new_unit"), ("payload", "new_unit", "placement"),
                 ("payload", "new_unit", "unit"), ("source_refs", 0),
                 ("source_refs", 0, "source_knowledge_artifact"))
        for path in paths:
            for mode in ("extra", "missing"):
                value = deepcopy(original)
                node = value
                for part in path:
                    node = node[part]
                if mode == "extra":
                    node["winner"] = "untrusted"
                else:
                    del node[next(iter(node))]
                with self.subTest(path=path, mode=mode), self.assertRaises(GoldGovernanceError):
                    decode_gold_operation(json.dumps(value).encode())

    def test_unsupported_versions_fail_before_interpreting_payload(self):
        for key in ("operation_schema_version", "governance_contract_version"):
            for invalid in (0, 2, True, 1.0, "1", None):
                value = gold_operation_document(operation())
                value[key] = invalid
                value["payload"] = "future schema"
                with self.subTest(key=key, value=invalid), self.assertRaises(GoldGovernanceError) as caught:
                    decode_gold_operation(json.dumps(value).encode())
                self.assertEqual(caught.exception.code, "VERSION")

    def test_malformed_json_duplicate_keys_nonfinite_and_invalid_encoding(self):
        raw = encode_gold_operation(operation())
        duplicate = raw.replace(b'"operation_id": "op-1"', b'"operation_id":"one","operation_id":"two"')
        for invalid in (b"{", b"null", b"[]", b"\xff", None, "{}", duplicate,
                        raw.replace(b'"rationale": "Synthetic integration rationale."', b'"rationale":NaN'),
                        b"[" * 1500 + b"]" * 1500):
            with self.subTest(invalid=type(invalid)), self.assertRaises(GoldGovernanceError):
                decode_gold_operation(invalid)

    def test_unknown_operations_delete_aliases_and_relation_kinds_are_rejected(self):
        for invalid in ("DELETE", "DROP", "REMOVE", "PURGE", "unknown", 1, None, {}):
            value = gold_operation_document(operation())
            value["kind"] = invalid
            with self.subTest(kind=invalid), self.assertRaises(GoldGovernanceError):
                decode_gold_operation(json.dumps(value).encode())
        value = gold_operation_document(examples()[5])
        value["payload"]["relation"]["relation"]["kind"] = "supports"
        with self.assertRaises(GoldGovernanceError):
            decode_gold_operation(json.dumps(value).encode())

    def test_no_change_and_conflict_cannot_carry_writer_or_winner_fields(self):
        for op in (operation(), examples()[-1]):
            for field in ("event_type", "integration_id", "winner", "delete", "resulting_content", "file_path"):
                value = gold_operation_document(op)
                value["payload"][field] = "not allowed"
                with self.subTest(kind=op.kind, field=field), self.assertRaises(GoldGovernanceError):
                    decode_gold_operation(json.dumps(value).encode())

    def test_invalid_array_shapes_and_payload_kind_mismatch(self):
        for invalid in (None, {}, "ids", 1):
            value = gold_operation_document(operation())
            value["payload"]["target_knowledge_ids"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(GoldGovernanceError):
                decode_gold_operation(json.dumps(value).encode())
        value = gold_operation_document(examples()[1])
        value["kind"] = "LINK"
        with self.assertRaises(GoldGovernanceError):
            decode_gold_operation(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
