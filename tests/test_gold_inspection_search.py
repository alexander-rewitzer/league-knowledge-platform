"""Deterministic lexical candidate inspection; no semantic ranking or providers."""

from dataclasses import replace
import unittest

from league_knowledge_platform import GoldInspectionRequestError, GoldSnapshot, GoldStatus, open_gold_inspection
from gold_document_fixtures import document, topic
from test_gold_models import identity
from test_gold_inspection import InspectionFixture


def ids(page):
    return tuple(match.unit.unit.knowledge_id for match in page.matches)


class GoldInspectionSearchTests(InspectionFixture):
    def test_query_overlap_then_stable_identity_orders_matches(self):
        page = self.inspection.search_gold("Charm wave")
        self.assertEqual(ids(page), (identity(2), identity(), identity(3), identity(4), identity(6)))
        self.assertEqual(page.matches[0].matched_tokens, ("charm", "wave"))
        self.assertEqual(page.matches[0].matched_token_count, 2)
        self.assertIn("content", page.matches[0].matched_fields)
        self.assertEqual(page.total_matches, 5)
        self.assertFalse(page.has_more)
        self.assertEqual(page, self.inspection.search_gold("Charm wave"))

    def test_query_and_field_repetitions_do_not_add_weight(self):
        self.assertEqual(self.inspection.search_gold("Charm wave"),
                         self.inspection.search_gold("CHARM charm wave WAVE"))
        single = self.inspection.search_gold("wave")
        self.assertTrue(all(m.matched_token_count == 1 for m in single.matches))

    def test_searches_relevant_content_and_structured_context(self):
        for query, expected, field in (("ahri", (1, 2, 3, 6, 7), "entity"),
             ("tempo", (4,), "document"), ("Windows", (1, 2), "topic"),
             ("related_to", (1,), "relations"), ("MERGED", (6, 7), "status"),
             ("B", (4,), "provenance")):
            page = self.inspection.search_gold(query)
            with self.subTest(query=query):
                self.assertEqual(set(ids(page)), {identity(n) for n in expected})
                self.assertTrue(all(field in m.matched_fields for m in page.matches))

    def test_global_search_covers_champion_and_independent_general_knowledge(self):
        page = self.inspection.search_gold("wave")
        self.assertIn(identity(2), ids(page))
        self.assertIn(identity(4), ids(page))
        scoped = self.inspection.search_gold("wave", entity_namespace="champions", entity_key="ahri")
        self.assertNotIn(identity(4), ids(scoped))

    def test_identifier_tokens_remain_atomic_and_unknown_ids_do_not_match_prefixes(self):
        self.assertEqual(ids(self.inspection.search_gold(identity(2))), (identity(2),))
        self.assertEqual(ids(self.inspection.search_gold(identity(99))), ())
        self.assertEqual(set(ids(self.inspection.search_gold(document().document_id))),
                         {identity(n) for n in (1, 2, 3, 6, 7)})
        self.assertEqual(set(ids(self.inspection.search_gold(topic().topic_id))), {identity(n) for n in (1, 2, 4)})
        self.assertEqual(ids(self.inspection.search_gold("gku")), ())

    def test_status_topic_document_filters_do_not_retarget_history(self):
        self.assertEqual(ids(self.inspection.search_gold("charm", status=GoldStatus.MERGED)), (identity(6),))
        self.assertNotIn(identity(6), ids(self.inspection.search_gold("charm", status=GoldStatus.ACTIVE)))
        self.assertEqual(ids(self.inspection.search_gold("charm", topic_id=topic().topic_id,
            document_id=document().document_id)), (identity(), identity(2)))
        self.assertEqual(ids(self.inspection.search_gold("charm", entity_key="zed")), ())

    def test_unicode_matching_never_rewrites_canonical_content(self):
        a = self.inspection.search_gold("CAFÉ")
        b = self.inspection.search_gold("Cafe\u0301")
        self.assertEqual(a, b)
        self.assertEqual(ids(a), (identity(3),))
        self.assertIn("Cafe\u0301", a.matches[0].unit.unit.content)

    def test_paging_is_bounded_deterministic_and_retains_total(self):
        full = self.inspection.search_gold("wave")
        first = self.inspection.search_gold("wave", limit=1)
        second = self.inspection.search_gold("wave", limit=1, offset=1)
        self.assertEqual(ids(first) + ids(second), ids(full)[:2])
        self.assertEqual(first.total_matches, full.total_matches)
        self.assertTrue(first.has_more)
        self.assertEqual(first.snapshot_reference, second.snapshot_reference)
        beyond = self.inspection.search_gold("wave", offset=10**50)
        self.assertEqual(beyond.matches, ())
        self.assertEqual(beyond.total_matches, full.total_matches)
        self.assertFalse(beyond.has_more)

    def test_empty_unmatched_and_punctuation_queries_do_not_browse_corpus(self):
        for query in ("", " \n\t", "___!?", "unrepresentedlexeme"):
            with self.subTest(query=query):
                page = self.inspection.search_gold(query)
                self.assertEqual(page.matches, ())
                self.assertEqual(page.total_matches, 0)
                self.assertFalse(page.has_more)

    def test_invalid_requests_are_rejected_without_echoing_query(self):
        for changes in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.0},
            {"offset": -1}, {"offset": True}, {"offset": "0"}, {"status": "ACTIVE"},
            {"entity_key": ""}, {"domain_key": "../path"}, {"topic_id": "Heading"},
            {"document_id": "file.md"}):
            with self.subTest(changes=changes), self.assertRaises(GoldInspectionRequestError) as caught:
                self.inspection.search_gold("private-query", **changes)
            self.assertNotIn("private-query", str(caught.exception))
        for query in (None, 1, True, "bad\ud800"):
            with self.subTest(query=repr(query)), self.assertRaises(GoldInspectionRequestError):
                self.inspection.search_gold(query)

    def test_full_long_content_is_not_truncated_or_summarized(self):
        doc = self.snapshot.documents[0]
        content = "Long source-grounded content. " * 2000
        changed = replace(doc, version=replace(doc.version, document_version=2),
            units=tuple(replace(u, content=content) if u.knowledge_id == identity() else u for u in doc.units))
        self.publish(GoldSnapshot((changed, *self.snapshot.documents[1:])))
        session = open_gold_inspection(self.config)
        self.assertEqual(session.search_gold("source-grounded").matches[0].unit.unit.content, content)


if __name__ == "__main__":
    unittest.main()
