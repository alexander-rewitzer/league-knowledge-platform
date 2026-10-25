"""Offline interoperability with real canonical source readers and complete lineage."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeConfig, GoldContractError, GoldContractState, GoldKnowledgeUnit,
    GoldProvenance, GoldReferenceError, GoldSourceArtifactReference, GoldStatus,
    SectionValidationConfig, SegmentationConfig, SilverConfig, SourceAuditConfig,
    SourceKnowledgeArtifact, SourceKnowledgeOutcome, TranscriptSegment,
    canonical_bronze_path, canonical_section_knowledge_path, canonical_segmentation_path,
    canonical_silver_path, construct_bronze_artifact, construct_silver_artifact,
    decode_bronze_artifact, decode_gold_state, decode_silver_artifact, encode_gold_state,
    load_source_knowledge, load_validated_section_knowledge, process_section_knowledge,
    process_source_knowledge, validate_gold_provenance, validate_gold_state,
    validate_silver_against_bronze,
)
from source_audit_fixtures import FakeAudit, FakeValidator, audit_response, validator_evaluation, workspace
from section_validation_fixtures import FakeExtractor, FakeJudge, evaluation, response
from segmentation_http_fixtures import bronze_bytes, proposal
from test_bronze import NOW, selected_source
from test_gold_models import reference, unit
from test_segmentation_artifacts import artifact as segmentation_artifact


def source_workspace(root, video_id):
    """Build real versioned artifacts using scripted semantics, with no live providers."""
    root = Path(root)
    configs = {"silver_config": SilverConfig(root / "silver"),
               "segmentation_config": SegmentationConfig(root / "sections"),
               "section_validation_config": SectionValidationConfig(root / "validated"),
               "config": SourceAuditConfig(root / "source-knowledge")}
    selected = selected_source(video_id)
    texts = ("Preserve Charm against Zed.", "Usually avoid wasting it.",
             "Missing Charm creates an engage window.", "Retain the explanation.", "Sponsor product")
    segments = tuple(TranscriptSegment(n * 3, text, text, Decimal(n), Decimal("0.7500"))
                     for n, text in enumerate(texts))
    selected = replace(selected, source=replace(selected.source, segments=segments))
    bronze = construct_bronze_artifact(selected, created_at_utc=NOW)
    bronze_path = canonical_bronze_path(video_id, BronzeConfig(root / "bronze"))
    silver = construct_silver_artifact(bronze, bronze_sha256=sha256(bronze_bytes(bronze)).hexdigest())
    segmented = segmentation_artifact(silver, proposal((0, 3, "CONTENT"), (6, 9, "CONTENT"), (12, 12, "ADVERTISEMENT")))
    for path, value in ((bronze_path, bronze), (canonical_silver_path(video_id, configs["silver_config"]), silver),
                        (canonical_segmentation_path(video_id, configs["segmentation_config"]), segmented)):
        path.parent.mkdir(parents=True)
        path.write_bytes(bronze_bytes(value))
    for n, statement, spans in ((1, texts[0], ((0, 3),)), (2, texts[2], ((6, 9),))):
        process_section_knowledge(video_id, f"section-{n:06d}", silver_config=configs["silver_config"],
            segmentation_config=configs["segmentation_config"], config=configs["section_validation_config"],
            extraction_capability=FakeExtractor([response(statement, spans=spans)]),
            judge_capability=FakeJudge([evaluation()]))
    result = process_source_knowledge(video_id, audit_capability=FakeAudit([audit_response()]),
                                     validator_capability=FakeValidator([validator_evaluation()]), **configs)
    return configs, result.path, bronze_path


def verified_context(video_id, configs, path):
    before = path.read_bytes()
    loaded = load_source_knowledge(video_id, **configs)
    if path.read_bytes() != before:
        raise AssertionError("Source changed while preparing test context")
    ref = GoldSourceArtifactReference("youtube", loaded.provenance.video_id,
        path.relative_to(configs["config"].root).as_posix(), sha256(before).hexdigest(),
        loaded.schema_version, loaded.source_audit_contract_version)
    return loaded, ref, {ref: frozenset(item.item_id for item in loaded.candidate.items)}


class GoldProvenanceTests(unittest.TestCase):
    def test_single_and_multiple_sources_resolve_through_actual_merged_source_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, path, _ = source_workspace(Path(directory) / "first", "A")
            other_configs, other_path, _ = source_workspace(Path(directory) / "second", "B")
            first, a, first_context = verified_context("A", configs, path)
            second, b, second_context = verified_context("B", other_configs, other_path)
            self.assertIsInstance(first, SourceKnowledgeArtifact)
            self.assertIsNot(type(first.candidate.items[0]), GoldKnowledgeUnit)
            p = (GoldProvenance(a, first.candidate.items[0].item_id), GoldProvenance(b, second.candidate.items[0].item_id))
            # Many source items may support one unit; one source item may also support several Gold units.
            state = GoldContractState(1, 1, (unit(provenance=p), unit(2, provenance=(p[0],))))
            before = {f: f.read_bytes() for f in Path(directory).rglob("*") if f.is_file()}
            from league_knowledge_platform import _bronze_filesystem as fs
            with patch.object(fs, "confirm_durable", side_effect=AssertionError), \
                    patch("socket.create_connection", side_effect=AssertionError), \
                    patch("sqlite3.connect", side_effect=AssertionError):
                validate_gold_provenance(state, source_items_by_artifact={**first_context, **second_context})
                self.assertEqual(decode_gold_state(encode_gold_state(state)), state)
                self.assertEqual(load_source_knowledge("A", **configs), first)
            self.assertEqual(before, {f: f.read_bytes() for f in Path(directory).rglob("*") if f.is_file()})

    def test_each_contributing_item_traces_to_section_evidence_and_exact_silver_and_bronze(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, path, bronze_path = source_workspace(directory, "A")
            loaded, ref, context = verified_context("A", configs, path)
            state = GoldContractState(1, 1, (unit(provenance=(GoldProvenance(ref, loaded.candidate.items[0].item_id),)),))
            validate_gold_provenance(state, source_items_by_artifact=context)
            silver_bytes = canonical_silver_path("A", configs["silver_config"]).read_bytes()
            segmented_bytes = canonical_segmentation_path("A", configs["segmentation_config"]).read_bytes()
            self.assertEqual(sha256(silver_bytes).hexdigest(), loaded.provenance.silver.sha256)
            self.assertEqual(sha256(segmented_bytes).hexdigest(), loaded.provenance.segmentation.sha256)
            self.assertEqual(sha256(bronze_path.read_bytes()).hexdigest(), loaded.provenance.bronze.sha256)
            silver = decode_silver_artifact(silver_bytes)
            bronze = decode_bronze_artifact(bronze_path.read_bytes())
            validate_silver_against_bronze(silver, bronze, bronze_sha256=sha256(bronze_path.read_bytes()).hexdigest())
            item = loaded.candidate.items[0]
            for contribution in item.derived_from:
                checkpoint = load_validated_section_knowledge("A", contribution.section_id,
                    silver_config=configs["silver_config"], segmentation_config=configs["segmentation_config"],
                    config=configs["section_validation_config"])
                source_item = next(i for i in checkpoint.accepted_proposal.items if i.item_id == contribution.item_id)
                self.assertTrue(all(e in item.evidence for e in source_item.evidence))
                checkpoint_ref = next(r for r in loaded.validated_sections if r.section_id == contribution.section_id)
                checkpoint_path = canonical_section_knowledge_path("A", contribution.section_id, configs["section_validation_config"])
                self.assertEqual(sha256(checkpoint_path.read_bytes()).hexdigest(), checkpoint_ref.sha256)
                for evidence in source_item.evidence:
                    indices = {s["source_index"] for s in silver["transcript"]["segments"]}
                    for span in evidence.spans:
                        self.assertIn(span.first_source_index, indices)
                        self.assertIn(span.last_source_index, indices)

    def test_structural_validity_does_not_certify_nonexistent_source_items_or_artifacts(self):
        state = GoldContractState(1, 1, (unit(),))
        validate_gold_state(state)
        for context in ({}, {reference(): frozenset()}, {reference(): frozenset({"different-item"})},
                        {reference(sha256="f" * 64): frozenset({"source-item-000001"})},
                        {reference(audit_contract_version=2): frozenset({"source-item-000001"})}):
            with self.subTest(context=context), self.assertRaises(GoldReferenceError):
                validate_gold_provenance(state, source_items_by_artifact=context)
        for context in (None, [], {reference(): []}, {reference(): frozenset({None})}, {"path": frozenset()}):
            with self.subTest(context=type(context)), self.assertRaises(GoldContractError):
                validate_gold_provenance(state, source_items_by_artifact=context)

    def test_accepted_empty_source_does_not_produce_placeholder_gold(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            result = process_source_knowledge("A", validator_capability=FakeValidator([validator_evaluation()]), **configs)
            loaded, ref, context = verified_context("A", configs, result.path)
            self.assertEqual(loaded.outcome, SourceKnowledgeOutcome.NO_SOURCE_KNOWLEDGE)
            validate_gold_provenance(GoldContractState(1, 1), source_items_by_artifact=context)
            self.assertEqual(context[ref], frozenset())
            with self.assertRaises(GoldReferenceError):
                validate_gold_provenance(GoldContractState(1, 1, (unit(provenance=(GoldProvenance(ref, "invented"),)),)),
                                         source_items_by_artifact=context)


if __name__ == "__main__":
    unittest.main()
