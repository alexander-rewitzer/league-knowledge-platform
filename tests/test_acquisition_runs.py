"""Sequential orchestration, isolation, immutable resume inputs, and retry layers."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeConfig, AcquisitionRunService, CaptionProviderError, DiscoveryError, DiscoveryFailureKind,
    DiscoveryPage, RunBusyError, RunConfigurationError, RunNotFoundError, RunStage,
    RunStateError, RunStatus, RunTargetStatus, SelectionConfig, SourceCatalogError,
    SQLiteSourceCatalog, TargetFailureKind, TranscriptFailureKind,
    initialize_run_repository, initialize_source_catalog,
)
from run_fixtures import CHAMPIONS, NOW, Captions, Discovery, Selection, write_catalog


class AcquisitionRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = write_catalog(self.root / "catalog.json")
        self.repository = initialize_run_repository(self.root / "runs.sqlite3")
        self.sources = initialize_source_catalog(self.root / "sources.sqlite3")

    def service(self, **overrides):
        values = dict(discovery=Discovery(), catalog_reader=self.sources,
                      bronze_config=BronzeConfig(self.root / "bronze"),
                      caption_provider=Captions(), selection_capability=Selection(),
                      selection_config=SelectionConfig(retry_delays_seconds=(0, 0)), clock=lambda: NOW)
        values.update(overrides)
        return AcquisitionRunService(self.repository, **values)

    def test_new_runs_are_distinct_and_completed_resume_skips_all_providers(self):
        service = self.service()
        first = service.start_new_run(self.path)
        second = service.start_new_run(self.path)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual((first.status, first.integrated, first.total_targets), (RunStatus.COMPLETED, 3, 3))
        # Completed integrations are excluded globally on the next new run.
        self.assertEqual((second.integrated, second.no_selection), (0, 3))
        self.assertEqual(len(service.discovery.calls), 6)
        resumed = service.resume_run(first.run_id)
        self.assertEqual(resumed, first)
        self.assertEqual(len(service.discovery.calls), 6)
        self.assertEqual(len(self.sources.load_integrated_ids()), 3)

    def test_explicit_unknown_resume_never_creates_or_loads_a_catalog(self):
        service = self.service()
        with patch("league_knowledge_platform.acquisition_runs.load_catalog",
                   side_effect=AssertionError("Resume must not read catalogs")):
            with self.assertRaises(RunNotFoundError):
                service.resume_run("unknown")
        self.assertFalse(service.discovery.calls)
        with self.assertRaises(ValueError):
            service.resume_run("")

    def test_no_selection_domains_are_terminal_without_technical_failure(self):
        empty = DiscoveryPage("Ahri", (), None, True)
        service = self.service(discovery=Discovery({"Ahri": [empty]}),
                               caption_provider=Captions(unavailable=("AatroxVideo",)),
                               selection_capability=Selection(rejected=("AniviaVideo",)))
        summary = service.start_new_run(self.path)
        targets = self.repository.list_targets(summary.run_id)
        self.assertEqual((summary.status, summary.no_selection, summary.failed), (RunStatus.COMPLETED, 3, 0))
        self.assertEqual([t.no_selection_reason.value for t in targets],
                         ["NO_TRANSCRIPTS", "NO_TRANSCRIPTS", "ALL_REJECTED"])
        service.resume_run(summary.run_id)
        self.assertEqual(len(service.discovery.calls), 3)

    def test_failure_isolation_three_resumes_and_exhausted_skip(self):
        discovery = Discovery({"Ahri": [DiscoveryError(DiscoveryFailureKind.TIMEOUT, "secret-token")] * 3})
        service = self.service(discovery=discovery)
        first = service.start_new_run(self.path)
        self.assertEqual((first.status, first.failed, first.integrated, first.retryable_targets),
                         (RunStatus.RESUMABLE, 1, 2, 1))
        self.assertEqual([t.id for t, _ in discovery.calls], ["Ahri", "Aatrox", "Anivia"])
        second = service.resume_run(first.run_id)
        self.assertEqual((second.status, second.failed), (RunStatus.RESUMABLE, 1))
        third = service.resume_run(first.run_id)
        self.assertEqual((third.status, third.failed, third.integrated),
                         (RunStatus.COMPLETED_WITH_FAILURES, 1, 2))
        before = self.repository.list_targets(first.run_id)
        self.assertEqual([t.attempt_count for t in before], [3, 1, 1])
        self.assertEqual(before[0].failure.stage, RunStage.DISCOVERY)
        self.assertEqual(before[0].failure.kind, TargetFailureKind.TIMEOUT)
        self.assertEqual(service.resume_run(first.run_id), third)
        self.assertEqual(self.repository.list_targets(first.run_id), before)
        self.assertEqual(len(discovery.calls), 5)
        self.assertNotIn(b"secret-token", self.repository.path.read_bytes())

    def test_component_retries_count_once_and_do_not_restart_other_components(self):
        capability = Selection(fail_targets=("Ahri",))
        service = self.service(selection_capability=capability)
        summary = service.start_new_run(self.path)
        first = self.repository.list_targets(summary.run_id)[0]
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual((first.failure.stage, first.failure.kind),
                         (RunStage.QUALITY, TargetFailureKind.TIMEOUT))
        self.assertEqual(sum(r.target_id == "Ahri" for r in capability.quality_calls), 3)
        self.assertEqual(sum(t.id == "Ahri" for t, _ in service.discovery.calls), 1)
        self.assertEqual(service.caption_provider.calls.count("AhriVideo"), 1)
        self.assertEqual(summary.integrated, 2)

    def test_transcript_and_unexpected_target_errors_are_sanitized_and_isolated(self):
        for captions, discovery, stage, kind in (
            (Captions(failure=CaptionProviderError(TranscriptFailureKind.NETWORK)), Discovery(),
             RunStage.TRANSCRIPTS, TargetFailureKind.NETWORK),
            (Captions(), Discovery({"Ahri": [RuntimeError("raw-request-secret")]}),
             RunStage.COLLECTION, TargetFailureKind.UNEXPECTED_COMPONENT_EXCEPTION),
        ):
            with self.subTest(stage=stage):
                service = self.service(caption_provider=captions, discovery=discovery)
                summary = service.start_new_run(self.path)
                first = self.repository.list_targets(summary.run_id)[0]
                self.assertEqual((first.failure.stage, first.failure.kind), (stage, kind))
                self.assertEqual(len(discovery.calls), 3)
                self.assertNotIn(b"raw-request-secret", self.repository.path.read_bytes())

    def test_catalog_state_failure_aborts_globally_and_is_recoverable(self):
        service = self.service(id_factory=lambda: "global-failure")
        with patch.object(self.sources, "load_integrated_ids", side_effect=SourceCatalogError("corrupt")):
            with self.assertRaises(RunStateError):
                service.start_new_run(self.path)
        self.assertFalse(service.discovery.calls)
        self.assertEqual(self.repository.list_targets("global-failure")[0].status, RunTargetStatus.RUNNING)
        inspected = service.inspect_run("global-failure")
        self.assertEqual((inspected.status, inspected.failed, inspected.pending), (RunStatus.RESUMABLE, 1, 2))
        self.assertEqual(service.resume_run("global-failure").status, RunStatus.COMPLETED)

    def test_result_storage_failure_never_becomes_target_failure_or_continues(self):
        service = self.service(id_factory=lambda: "storage-failure")
        with patch.object(self.repository, "finish_attempt", side_effect=RunStateError("cannot commit")):
            with self.assertRaises(RunStateError):
                service.start_new_run(self.path)
        self.assertEqual(len(service.discovery.calls), 1)
        targets = self.repository.list_targets("storage-failure")
        self.assertEqual([t.status for t in targets],
                         [RunTargetStatus.RUNNING, RunTargetStatus.PENDING, RunTargetStatus.PENDING])

    def test_resume_uses_original_membership_metadata_and_totals_after_catalog_replacement(self):
        failures = {t["id"]: [DiscoveryError(DiscoveryFailureKind.SERVICE, "fixture")] for t in CHAMPIONS}
        old = self.service(discovery=Discovery(failures)).start_new_run(self.path)
        original = self.repository.get_run(old.run_id).catalog
        changed = [dict(CHAMPIONS[0], name="New display name", key="999"), CHAMPIONS[1],
                   {"id": "Brand", "key": "63", "name": "Brand"}]
        write_catalog(self.path, changed, "16.21.1")
        service = self.service()
        with patch("league_knowledge_platform.acquisition_runs.load_catalog",
                   side_effect=AssertionError("Resume read the current catalog")):
            resumed = service.resume_run(old.run_id)
        self.assertEqual(resumed.total_targets, 3)
        self.assertEqual([target for target, _ in service.discovery.calls], list(original.targets))
        self.assertEqual(self.repository.get_run(old.run_id).catalog, original)
        newer = service.start_new_run(self.path)
        self.assertEqual([t.target.name for t in self.repository.list_targets(newer.run_id)],
                         [t["name"] for t in changed])
        self.assertEqual(self.repository.get_run(newer.run_id).catalog.roster_version, "16.21.1")
        self.assertEqual(self.repository.get_run(old.run_id).catalog, original)

    def test_resume_requires_no_catalog_file_and_handles_noncontiguous_terminal_targets(self):
        service = self.service()
        run_id = "mixed"
        from league_knowledge_platform import NoSelectionReason, SelectionMethod, load_catalog
        self.repository.create_run(run_id, load_catalog(self.path), NOW)
        self.repository.activate_run(run_id, NOW)
        self.repository.begin_attempt(run_id, "Ahri", NOW)
        self.repository.finish_attempt(run_id, "Ahri", 1, NOW, status=RunTargetStatus.SELECTED,
                                       selected_video_id="selected", selection_method=SelectionMethod.ONLY_PASSING_CANDIDATE)
        self.repository.finish_integration(run_id, "Ahri", 1, "selected", NOW)
        self.repository.begin_attempt(run_id, "Anivia", NOW)
        self.repository.finish_attempt(run_id, "Anivia", 1, NOW, status=RunTargetStatus.NO_SELECTION,
                                       no_selection_reason=NoSelectionReason.ALL_REJECTED)
        self.repository.finish_invocation(run_id, NOW)
        self.path.unlink()
        resumed = service.resume_run(run_id)
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual([t.id for t, _ in service.discovery.calls], ["Aatrox"])

    def test_catalog_addition_and_removal_change_only_subsequent_new_run_totals(self):
        for changed in (
            (*CHAMPIONS, {"id": "Brand", "key": "63", "name": "Brand"}),
            CHAMPIONS[:2],
        ):
            with self.subTest(new_count=len(changed)):
                write_catalog(self.path)
                failures = {t["id"]: [DiscoveryError(DiscoveryFailureKind.TIMEOUT, "fixture")]
                            for t in CHAMPIONS}
                old = self.service(discovery=Discovery(failures)).start_new_run(self.path)
                write_catalog(self.path, changed, "16.21.1")
                service = self.service()
                resumed = service.resume_run(old.run_id)
                newer = service.start_new_run(self.path)
                self.assertEqual((resumed.total_targets, resumed.integrated + resumed.no_selection), (3, 3))
                self.assertEqual((newer.total_targets, newer.integrated + newer.no_selection), (len(changed), len(changed)))
                self.assertEqual([t.target.id for t in self.repository.list_targets(old.run_id)],
                                 [t["id"] for t in CHAMPIONS])

    def test_only_new_winners_are_registered_and_transcripts_stay_out_of_run_state(self):
        with closing(sqlite3.connect(self.sources.path)) as connection, connection:
            connection.execute("INSERT INTO integrated_sources VALUES (?,?)", ("AhriVideo", NOW.isoformat()))
        before = self.sources.path.read_bytes()
        service = self.service()
        with patch.object(self.sources, "register_integrated", wraps=self.sources.register_integrated) as registration:
            summary = service.start_new_run(self.path)
        self.assertEqual((summary.integrated, summary.no_selection), (2, 1))
        self.assertEqual([call.args[0] for call in registration.call_args_list], ["AatroxVideo", "AniviaVideo"])
        self.assertNotEqual(self.sources.path.read_bytes(), before)
        self.assertNotIn(b"Synthetic complete transcript", self.repository.path.read_bytes())
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {"catalog.json", "runs.sqlite3", "runs.sqlite3.lock", "sources.sqlite3", "bronze"})

    def test_invalid_catalog_and_configuration_fail_before_run_creation(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service().start_new_run(self.path)
        with closing(sqlite3.connect(self.repository.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM acquisition_runs").fetchone()[0], 0)
        with self.assertRaises(RunConfigurationError):
            self.service(caption_provider=object())
        with self.assertRaises(RunConfigurationError):
            self.service(catalog_reader=SQLiteSourceCatalog(self.repository.path))
        with self.repository.execution_guard():
            with self.assertRaises(RunBusyError):
                self.service().resume_run("any")


if __name__ == "__main__":
    unittest.main()
