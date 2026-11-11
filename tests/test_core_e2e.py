"""Offline acceptance through production stage APIs with scripted semantics."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    AcquisitionRunService, BronzeConfig, CandidateVideo, CaptionSegment, DiscoveryPage,
    GoldConfig, GoldCreatePayload, GoldEnrichPayload, GoldFlagConflictPayload,
    GoldHistoryError, GoldLinkPayload, GoldMergePayload, GoldNewUnitIntent,
    GoldNoChangePayload, GoldOperationKind, GoldProvenance, GoldProvenanceAddition,
    GoldRelationKind, GoldRewritePayload, GoldStatus, SectionValidationConfig,
    SegmentationConfig, SilverConfig, SourceAuditConfig, canonical_bronze_path,
    canonical_section_knowledge_path, canonical_segmentation_path, canonical_silver_path,
    canonical_source_knowledge_path, clean_bronze_source, commit_prepared_integration,
    decode_bronze_artifact, decode_silver_artifact, initialize_gold_integration_store,
    initialize_run_repository, initialize_source_catalog, integrate_source_knowledge,
    load_gold_history, load_gold_snapshot, load_source_knowledge,
    load_validated_section_knowledge, open_gold_inspection, prepare_gold_integration,
    process_section_knowledge, process_source_knowledge, segment_silver_source,
    validate_silver_against_bronze,
)
from league_knowledge_platform import _gold_filesystem

from gold_document_fixtures import layout, topic
from gold_integration_fixtures import FakeAgent, FakeValidator, plan
from run_fixtures import CHAMPIONS, NOW, Captions, Discovery, Selection, process_environment, write_catalog
from section_validation_fixtures import FakeExtractor, FakeJudge, evaluation, response
from segmentation_http_fixtures import FakeSegmentation, proposal
from source_audit_fixtures import FakeAudit, FakeValidator as SourceValidator, audit_response, validator_evaluation
from test_gold_governance_models import edge, operation, placement
from test_gold_models import identity, unit


class CoreAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bronze = BronzeConfig(self.root / "bronze")
        self.silver = SilverConfig(self.root / "silver")
        self.segmentation = SegmentationConfig(self.root / "segments")
        self.sections = SectionValidationConfig(self.root / "validated")
        self.audit = SourceAuditConfig(self.root / "source")
        self.gold = GoldConfig(self.root / "gold")
        self.source_configs = dict(silver_config=self.silver, segmentation_config=self.segmentation,
                                   section_validation_config=self.sections, config=self.audit)
        self.runs = initialize_run_repository(self.root / "runs.sqlite3")
        self.catalog = initialize_source_catalog(self.root / "sources.sqlite3")
        self.targets = write_catalog(self.root / "targets.json", champions=CHAMPIONS[:1])
        initialize_gold_integration_store(self.gold)

    def acquire_and_audit(self, video_id, statement, *, empty=False):
        candidate = CandidateVideo("Ahri", video_id, "Fixture guide", "Fixture channel", NOW, 1000, 600)
        discovery = Discovery({"Ahri": [DiscoveryPage("Ahri", (candidate,), None, True)]})

        class FixtureCaptions(Captions):
            def fetch_track(self, track):
                return replace(super().fetch_track(track), segments=(
                    CaptionSegment(0, statement, Decimal("0.125"), Decimal("1.25")),
                ))

        service = AcquisitionRunService(self.runs, discovery=discovery, catalog_reader=self.catalog,
                                        caption_provider=FixtureCaptions(), selection_capability=Selection(),
                                        bronze_config=self.bronze, clock=lambda: NOW)
        run = service.start_new_run(self.targets)
        self.assertEqual(run.integrated, 1)
        self.assertEqual(self.runs.list_targets(run.run_id)[0].selected_video_id, video_id)
        clean_bronze_source(video_id, bronze_config=self.bronze, silver_config=self.silver)
        segment_silver_source(video_id, bronze_config=self.bronze, silver_config=self.silver, config=self.segmentation,
                             capability=FakeSegmentation([proposal((0, 0, "CONTENT"))]))
        section_args = dict(bronze_config=self.bronze, silver_config=self.silver,
                            segmentation_config=self.segmentation, config=self.sections)
        checkpoint = process_section_knowledge(video_id, "section-000001", **section_args,
            extraction_capability=FakeExtractor([response(*(() if empty else (statement,)), spans=((0, 0),))]),
            judge_capability=FakeJudge([evaluation()]))
        self.assertTrue(checkpoint.created)
        # Continue from the durable section boundary without supplying semantic roles.
        resumed = process_section_knowledge(video_id, "section-000001", **section_args)
        self.assertFalse(resumed.created)
        self.assertEqual(resumed.operation_rounds_used, 0)
        self.assertEqual(resumed.checkpoint, checkpoint.checkpoint)
        source = process_source_knowledge(video_id, **self.source_configs,
            audit_capability=None if empty else FakeAudit([audit_response((statement, ("input-000001",)))]),
            validator_capability=SourceValidator([validator_evaluation()]))
        self.assertTrue(source.created)
        return run.run_id

    def prepare(self, video_id, build):
        def propose(request):
            supports = tuple(GoldProvenance(request.source.reference, item_id)
                             for item_id in sorted(request.source.item_ids))
            payload, kind, layouts = build(supports[0], request.inspection.snapshot)
            return plan(operations=(operation(payload, kind, refs=supports),), layouts=layouts,
                        source_reference=request.source.reference, base_reference=request.inspection.reference)

        return prepare_gold_integration(video_id, **self.source_configs, gold_config=self.gold,
                                        agent=FakeAgent([propose]), validator=FakeValidator())

    def commit(self, prepared):
        return commit_prepared_integration(prepared, **self.source_configs, gold_config=self.gold)

    @staticmethod
    def create(number, statement):
        def build(support, snapshot):
            existing = snapshot.documents[0].topics if snapshot.documents else ()
            new_topic = topic(number, (number,), position=len(existing), title=f"Fixture topic {number}")
            new = GoldNewUnitIntent(unit(number, content=statement, provenance=(support,)),
                                    placement(0, topic=number))
            return GoldCreatePayload(new), GoldOperationKind.CREATE, (layout(topics=(*existing, new_topic)),)
        return build

    def assert_lineage(self, provenance, run_id):
        ref = provenance.source_knowledge_artifact
        video_id = ref.source_id
        source = load_source_knowledge(video_id, **self.source_configs)
        source_path = canonical_source_knowledge_path(video_id, self.audit)
        self.assertEqual(source_path.relative_to(self.audit.root).as_posix(), ref.relative_path)
        self.assertEqual(sha256(source_path.read_bytes()).hexdigest(), ref.sha256)
        item = next(i for i in source.candidate.items if i.item_id == provenance.source_item_id)
        self.assertTrue(item.derived_from)
        bronze_bytes = canonical_bronze_path(video_id, self.bronze).read_bytes()
        silver_bytes = canonical_silver_path(video_id, self.silver).read_bytes()
        bronze = decode_bronze_artifact(bronze_bytes)
        silver = decode_silver_artifact(silver_bytes)
        self.assertEqual(bronze["source"]["video_id"], video_id)
        self.assertEqual(bronze["source"]["url"], f"https://www.youtube.com/watch?v={video_id}")
        self.assertEqual(bronze["acquisition"]["run_id"], run_id)
        self.assertEqual(sha256(bronze_bytes).hexdigest(), source.provenance.bronze.sha256)
        self.assertEqual(sha256(silver_bytes).hexdigest(), source.provenance.silver.sha256)
        self.assertEqual(sha256(canonical_segmentation_path(video_id, self.segmentation).read_bytes()).hexdigest(),
                         source.provenance.segmentation.sha256)
        validate_silver_against_bronze(silver, bronze, bronze_sha256=source.provenance.bronze.sha256)
        for contribution in item.derived_from:
            section_id = contribution.section_id
            checkpoint = load_validated_section_knowledge(video_id, section_id, silver_config=self.silver,
                segmentation_config=self.segmentation, config=self.sections)
            section_ref = next(r for r in source.validated_sections if r.section_id == section_id)
            path = canonical_section_knowledge_path(video_id, section_id, self.sections)
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), section_ref.sha256)
            original = next(i for i in checkpoint.accepted_proposal.items if i.item_id == contribution.item_id)
            for evidence in original.evidence:
                self.assertIn(evidence, item.evidence)
                for span in evidence.spans:
                    self.assertEqual((span.first_source_index, span.last_source_index), (0, 0))
                    segment = next(s for s in bronze["transcript"]["segments"] if s["source_index"] == 0)
                    self.assertEqual(Decimal(segment["start_seconds"]), Decimal("0.125"))
                    self.assertEqual(Decimal(segment["duration_seconds"]), Decimal("1.25"))

    def test_sequential_canonical_sources_exercise_every_governed_operation(self):
        first = "Hold Charm while Zed can engage."
        second = "Zed can engage when Charm is unavailable."
        enriched = "Hold Charm while Zed can engage and until he commits his shadow."
        rewritten = "Keep Charm ready until Zed commits his shadow whenever he can engage."
        merged = f"{rewritten} {second}"
        opposing = "Use Charm immediately even while Zed can engage."
        kind = GoldOperationKind

        def no_change(add_support):
            return lambda ref, _: (GoldNoChangePayload((identity(),),
                (GoldProvenanceAddition(identity(), ref),) if add_support else ()), kind.NO_CHANGE, ())

        def revise(payload, operation_kind, content):
            def build(ref, snapshot):
                old = snapshot.documents[0].units[0]
                return payload(old.knowledge_id, replace(old, content=content,
                    provenance=(*old.provenance, ref))), operation_kind, ()
            return build

        def merge(ref, snapshot):
            survivor, retired = snapshot.documents[0].units
            return GoldMergePayload((survivor.knowledge_id, retired.knowledge_id), replace(survivor,
                content=merged, provenance=(*survivor.provenance, *retired.provenance, ref))), kind.MERGE, ()

        def conflict(ref, snapshot):
            doc = snapshot.documents[0]
            new = GoldNewUnitIntent(unit(3, content=opposing, provenance=(ref,)), placement(1))
            payload = GoldFlagConflictPayload((identity(), identity(3)), (new,),
                (edge(1, 3, GoldRelationKind.CONTRADICTS),), (), "Incompatible guidance for the same engage window.")
            return payload, kind.FLAG_CONFLICT, (layout(version=doc.document_version,
                topics=(replace(doc.topics[0], units=topic(ids=(1, 3)).units),)),)

        cases = (
            (first, self.create(1, first), kind.CREATE, {"KNOWLEDGE_CREATED", "PROVENANCE_ADDED"}),
            (second, self.create(2, second), kind.CREATE, {"KNOWLEDGE_CREATED", "PROVENANCE_ADDED"}),
            (first, no_change(False), kind.NO_CHANGE, {"NO_CHANGE_RECORDED"}),
            (first, no_change(True), kind.NO_CHANGE, {"PROVENANCE_ADDED"}),
            (enriched, revise(GoldEnrichPayload, kind.ENRICH, enriched), kind.ENRICH,
             {"KNOWLEDGE_ENRICHED", "PROVENANCE_ADDED"}),
            (rewritten, revise(GoldRewritePayload, kind.REWRITE, rewritten), kind.REWRITE,
             {"KNOWLEDGE_REWRITTEN", "PROVENANCE_ADDED"}),
            (merged, lambda ref, state: (GoldLinkPayload(edge()), kind.LINK, ()), kind.LINK, {"RELATION_ADDED"}),
            (merged, merge, kind.MERGE, {"KNOWLEDGE_MERGED", "PROVENANCE_ADDED"}),
            (opposing, conflict, kind.FLAG_CONFLICT,
             {"KNOWLEDGE_CREATED", "PROVENANCE_ADDED", "RELATION_ADDED", "CONFLICT_FLAGGED"}),
        )
        records, runs = [], {}
        for index, (statement, build, operation_kind, event_types) in enumerate(cases):
            video_id = f"fixture-{index}"
            with self.subTest(operation=operation_kind, source=video_id):
                runs[video_id] = self.acquire_and_audit(video_id, statement)
                before = load_gold_snapshot(self.gold)
                result = self.commit(self.prepare(video_id, build))
                after = load_gold_snapshot(self.gold)
                record = result.integration
                records.append(record)
                history = load_gold_history(self.gold)
                self.assertEqual(history.integrations, tuple(records))
                self.assertEqual(record.base_reference, before.reference)
                self.assertEqual(record.result_reference, after.reference)
                self.assertEqual(tuple(o.kind for o in record.operations), (operation_kind,))
                expected = Counter(event_types)
                if operation_kind is kind.MERGE:
                    # The survivor gains both the retired unit's evidence and the incoming source.
                    expected["PROVENANCE_ADDED"] = 2
                self.assertEqual(Counter(e.event_type.value for e in record.events), expected)
                self.assertEqual(history.query_events(integration_id=record.integration_id), record.events)
                self.assertEqual(history.query_events(source_id=video_id), record.events)
                if index == 2:
                    self.assertEqual(after, before)
                    self.assertEqual(record.events[0].reason, "NO_CANONICAL_MUTATION")
                else:
                    self.assertNotEqual(after.reference, before.reference)
                    old_version = before.snapshot.documents[0].document_version if before.snapshot.documents else 0
                    self.assertEqual(after.snapshot.documents[0].document_version, old_version + 1)
        self.assertEqual({r.operations[0].kind for r in records}, set(kind))
        self.assertEqual([e for r in records for e in r.events], list(load_gold_history(self.gold).query_events()))
        inspected = open_gold_inspection(self.gold)
        survivor = inspected.get_gold_unit(identity()).value.unit
        retired = inspected.get_gold_unit(identity(2)).value.unit
        incompatible = inspected.get_gold_unit(identity(3)).value.unit
        self.assertEqual((survivor.content, incompatible.content), (merged, opposing))
        self.assertEqual((retired.status, retired.merged_into), (GoldStatus.MERGED, identity()))
        self.assertIn(edge(1, 3, GoldRelationKind.CONTRADICTS).relation, survivor.relations)
        self.assertEqual(len(load_gold_snapshot(self.gold).snapshot.documents[0].topics), 1)
        self.assertIn("KNOWLEDGE_MERGED", {e.event_type.value for e in load_gold_history(self.gold).query_events(knowledge_id=identity(2))})
        self.assertEqual({p.source_knowledge_artifact.source_id for p in survivor.provenance},
                         {"fixture-0", "fixture-1", "fixture-3", "fixture-4", "fixture-5", "fixture-7"})
        for knowledge in (survivor, retired, incompatible):
            for p in knowledge.provenance:
                self.assert_lineage(p, runs[p.source_knowledge_artifact.source_id])

        self.acquire_and_audit("empty", "Welcome to the channel.", empty=True)
        before = load_gold_snapshot(self.gold)
        empty = integrate_source_knowledge("empty", gold_config=self.gold, **self.source_configs).integration
        self.assertEqual(empty.operations, ())
        self.assertEqual(empty.rounds, ())
        self.assertEqual([(e.event_type.value, e.reason) for e in empty.events],
                         [("NO_CHANGE_RECORDED", "NO_SOURCE_KNOWLEDGE")])
        self.assertEqual(load_gold_snapshot(self.gold), before)
        self.assertEqual(load_gold_history(self.gold).integrations, (*records, empty))
        gold_bytes = {p: p.read_bytes() for p in self.gold.root.rglob("*") if p.is_file()}
        # Reopen and replay in a fresh interpreter with no semantic adapters.
        code = """
import sys
from pathlib import Path
from league_knowledge_platform import (GoldConfig, SectionValidationConfig, SegmentationConfig, SilverConfig,
    SourceAuditConfig, integrate_source_knowledge, load_gold_history, load_gold_snapshot)
root = Path(sys.argv[1])
gold = GoldConfig(root / 'gold')
view = load_gold_history(gold)
if view.gold_reference != load_gold_snapshot(gold).reference:
    raise AssertionError('Gold snapshot and history disagree after restart')
for video, identity in [('fixture-0', sys.argv[2]), ('empty', sys.argv[3])]:
    replay = integrate_source_knowledge(video, gold_config=gold, silver_config=SilverConfig(root / 'silver'),
        segmentation_config=SegmentationConfig(root / 'segments'), section_validation_config=SectionValidationConfig(root / 'validated'),
        config=SourceAuditConfig(root / 'source'))
    if not replay.reused or replay.integration != view.get_integration(identity):
        raise AssertionError(f'Replay changed the recorded integration for {video}')
if load_gold_history(gold) != view:
    raise AssertionError('Replay changed durable history')
"""
        subprocess.run([sys.executable, "-c", code, str(self.root), records[0].integration_id, empty.integration_id],
                       env=process_environment(), check=True, timeout=60)
        self.assertEqual(gold_bytes, {p: p.read_bytes() for p in self.gold.root.rglob("*") if p.is_file()})

    def test_failed_and_stale_commit_cannot_publish_partial_canonical_success(self):
        self.acquire_and_audit("stale", "Hold Charm against Zed.")
        self.acquire_and_audit("winner", "Hold Charm against an early dash.")
        stale = self.prepare("stale", self.create(1, "Hold Charm against Zed."))
        winner = self.prepare("winner", self.create(1, "Hold Charm against an early dash."))
        prior = load_gold_history(self.gold)
        prior_snapshot = load_gold_snapshot(self.gold)
        with patch.object(_gold_filesystem, "replace_selector", side_effect=OSError("fixture interruption")):
            with self.assertRaises(GoldHistoryError):
                self.commit(winner)
        self.assertEqual(load_gold_history(self.gold), prior)
        self.assertEqual(load_gold_snapshot(self.gold), prior_snapshot)
        committed = self.commit(winner)
        with self.assertRaises(GoldHistoryError) as caught:
            self.commit(stale)
        self.assertEqual(caught.exception.code, "STALE_PREPARED_INTEGRATION")
        self.assertEqual(load_gold_snapshot(self.gold).reference, committed.gold_reference)
        self.assertEqual(load_gold_history(self.gold).integrations, (committed.integration,))
