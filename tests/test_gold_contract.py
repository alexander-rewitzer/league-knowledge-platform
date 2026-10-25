"""Whole-state integrity and strict deterministic interchange without file persistence."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldContractError, GoldContractState, GoldDocumentVersion, GoldProvenance,
    GoldReferenceError, GoldRelation, GoldRelationKind, GoldStatus, GoldVersionError,
    decode_gold_document_version, decode_gold_state, decode_gold_unit_document,
    encode_gold_state, get_gold_unit, gold_document_version_document,
    gold_state_document, gold_unit_document, resolve_knowledge_id, validate_gold_state,
)
from test_gold_models import identity, reference, unit


class GoldContractTests(unittest.TestCase):
    def test_empty_gold_is_valid_roundtrippable_and_needs_no_seed_or_configuration(self):
        empty = GoldContractState(1, 1)
        self.assertEqual(validate_gold_state(empty), empty)
        self.assertEqual(decode_gold_state(encode_gold_state(empty)), empty)
        self.assertEqual(gold_state_document(empty), {"schema_version": 1, "contract_version": 1, "units": []})

    def test_duplicate_ids_fail_even_when_different_status_content_or_document_origin(self):
        for other in (unit(), unit(content="Different"), unit(status=GoldStatus.MERGED, merged_into=identity(2))):
            with self.subTest(other=other), self.assertRaises(GoldContractError):
                GoldContractState(1, 1, (unit(), other, unit(2)))
        # A decoder rejects duplicates too, rather than silently building a dict.
        value = gold_state_document(GoldContractState(1, 1, (unit(),)))
        value["units"] *= 2
        with self.assertRaises(GoldContractError):
            decode_gold_state(json.dumps(value).encode())

    def test_chained_merge_lookup_is_historical_resolution_is_active_without_mutation(self):
        oldest = unit(1, status=GoldStatus.MERGED, merged_into=identity(2))
        previous = unit(2, status=GoldStatus.MERGED, merged_into=identity(3))
        active = unit(3)
        relation = GoldRelation(GoldRelationKind.CONTRADICTS, oldest.knowledge_id)
        observer = unit(4, content="Conflicting guidance", relations=(relation,))
        state = GoldContractState(1, 1, (oldest, previous, active, observer))
        before = encode_gold_state(state)
        self.assertEqual(get_gold_unit(state, identity(1)), oldest)
        self.assertEqual(resolve_knowledge_id(state, identity(1)), active.knowledge_id)
        self.assertEqual(get_gold_unit(state, identity(1)).status, GoldStatus.MERGED)
        self.assertEqual(get_gold_unit(state, identity(3)).status, GoldStatus.ACTIVE)
        self.assertEqual(observer.relations, (relation,))
        self.assertEqual(encode_gold_state(state), before)
        with self.assertRaises(GoldReferenceError):
            get_gold_unit(state, identity(99))
        with self.assertRaises(GoldReferenceError):
            resolve_knowledge_id(state, identity(99))

    def test_missing_survivor_and_short_or_long_cycles_fail_explicitly(self):
        cases = [
            (unit(status=GoldStatus.MERGED, merged_into=identity(99)),),
            tuple(unit(n, status=GoldStatus.MERGED, merged_into=identity(3-n)) for n in (1, 2)),
            tuple(unit(n, status=GoldStatus.MERGED, merged_into=identity(n+1 if n<4 else 2)) for n in range(1, 5)),
        ]
        for units in cases:
            state = GoldContractState(1, 1, units)
            with self.subTest(units=units), self.assertRaises(GoldReferenceError):
                validate_gold_state(state)
            with self.assertRaises(GoldReferenceError):
                encode_gold_state(state)

    def test_long_history_does_not_require_recursion_or_flatten_existing_links(self):
        count = 1600
        state = GoldContractState(1, 1, tuple(unit(n, status=GoldStatus.MERGED, merged_into=identity(n+1))
                                            for n in range(1, count)) + (unit(count),))
        self.assertEqual(resolve_knowledge_id(state, identity()), identity(count))
        self.assertEqual(state.units[0].merged_into, identity(2))

    def test_missing_relation_target_fails_only_at_complete_state_boundary(self):
        single = unit(relations=(GoldRelation(GoldRelationKind.RELATED_TO, identity(2)),))
        self.assertEqual(decode_gold_unit_document(gold_unit_document(single)), single)
        with self.assertRaises(GoldReferenceError):
            validate_gold_state(GoldContractState(1, 1, (single,)))
        validate_gold_state(GoldContractState(1, 1, (single, unit(2))))

    def test_explicit_contradiction_preserves_both_claims_and_provenance_without_mirror(self):
        a = unit(content="Wait under the stated condition.", relations=(GoldRelation(GoldRelationKind.CONTRADICTS, identity(2)),))
        b = unit(2, content="Do not wait under the same stated condition.",
                 provenance=(GoldProvenance(reference("B"), "source-item-000009"),))
        state = decode_gold_state(encode_gold_state(GoldContractState(1, 1, (a, b))))
        self.assertEqual(state.units, (a, b))
        self.assertEqual(b.relations, ())

    def test_collection_permutations_have_equal_logical_state_and_bytes(self):
        p = (GoldProvenance(reference("B"), "two"), GoldProvenance(reference(), "one"))
        r = (GoldRelation(GoldRelationKind.RELATED_TO, identity(3)), GoldRelation(GoldRelationKind.CONTRADICTS, identity(2)))
        a = unit(provenance=p, relations=r, content="  Cafe\u0301 条件\n")
        first = GoldContractState(1, 1, (unit(3), a, unit(2)))
        second = GoldContractState(1, 1, (unit(2), replace(a, provenance=p[::-1], relations=r[::-1]), unit(3)))
        with patch("league_knowledge_platform.gold_models.uuid4", side_effect=AssertionError):
            content = encode_gold_state(first)
            self.assertEqual(content, encode_gold_state(second))
            self.assertEqual(decode_gold_state(content), first)
        self.assertEqual(first, second)
        self.assertIn("条件".encode(), content)
        self.assertIn("Cafe\u0301".encode(), content)
        self.assertTrue(content.endswith(b"\n"))
        self.assertNotIn(b"\r\n", content)

    def test_strict_fields_and_nested_array_types_reject_extra_or_incomplete_payloads(self):
        original = gold_state_document(GoldContractState(1, 1, (unit(),)))
        bad = []
        for path in ((), ("units", 0), ("units", 0, "provenance", 0),
                     ("units", 0, "provenance", 0, "source_knowledge_artifact")):
            for action in ("missing", "extra"):
                value = deepcopy(original); node = value
                for key in path: node = node[key]
                if action == "missing": del node[next(iter(node))]
                else: node["embedding"] = [0.1]
                bad.append(value)
        for path in (("units",), ("units", 0, "provenance"), ("units", 0, "relations")):
            for invalid in (None, {}, "not-array", 5):
                value = deepcopy(original); node = value
                for key in path[:-1]: node = node[key]
                node[path[-1]] = invalid; bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(GoldContractError):
                decode_gold_state(json.dumps(value).encode())

    def test_malformed_and_duplicate_json_keys_are_sanitized_without_partial_result(self):
        for raw in (b"{", b"\xff", b"null", b"[]", b'"private"',
                    b'{"schema_version":1,"schema_version":1,"contract_version":1,"units":[]}',
                    b'{"schema_version":NaN,"contract_version":1,"units":[]}',
                    b'{"schema_version":Infinity,"contract_version":1,"units":[]}',
                    b"[" * 1500 + b"]" * 1500, "not-bytes", None):
            with self.subTest(raw=type(raw)), self.assertRaises(GoldContractError) as caught:
                decode_gold_state(raw)
            self.assertNotIn("private", str(caught.exception))
        raw = encode_gold_state(GoldContractState(1, 1, (unit(),)))
        raw = raw.replace(b'"source_item_id": "source-item-000001"',
                          b'"source_item_id":"one","source_item_id":"two"')
        with self.assertRaises(GoldContractError):
            decode_gold_state(raw)

    def test_unsupported_versions_are_rejected_before_interpreting_unit_payload(self):
        for key in ("schema_version", "contract_version"):
            for version in (2, 0, True, 1.0, "1", None):
                d = {"schema_version": 1, "contract_version": 1, "units": "future-shape", key: version}
                with self.subTest(key=key, version=version), self.assertRaises(GoldVersionError):
                    decode_gold_state(json.dumps(d).encode())

    def test_invalid_status_and_relations_cannot_enter_through_the_codec(self):
        original = gold_state_document(GoldContractState(1, 1, (unit(),)))
        for status in ("DELETED", True, [], None):
            d = deepcopy(original); d["units"][0]["status"] = status
            with self.subTest(status=status), self.assertRaises(GoldContractError):
                decode_gold_state(json.dumps(d).encode())
        for relation in ({"kind": "supports", "target_knowledge_id": identity(2)},
                         {"kind": "contradicts", "target_knowledge_id": "bad"},
                         {"kind": "related_to", "target_knowledge_id": identity(), "score": 1}):
            d = deepcopy(original); d["units"][0]["relations"] = [relation]
            with self.subTest(relation=relation), self.assertRaises(GoldContractError):
                decode_gold_state(json.dumps(d).encode())

    def test_document_version_codec_is_independent_and_strict(self):
        document = GoldDocumentVersion("gdoc-" + "a" * 32, 7)
        d = gold_document_version_document(document)
        self.assertEqual(decode_gold_document_version(d), document)
        for invalid in ({**d, "path": "laning.md"}, {"document_id": document.document_id}, None,
                        {**d, "document_version": True}, {**d, "document_id": identity()}):
            with self.subTest(invalid=invalid), self.assertRaises(GoldContractError):
                decode_gold_document_version(invalid)

    def test_boundary_revalidation_rejects_tampered_nested_models_without_modifying_input(self):
        state = GoldContractState(1, 1, (unit(),))
        object.__setattr__(state.units[0], "relations", ["invalid"])
        with self.assertRaises(GoldContractError):
            encode_gold_state(state)
        self.assertEqual(state.units[0].relations, ["invalid"])
        for invalid in (None, {}, (), unit()):
            with self.subTest(invalid=type(invalid)), self.assertRaises(GoldContractError):
                validate_gold_state(invalid)


if __name__ == "__main__":
    unittest.main()
