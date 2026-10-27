from dataclasses import replace
import json
from pathlib import Path
import random
import unittest

from league_knowledge_platform import (
    GoldContractError, GoldDocumentError, GoldSnapshot, GoldStatus, GoldVersionError,
    decode_gold_document_pair, encode_gold_document_metadata, gold_document_metadata,
    render_gold_document, validate_gold_snapshot,
)
from league_knowledge_platform.gold_documents import validate_document_transition
from gold_document_fixtures import document, topic
from test_gold_models import identity, unit


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class GoldDocumentCodecTests(unittest.TestCase):
    def test_multi_topic_round_trip_and_readable_fixture(self):
        d = document(topics=(topic(), topic(2, (3,), position=1, title="Trading")),
                     units=(unit(3), *document().units))
        md, meta = render_gold_document(d), encode_gold_document_metadata(d)
        self.assertIn(b"## Reset Timing", md)
        self.assertIn(b"## Trading", md)
        self.assertEqual(md.count(b"<!-- knowledge:"), 3)
        self.assertNotIn(b"source_item_id", md)
        self.assertEqual(decode_gold_document_pair(md, meta), d)
        self.assertEqual(encode_gold_document_metadata(decode_gold_document_pair(md, meta)), meta)

    def test_exact_unicode_whitespace_control_and_marker_like_content(self):
        samples = ["  Cafe\u0301 — 条件\nunchanged  ", "  one\r\ntwo\t\n\n", "a &amp; &#32; b",
                   "<!-- /knowledge -->\n<!-- knowledge:" + identity(2) + " -->",
                   "# heading\n1. list\n> quote\n```code```\n[link](url)", "\x00x\x7f", "\n\ntext\n"]
        random.seed(51)
        samples += ["prefix" + "".join(chr(random.randrange(0, 0xD800)) for _ in range(100)) for _ in range(12)]
        for content in samples:
            with self.subTest(content=repr(content)):
                d = document(units=(unit(1, content=content), unit(2)))
                loaded = decode_gold_document_pair(render_gold_document(d), encode_gold_document_metadata(d))
                self.assertEqual(loaded.units[0].content, content)

    def test_markers_reject_missing_unknown_duplicate_malformed_nested_or_unmarked(self):
        d = document()
        md, meta = render_gold_document(d), encode_gold_document_metadata(d)
        marker = f"<!-- knowledge:{identity()} -->".encode()
        bad = [md.replace(marker, b"<!-- malformed -->"), md.replace(marker, marker + b"\n" + marker),
               md.replace(marker, f"<!-- knowledge:{identity(99)} -->".encode()),
               md.replace(b"<!-- /knowledge -->", b"<!-- /unknown -->", 1),
               md + b"extra prose\n", md.replace(b"## Reset Timing", b"### Reset Timing"),
               md.replace(b"Prepare", b"Changed", 1),
               md.replace(b"<!-- /gold-topic -->", b"<!-- /gold-topic -->\n<!-- /gold-topic -->")]
        for corrupted in bad:
            with self.subTest(markdown=corrupted[:80]), self.assertRaises(GoldContractError):
                decode_gold_document_pair(corrupted, meta)

    def test_metadata_strict_json_and_versions(self):
        d = document()
        md, meta = render_gold_document(d), encode_gold_document_metadata(d)
        for bad in (meta.replace(b'{', b'{"title":"duplicate",', 1), b"{", b"null", b"[]", b"\xff"):
            with self.subTest(bad=bad[:60]), self.assertRaises(GoldContractError):
                decode_gold_document_pair(md, bad)
        for field in ("document_schema_version", "gold_schema_version", "gold_contract_version", "marker_format_version"):
            for invalid in (True, 2, "1", None):
                value = gold_document_metadata(d)
                value[field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(GoldVersionError):
                    decode_gold_document_pair(md, json_bytes(value))

    def test_pair_field_and_reference_mismatches(self):
        d = document()
        mutations = [lambda v: v.update(extra=True), lambda v: v.update(document_id="gdoc-" + "f" * 32),
            lambda v: v.update(document_version=2), lambda v: v.update(title="Other"),
            lambda v: v.update(topics=[]), lambda v: v.update(units=[]),
            lambda v: v["topics"][0].update(title="Other"),
            lambda v: v["topics"][0].update(units=[]),
            lambda v: v["topics"][0]["units"][0].update(knowledge_id=identity(99)),
            lambda v: v["units"][0].update(content_sha256="a" * 64),
            lambda v: v["units"].append(v["units"][0]),
            lambda v: v["units"][0].update(content="should not be duplicated"),
            lambda v: v["units"][0].update(status="MERGED", merged_into=identity(2)),
            lambda v: v["entity"].update(extra=1)]
        for mutate in mutations:
            value = gold_document_metadata(d)
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(GoldContractError):
                decode_gold_document_pair(render_gold_document(d), json_bytes(value))

    def test_deterministic_bytes_ignore_input_collection_order(self):
        d = document()
        alternate = replace(d, units=tuple(reversed(d.units)),
            topics=(replace(d.topics[0], units=tuple(reversed(d.topics[0].units))),))
        self.assertEqual(render_gold_document(d), render_gold_document(alternate))
        self.assertEqual(encode_gold_document_metadata(d), encode_gold_document_metadata(alternate))

    def test_version_transitions_do_not_allocate_or_replace_unit_identity(self):
        d = document()
        changed = replace(d, units=(replace(d.units[0], content="Rewritten condition."), d.units[1]))
        with self.assertRaises(GoldDocumentError):
            validate_document_transition(GoldSnapshot((d,)), GoldSnapshot((changed,)))
        changed = replace(changed, version=replace(d.version, document_version=2))
        validate_document_transition(GoldSnapshot((d,)), GoldSnapshot((changed,)))
        self.assertEqual(d.units[0].knowledge_id, changed.units[0].knowledge_id)
        with self.assertRaises(GoldDocumentError):
            validate_document_transition(GoldSnapshot((d,)), GoldSnapshot((replace(d, version=changed.version),)))
        with self.assertRaises(GoldDocumentError):
            validate_document_transition(GoldSnapshot((d,)), GoldSnapshot())

    def test_checked_in_synthetic_fixture_matches_renderer(self):
        root = Path(__file__).parent / "fixtures" / "gold_documents"
        d = decode_gold_document_pair((root / "laning.md").read_bytes(), (root / "laning.meta.json").read_bytes())
        self.assertEqual(render_gold_document(d), (root / "laning.md").read_bytes())
        self.assertEqual(encode_gold_document_metadata(d), (root / "laning.meta.json").read_bytes())
        self.assertEqual(len(d.topics), 2)
