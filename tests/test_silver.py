"""Offline Silver cleaning, artifact contract and provenance tests."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeIntegrationContext, SilverConfig, SilverContractError, SilverInputError,
    SilverIntegrityError, canonical_silver_path, construct_bronze_artifact,
    construct_silver_artifact, decode_silver_artifact, validate_bronze_artifact,
    validate_silver_against_bronze, validate_silver_artifact,
)
from test_bronze import NOW, TARGET, selected_source


def bronze_fixture(*raw_texts):
    artifact = construct_bronze_artifact(selected_source(), created_at_utc=NOW,
                                        context=BronzeIntegrationContext("original-run", NOW, TARGET))
    template = artifact["transcript"]["segments"][0]
    artifact["transcript"]["segments"] = [
        {**template, "source_index": index * 3, "source_text": raw,
         "text": "Existing Bronze text", "start_seconds": "0.12500", "duration_seconds": "1E-8"}
        for index, raw in enumerate(raw_texts or ("  Cafe\u0301\tuses\r\nCharm. ",))
    ]
    artifact["transcript"]["text"] = "\n".join(s["text"] for s in artifact["transcript"]["segments"])
    return validate_bronze_artifact(artifact)


def bronze_bytes(artifact):
    return (json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def silver_fixture(bronze=None):
    bronze = bronze if bronze is not None else bronze_fixture()
    return construct_silver_artifact(bronze, bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest())


class SilverContractTests(unittest.TestCase):
    def test_exact_normalization_examples(self):
        examples = (
            ("  Ahri\tuses\r\nCharm.  ", "Ahri uses Charm."),
            ("Cafe\u0301", "Café"),
            ("A\u00a0\u2003B", "A B"),
            ("A\rB\nC\vD\fE\u0085F\u2028G\u2029H", "A B C D E F G H"),
            ("again again", "again again"),
            ("this video is sponsored", "this video is sponsored"),
            ("uh uh odd grammer no punctuation", "uh uh odd grammer no punctuation"),
            ("\u200bkeep\u200b", "\u200bkeep\u200b"),
            ("ＦＵＬＬ ﬁ", "ＦＵＬＬ ﬁ"),
            ("", ""),
            (" \t\r\n ", ""),
        )
        for raw, expected in examples:
            with self.subTest(raw=repr(raw)):
                artifact = silver_fixture(bronze_fixture(raw))
                self.assertEqual(artifact["transcript"]["segments"][0]["text"], expected)
                self.assertEqual(artifact["transcript"]["text"], expected)

    def test_source_text_is_input_not_previous_bronze_normalized_text(self):
        artifact = silver_fixture()
        self.assertEqual(artifact["transcript"]["text"], "Café uses Charm.")
        self.assertNotIn("Existing Bronze text", json.dumps(artifact))

    def test_order_gaps_repetition_empty_segments_and_timing_are_preserved(self):
        bronze = bronze_fixture("repeated", "\t ", "repeated", "")
        bronze["transcript"]["segments"][2]["start_seconds"] = "0"
        bronze["transcript"]["segments"][3]["duration_seconds"] = "0.000"
        artifact = silver_fixture(bronze)
        self.assertEqual(artifact["transcript"]["text"], "repeated repeated")
        self.assertEqual([s["text"] for s in artifact["transcript"]["segments"]], ["repeated", "", "repeated", ""])
        for original, cleaned in zip(bronze["transcript"]["segments"], artifact["transcript"]["segments"]):
            for name in ("source_index", "start_seconds", "duration_seconds"):
                self.assertEqual(cleaned[name], original[name])

    def test_provenance_metadata_and_original_input_are_preserved(self):
        bronze = bronze_fixture()
        before = deepcopy(bronze)
        artifact = silver_fixture(bronze)
        self.assertEqual(artifact["source"], bronze["source"])
        self.assertEqual(artifact["acquisition"], bronze["acquisition"])
        self.assertEqual(artifact["bronze"], {"schema_version": 1, "relative_path": "youtube/id-41.json",
                                             "sha256": sha256(bronze_bytes(bronze)).hexdigest()})
        for name in ("language_code", "caption_kind", "source_track_id", "provider_name", "provider_version"):
            self.assertEqual(artifact["transcript"][name], bronze["transcript"][name])
        self.assertNotIn("selection", artifact)
        artifact["source"]["title"] = "Independent copy"
        artifact["acquisition"]["run_id"] = "different"
        self.assertEqual(bronze, before)

    def test_missing_optional_metadata_is_not_invented(self):
        bronze = bronze_fixture()
        for name in ("title", "channel_title", "published_at_utc", "duration_seconds", "view_count"):
            bronze["source"][name] = None
        for name in ("target_key", "target_name", "run_id", "selected_at_utc"):
            bronze["acquisition"][name] = None
        artifact = silver_fixture(bronze)
        self.assertEqual(artifact["source"], bronze["source"])
        self.assertEqual(artifact["acquisition"], bronze["acquisition"])

    def test_standalone_downstream_round_trip_needs_no_bronze_file(self):
        artifact = silver_fixture(bronze_fixture("first", "second"))
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("No Bronze access")):
            decoded = decode_silver_artifact(bronze_bytes(artifact))
        self.assertEqual(decoded, artifact)
        self.assertEqual(decoded["transcript"]["text"], "first second")
        self.assertEqual(decoded["cleaning"]["rule_version"], 1)

    def test_materially_equal_unicode_version_differences_are_compatible(self):
        bronze = bronze_fixture()
        artifact = silver_fixture(bronze)
        artifact["cleaning"]["unicode_version"] = "different-runtime"
        self.assertIs(validate_silver_artifact(artifact), artifact)
        self.assertIs(validate_silver_against_bronze(artifact, bronze,
                      bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest()), artifact)
        with patch("league_knowledge_platform.silver.unicodedata.unidata_version", "another-runtime"):
            self.assertIs(validate_silver_against_bronze(artifact, bronze,
                          bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest()), artifact)

    def test_material_difference_conflicts_regardless_of_unicode_version(self):
        bronze = bronze_fixture()
        for version in (silver_fixture()["cleaning"]["unicode_version"], "different-runtime"):
            artifact = silver_fixture(bronze)
            artifact["cleaning"]["unicode_version"] = version
            artifact["transcript"]["segments"][0]["text"] = "changed"
            artifact["transcript"]["text"] = "changed"
            with self.subTest(version=version), self.assertRaises(SilverIntegrityError):
                validate_silver_against_bronze(artifact, bronze,
                    bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest())

    def test_all_provenance_and_timing_fields_are_material(self):
        bronze = bronze_fixture()
        mutations = (
            lambda a: a["source"].update(title="changed title"),
            lambda a: a["acquisition"].update(run_id="later-run"),
            lambda a: a["bronze"].update(sha256="0" * 64),
            lambda a: a["transcript"].update(provider_version="later"),
            lambda a: a["transcript"]["segments"][0].update(source_index=1),
            lambda a: a["transcript"]["segments"][0].update(start_seconds="0.125000"),
        )
        for mutate in mutations:
            artifact = silver_fixture(bronze)
            mutate(artifact)
            with self.subTest(mutate=mutate), self.assertRaises(SilverIntegrityError):
                validate_silver_against_bronze(artifact, bronze,
                    bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest())

    def test_repeated_construction_is_deterministic(self):
        bronze = bronze_fixture()
        self.assertEqual(bronze_bytes(silver_fixture(bronze)), bronze_bytes(silver_fixture(bronze)))

    def test_invalid_contracts_and_unsupported_versions_fail_explicitly(self):
        mutations = (
            lambda a: a.update(schema_version=2),
            lambda a: a.update(schema_version=True),
            lambda a: a.update(extra="field"),
            lambda a: a["cleaning"].update(rule_version=2),
            lambda a: a["cleaning"].update(rule_version=True),
            lambda a: a["cleaning"].update(input_field="text"),
            lambda a: a["cleaning"].update(unicode_version=""),
            lambda a: a["source"].update(video_id="../A"),
            lambda a: a["source"].update(url="https://youtu.be/A"),
            lambda a: a["source"].update(view_count=True),
            lambda a: a["source"].update(published_at_utc="not-a-date"),
            lambda a: a["bronze"].update(schema_version=2),
            lambda a: a["bronze"].update(relative_path="../bronze.json"),
            lambda a: a["bronze"].update(sha256="not-a-hash"),
            lambda a: a["acquisition"].update(run_id=True),
            lambda a: a["acquisition"].update(target_id=""),
            lambda a: a["acquisition"].update(bronze_created_at_utc="2026-01-01"),
            lambda a: a["transcript"].update(caption_kind="invented"),
            lambda a: a["transcript"].update(language_code=""),
            lambda a: a["transcript"].update(source_track_id=""),
            lambda a: a["transcript"].update(text="truncated"),
            lambda a: a["transcript"].update(segments=[]),
            lambda a: a["transcript"]["segments"][0].update(source_index=True),
            lambda a: a["transcript"]["segments"][0].update(source_index=-1),
            lambda a: a["transcript"]["segments"][0].update(start_seconds=0.125),
            lambda a: a["transcript"]["segments"][0].update(start_seconds="NaN"),
            lambda a: a["transcript"]["segments"][0].update(start_seconds=" 0"),
            lambda a: a["transcript"]["segments"][0].update(duration_seconds="-1"),
            lambda a: a["transcript"]["segments"][0].update(text=" padded "),
            lambda a: a["transcript"]["segments"][0].update(text="Cafe\u0301"),
        )
        for mutate in mutations:
            artifact = silver_fixture()
            mutate(artifact)
            with self.subTest(mutate=mutate), self.assertRaises(SilverContractError):
                validate_silver_artifact(artifact)
        artifact = silver_fixture(bronze_fixture("one", "two"))
        artifact["transcript"]["segments"].reverse()
        with self.assertRaises(SilverContractError):
            validate_silver_artifact(artifact)

    def test_corrupt_bronze_cannot_be_constructed(self):
        for field, value in (("schema_version", 2), ("schema_version", True), ("transcript", {})):
            bronze = bronze_fixture()
            bronze[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SilverInputError):
                silver_fixture(bronze)

    def test_strict_json_decoder(self):
        for content in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}',
                        b'{"x":Infinity}', b'{', b'\xff', b'[]', b'null'):
            with self.subTest(content=content), self.assertRaises(SilverContractError):
                decode_silver_artifact(content)

    def test_safe_paths_preserve_case_and_reject_invalid_identity(self):
        config = SilverConfig(Path("synthetic silver"))
        for video_id, encoded in (("A", "41"), ("a", "61"), ("CON", "434f4e")):
            self.assertEqual(canonical_silver_path(video_id, config),
                             Path(config.root).resolve() / "youtube" / f"id-{encoded}.json")
        for video_id in ("", "../A", "A/B", "A\n", "é", None):
            with self.subTest(video_id=video_id), self.assertRaises(ValueError):
                canonical_silver_path(video_id, config)
        for root in (None, "", " "):
            with self.subTest(root=root), self.assertRaises(ValueError):
                SilverConfig(root)


if __name__ == "__main__":
    unittest.main()
