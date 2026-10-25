"""Gold values preserve stable identity, source references and immutable content."""

from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldContractError, GoldContractState, GoldDocumentVersion, GoldKnowledgeUnit,
    GoldProvenance, GoldRelation, GoldRelationKind, GoldSourceArtifactReference,
    GoldStatus, GoldVersionError, new_document_id, new_knowledge_id,
    validate_gold_unit,
)


def identity(number=1):
    return f"gku-{number:032x}"


def reference(source="A", **changes):
    return replace(GoldSourceArtifactReference("youtube", source,
        f"youtube/id-{source.encode('ascii').hex()}.json", "a" * 64, 1, 1), **changes)


def unit(number=1, **changes):
    return replace(GoldKnowledgeUnit(identity(number), " Preserve the stated condition.\n",
        (GoldProvenance(reference(), "source-item-000001"),), GoldStatus.ACTIVE), **changes)


class GoldModelTests(unittest.TestCase):
    def test_explicit_generation_has_no_content_or_placement_input(self):
        generated = UUID("fa49faf2-03a0-4c62-a25f-6b21592ef301")
        with patch("league_knowledge_platform.gold_models.uuid4", return_value=generated) as generate:
            self.assertEqual(new_knowledge_id(), "gku-" + generated.hex)
            self.assertEqual(new_document_id(), "gdoc-" + generated.hex)
            self.assertEqual(generate.call_args_list, [unittest.mock.call(), unittest.mock.call()])

    def test_content_replacement_preserves_identity_without_generating_one(self):
        original = unit()
        with patch("league_knowledge_platform.gold_models.uuid4", side_effect=AssertionError):
            changed = replace(original, content="A compatible rewritten statement.")
            validate_gold_unit(changed)
        self.assertEqual(changed.knowledge_id, original.knowledge_id)
        self.assertEqual(changed.provenance, original.provenance)
        self.assertNotEqual(changed.content, original.content)
        self.assertEqual({f.name for f in fields(original)},
            {"knowledge_id", "content", "provenance", "status", "relations", "merged_into"})

    def test_invalid_identity_never_gets_repaired_or_reallocated(self):
        for value in (None, True, 1, "gku-118", "gku-" + "A" * 32, "gku-" + "a" * 31,
                      "gku-" + "0" * 32 + "\n", "gdoc-" + "0" * 32):
            with self.subTest(value=value), self.assertRaises(GoldContractError):
                unit(knowledge_id=value)

    def test_unicode_content_is_exact_and_required_even_on_historical_records(self):
        content = "  Cafe\u0301 — 条件\nunchanged  "
        self.assertEqual(unit(content=content).content, content)
        for value in ("", " \t\n", None, 10, "bad\ud800"):
            for status in (GoldStatus.ACTIVE, GoldStatus.MERGED):
                with self.subTest(value=repr(value), status=status), self.assertRaises(GoldContractError):
                    unit(content=value, status=status, merged_into=identity(2) if status is GoldStatus.MERGED else None)

    def test_provenance_is_nonempty_unique_immutable_and_sorted_without_changing_callers(self):
        p = (GoldProvenance(reference("B"), "opaque-item"), GoldProvenance(reference("A"), "local/文"))
        result = unit(provenance=p)
        self.assertEqual(result.provenance, p[::-1])
        self.assertEqual(p[0].source_knowledge_artifact.source_id, "B")
        for value in ((), list(p), (p[0], p[0]), (None,), (reference(),)):
            with self.subTest(value=value), self.assertRaises(GoldContractError):
                unit(provenance=value)
        for value in ("", " ", None, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(GoldContractError):
                GoldProvenance(reference(), value)

    def test_source_reference_paths_hashes_versions_and_case_sensitive_identity(self):
        self.assertNotEqual(reference("A"), reference("a"))
        self.assertEqual(reference(relative_path="custom/source-result.json").relative_path, "custom/source-result.json")
        bad = [{"relative_path": p} for p in ("", "/abs", "C:/file", "a\\file", "../file", "a/../file",
               "a/./file", "a//file", "a/", "//server/file", "a\0file")]
        bad += [{"sha256": x} for x in ("f" * 63, "F" * 64, None, 10)]
        bad += [{name: x} for name in ("schema_version", "audit_contract_version") for x in (0, -1, True, 1.0, "1")]
        bad += [{"source_id": x} for x in (None, "", "a/b", "文")]
        bad += [{"source_type": "other"}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(GoldContractError):
                reference(**changes)
        # Structural references do not claim an unknown upstream schema is supported.
        self.assertEqual(reference(schema_version=99).schema_version, 99)

    def test_status_has_only_active_and_merged_and_local_survivor_rules(self):
        self.assertEqual(set(GoldStatus), {GoldStatus.ACTIVE, GoldStatus.MERGED})
        historical = unit(status=GoldStatus.MERGED, merged_into=identity(2))
        self.assertEqual(historical.content, unit().content)
        for changes in ({"status": "ACTIVE"}, {"status": None}, {"status": "DELETED"},
                        {"merged_into": identity(2)}, {"status": GoldStatus.MERGED},
                        {"status": GoldStatus.MERGED, "merged_into": identity()},
                        {"status": GoldStatus.MERGED, "merged_into": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(GoldContractError):
                unit(**changes)

    def test_relations_are_minimal_unique_and_literal_self_edges_fail(self):
        related = GoldRelation(GoldRelationKind.RELATED_TO, identity(2))
        contradiction = GoldRelation(GoldRelationKind.CONTRADICTS, identity(3))
        self.assertEqual(unit(relations=(related, contradiction)).relations, (contradiction, related))
        self.assertEqual(unit().relations, ())
        for value in ((related, related), [related], (None,), (GoldRelation(GoldRelationKind.RELATED_TO, identity()),)):
            with self.subTest(value=value), self.assertRaises(GoldContractError):
                unit(relations=value)
        for kind in ("related_to", "implies", None):
            with self.subTest(kind=kind), self.assertRaises(GoldContractError):
                GoldRelation(kind, identity(2))

    def test_frozen_models_and_revalidation_of_nested_tampering(self):
        value = unit()
        with self.assertRaises(FrozenInstanceError):
            value.content = "Changed"
        with self.assertRaises(FrozenInstanceError):
            value.provenance[0].source_item_id = "Changed"
        object.__setattr__(value.provenance[0].source_knowledge_artifact, "sha256", "invalid")
        with self.assertRaises(GoldContractError):
            validate_gold_unit(value)

    def test_document_version_and_contract_versions_are_separate(self):
        document = GoldDocumentVersion("gdoc-" + "a" * 32, 1)
        self.assertEqual(replace(document, document_version=2).document_id, document.document_id)
        self.assertEqual(unit().knowledge_id, unit().knowledge_id)
        for value in (0, -1, True, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(GoldContractError):
                replace(document, document_version=value)
        for name in ("schema_version", "contract_version"):
            for value in (0, 2, True, 1.0, "1"):
                with self.subTest(name=name, value=value), self.assertRaises(GoldVersionError):
                    GoldContractState(**{"schema_version": 1, "contract_version": 1, name: value})


if __name__ == "__main__":
    unittest.main()
