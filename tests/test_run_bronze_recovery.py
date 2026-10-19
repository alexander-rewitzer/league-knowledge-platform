"""Offline end-to-end handoff, shared-budget recovery and actual process crashes."""

from contextlib import closing
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from league_knowledge_platform import (
    AcquisitionRunService, BronzeConfig, BronzeFilesystemError, BronzeIntegrityError,
    BronzeIntegrationContext, BronzeRegistrationError, RunStage, RunStateError, RunStatus,
    RunTargetStatus, SelectionConfig, TargetFailure, TargetFailureKind,
    canonical_bronze_path, decode_bronze_artifact, initialize_run_repository,
    initialize_source_catalog, inspect_existing_bronze, integrate_selected_source,
    load_catalog, recover_existing_bronze,
)
from run_fixtures import CHAMPIONS, NOW, Captions, Discovery, Selection, process_environment, write_catalog


class RunBronzeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog_path = write_catalog(self.root / "catalog.json", CHAMPIONS[:1])
        self.catalog = load_catalog(self.catalog_path)
        self.repository = initialize_run_repository(self.root / "runs.sqlite3")
        self.sources = initialize_source_catalog(self.root / "sources.sqlite3")
        self.config = BronzeConfig(self.root / "bronze")

    def service(self, *, clock=None):
        return AcquisitionRunService(
            self.repository, discovery=Discovery(), catalog_reader=self.sources,
            caption_provider=Captions(), selection_capability=Selection(), bronze_config=self.config,
            selection_config=SelectionConfig(retry_delays_seconds=(0, 0)),
            clock=clock if clock is not None else (lambda: NOW),
            id_factory=lambda: "run",
        )

    def row(self):
        return self.repository.list_targets("run")[0]

    def seed_selected(self, *, count=1, bronze=False, registered=False):
        self.repository.create_run("run", self.catalog, NOW)
        self.repository.activate_run("run", NOW)
        for number in range(1, count):
            self.repository.begin_attempt("run", "Ahri", NOW)
            self.repository.finish_attempt("run", "Ahri", number, NOW, status=RunTargetStatus.FAILED,
                                           failure=TargetFailure(RunStage.DISCOVERY, TargetFailureKind.TIMEOUT))
        self.repository.begin_attempt("run", "Ahri", NOW)
        selected = self.service()._process_target(self.catalog.targets[0], NOW)
        self.repository.finish_attempt("run", "Ahri", count, NOW, status=RunTargetStatus.SELECTED,
                                       selected_video_id="AhriVideo", selection_method=selected.method)
        if bronze:
            catalog = self.sources if registered else Mock(wraps=self.sources)
            if not registered:
                catalog.register_integrated.side_effect = OSError("synthetic registration failure")
            try:
                integrate_selected_source(selected, catalog=catalog, config=self.config,
                                          context=BronzeIntegrationContext("run", NOW, self.catalog.targets[0]))
            except BronzeRegistrationError:
                if registered:
                    raise
        self.repository.finish_invocation("run", NOW)
        return selected

    def resume_without_pipeline(self, *, clock=None):
        service = self.service(clock=clock)
        with patch.object(service, "_process_target", side_effect=AssertionError("Unexpected acquisition")):
            summary = service.resume_run("run")
        self.assertFalse(service.discovery.calls)
        self.assertFalse(service.caption_provider.calls)
        self.assertFalse(service.selection_capability.quality_calls)
        return summary

    def test_original_selected_object_and_committed_selected_precede_real_bronze(self):
        service = self.service()
        selected = service._process_target(self.catalog.targets[0], NOW)
        def integrate(actual, **kwargs):
            self.assertIs(actual, selected)
            self.assertEqual((self.row().status, self.row().attempt_count), (RunTargetStatus.SELECTED, 1))
            self.assertEqual(kwargs["context"], BronzeIntegrationContext("run", NOW, self.catalog.targets[0]))
            with closing(sqlite3.connect(self.repository.path, timeout=0.1)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return integrate_selected_source(actual, **kwargs)
        with patch.object(service, "_process_target", return_value=selected), \
                patch("league_knowledge_platform.acquisition_runs.integrate_selected_source", side_effect=integrate):
            summary = service.start_new_run(self.catalog_path)
        self.assertEqual((summary.status, summary.selected, summary.integrated), (RunStatus.COMPLETED, 0, 1))
        self.assertEqual((self.row().status, self.row().attempt_count), (RunTargetStatus.INTEGRATED, 1))
        artifact = decode_bronze_artifact(canonical_bronze_path("AhriVideo", self.config).read_bytes())
        self.assertEqual(artifact["acquisition"]["run_id"], "run")
        self.assertEqual(artifact["transcript"]["text"], selected.source.text)
        self.assertNotIn(selected.source.text.encode(), self.repository.path.read_bytes())

    def test_no_selection_never_calls_bronze(self):
        service = self.service()
        service.caption_provider = Captions(unavailable=("AhriVideo",))
        with patch("league_knowledge_platform.acquisition_runs.integrate_selected_source",
                   side_effect=AssertionError("No winner")):
            summary = service.start_new_run(self.catalog_path)
        self.assertEqual((summary.status, summary.no_selection, summary.integrated), (RunStatus.COMPLETED, 1, 0))
        self.assertFalse(self.config.root.exists())

    def test_passive_repair_is_read_only_outside_run_state_even_at_three(self):
        self.seed_selected(count=3, bronze=True, registered=True)
        path = canonical_bronze_path("AhriVideo", self.config)
        content, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        catalog_bytes = self.sources.path.read_bytes()
        with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Passive repair")), \
                patch("league_knowledge_platform._bronze_filesystem.confirm_durable",
                      side_effect=AssertionError("Read-only inspection")):
            summary = self.resume_without_pipeline()
        self.assertEqual((summary.status, summary.integrated, self.row().attempt_count),
                         (RunStatus.COMPLETED, 1, 3))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (content, timestamp))
        self.assertEqual(self.sources.path.read_bytes(), catalog_bytes)

    def test_registration_only_claim_precedes_call_and_preserves_provenance(self):
        self.seed_selected(bronze=True)
        path = canonical_bronze_path("AhriVideo", self.config)
        before = path.read_bytes()
        register = self.sources.register_integrated
        def checked(video_id):
            self.assertEqual((self.row().status, self.row().attempt_count, self.row().selected_video_id),
                             (RunTargetStatus.RUNNING, 2, video_id))
            with closing(sqlite3.connect(self.repository.path, timeout=0.1)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return register(video_id)
        with patch.object(self.sources, "register_integrated", side_effect=checked) as calls:
            self.assertEqual(self.resume_without_pipeline().integrated, 1)
        calls.assert_called_once_with("AhriVideo")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.row().selected_at_utc, NOW)

    def test_repeated_registration_failures_exhaust_shared_budget_and_never_call_fourth(self):
        self.seed_selected(bronze=True)
        with patch.object(self.sources, "register_integrated", side_effect=OSError("secret failure")) as register:
            for count in (2, 3):
                summary = self.resume_without_pipeline()
                self.assertEqual(self.row().attempt_count, count)
                self.assertEqual(self.row().failure.stage, RunStage.SOURCE_REGISTRATION)
                self.assertEqual(self.row().selected_video_id, "AhriVideo")
            self.assertEqual(summary.status, RunStatus.COMPLETED_WITH_FAILURES)
            for _ in range(3):
                self.assertEqual(self.resume_without_pipeline(), summary)
            self.assertEqual(register.call_count, 2)
        self.assertNotIn(b"secret failure", self.repository.path.read_bytes())
        # An externally completed registration can still repair an exhausted projection.
        first_final = self.repository.get_run("run").finished_at_utc
        self.assertEqual(first_final, NOW)
        self.sources.register_integrated("AhriVideo")
        repair_time = NOW + timedelta(hours=3)
        times = iter(NOW + timedelta(hours=hours) for hours in (1, 2, 3, 4))
        repaired = self.resume_without_pipeline(clock=lambda: next(times))
        self.assertEqual(repaired.status, RunStatus.COMPLETED)
        self.assertEqual(self.repository.get_run("run").finished_at_utc, repair_time)
        self.assertEqual(self.row().attempt_count, 3)
        self.assertEqual(self.resume_without_pipeline(clock=lambda: NOW + timedelta(hours=5)), repaired)
        self.assertEqual(self.repository.get_run("run").finished_at_utc, repair_time)

    def test_passive_repair_refinalizes_when_other_exhausted_failure_remains(self):
        self.catalog_path = write_catalog(self.root / "catalog.json", CHAMPIONS[:2])
        self.catalog = load_catalog(self.catalog_path)
        self.seed_selected(count=3, bronze=True)
        for _ in range(3):
            self.repository.activate_run("run", NOW)
            attempt = self.repository.begin_attempt("run", "Aatrox", NOW)
            self.repository.finish_attempt(
                "run", "Aatrox", attempt.attempt_count, NOW, status=RunTargetStatus.FAILED,
                failure=TargetFailure(RunStage.DISCOVERY, TargetFailureKind.TIMEOUT),
            )
        self.repository.fail_integration(
            "run", "Ahri", 3, "AhriVideo", NOW,
            TargetFailure(RunStage.SOURCE_REGISTRATION, TargetFailureKind.REGISTRATION),
        )
        before = self.repository.summary("run")
        self.assertEqual((before.status, before.failed), (RunStatus.COMPLETED_WITH_FAILURES, 2))
        self.assertEqual(self.repository.get_run("run").finished_at_utc, NOW)

        self.sources.register_integrated("AhriVideo")
        repair_time = NOW + timedelta(hours=3)
        times = iter(NOW + timedelta(hours=hours) for hours in (1, 2, 3, 4))
        repaired = self.resume_without_pipeline(clock=lambda: next(times))
        self.assertEqual((repaired.status, repaired.failed, repaired.integrated),
                         (RunStatus.COMPLETED_WITH_FAILURES, 1, 1))
        self.assertEqual(self.repository.get_run("run").finished_at_utc, repair_time)
        targets = self.repository.list_targets("run")
        self.assertEqual([(target.status, target.attempt_count) for target in targets],
                         [(RunTargetStatus.INTEGRATED, 3), (RunTargetStatus.FAILED, 3)])
        self.assertEqual(self.resume_without_pipeline(clock=lambda: NOW + timedelta(hours=5)), repaired)
        self.assertEqual(self.repository.get_run("run").finished_at_utc, repair_time)

    def test_exhausted_selected_without_registration_never_activates_recovery(self):
        self.seed_selected(count=3, bronze=True)
        with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Fourth attempt")):
            result = self.resume_without_pipeline()
        self.assertEqual((result.status, result.failed), (RunStatus.COMPLETED_WITH_FAILURES, 1))
        self.assertEqual(self.row().attempt_count, 3)

    def test_missing_bronze_requires_new_whole_attempt_or_exhausted_failure(self):
        self.seed_selected()
        service = self.service()
        self.assertEqual(service.resume_run("run").integrated, 1)
        self.assertEqual(self.row().attempt_count, 2)
        self.assertEqual(len(service.discovery.calls), 1)

    def test_missing_bronze_at_three_never_calls_pipeline(self):
        self.seed_selected(count=3)
        result = self.resume_without_pipeline()
        self.assertEqual((result.status, self.row().attempt_count), (RunStatus.COMPLETED_WITH_FAILURES, 3))

    def test_corrupt_or_wrong_identity_bronze_is_global_and_not_overwritten(self):
        self.seed_selected(bronze=True)
        path = canonical_bronze_path("AhriVideo", self.config)
        artifact = decode_bronze_artifact(path.read_bytes())
        other = deepcopy(artifact)
        other["source"].update(video_id="Other", url="https://www.youtube.com/watch?v=Other")
        other["selection"]["winner_quality"]["video_id"] = "Other"
        for content in (b'{"partial":', json.dumps({**artifact, "schema_version": 999}).encode(),
                        json.dumps(other).encode()):
            with self.subTest(content=content[:25]):
                path.write_bytes(content)
                with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Invalid Bronze")):
                    with self.assertRaises(BronzeIntegrityError):
                        self.resume_without_pipeline()
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(self.row().attempt_count, 1)
                self.assertEqual(self.row().failure.stage, RunStage.BRONZE_VALIDATION)

    def test_catalog_registration_without_bronze_is_integrity_failure(self):
        self.seed_selected()
        self.sources.register_integrated("AhriVideo")
        with self.assertRaises(BronzeIntegrityError):
            self.resume_without_pipeline()

    def test_bronze_failure_keeps_identity_and_continues_later_champions(self):
        write_catalog(self.catalog_path)
        def integrate(selected, **kwargs):
            if selected.target_id == "Ahri":
                raise BronzeFilesystemError("AhriVideo", "publication")
            return integrate_selected_source(selected, **kwargs)
        service = self.service()
        with patch("league_knowledge_platform.acquisition_runs.integrate_selected_source", side_effect=integrate):
            result = service.start_new_run(self.catalog_path)
        self.assertEqual((result.status, result.integrated, result.failed), (RunStatus.RESUMABLE, 2, 1))
        self.assertEqual(self.row().selected_video_id, "AhriVideo")
        self.assertEqual(self.row().failure.stage, RunStage.BRONZE_PERSISTENCE)

    def test_normal_registration_failure_then_payload_free_resume(self):
        write_catalog(self.catalog_path)
        register = self.sources.register_integrated
        def failure_for_first(video_id):
            if video_id == "AhriVideo":
                raise OSError("synthetic registration failure")
            return register(video_id)
        service = self.service()
        with patch.object(self.sources, "register_integrated", side_effect=failure_for_first):
            summary = service.start_new_run(self.catalog_path)
        self.assertEqual((summary.integrated, summary.failed), (2, 1))
        self.assertEqual(self.row().failure.stage, RunStage.SOURCE_REGISTRATION)
        self.assertEqual(self.row().attempt_count, 1)
        before = canonical_bronze_path("AhriVideo", self.config).read_bytes()
        self.assertEqual(self.resume_without_pipeline().status, RunStatus.COMPLETED)
        self.assertEqual(self.row().attempt_count, 2)
        self.assertEqual(canonical_bronze_path("AhriVideo", self.config).read_bytes(), before)

    def test_integration_commit_failure_aborts_without_second_source_on_resume(self):
        service = self.service()
        with patch.object(self.repository, "finish_integration", side_effect=RunStateError("commit failed")):
            with self.assertRaises(RunStateError):
                service.start_new_run(self.catalog_path)
        self.assertEqual(self.row().status, RunTargetStatus.SELECTED)
        self.assertTrue(self.sources.is_integrated("AhriVideo"))
        self.assertEqual(self.resume_without_pipeline().integrated, 1)
        self.assertEqual(self.row().attempt_count, 1)

    def test_integrated_transaction_rollback_leaves_selected_for_passive_repair(self):
        self.seed_selected(bronze=True, registered=True)
        with patch.object(self.repository, "_settle", side_effect=RunStateError("result transaction failed")):
            with self.assertRaises(RunStateError):
                self.repository.finish_integration("run", "Ahri", 1, "AhriVideo", NOW)
        self.assertEqual(self.row().status, RunTargetStatus.SELECTED)
        self.assertEqual(self.resume_without_pipeline().integrated, 1)
        self.assertEqual(self.row().attempt_count, 1)

    def test_recovery_api_needs_no_payload_and_revalidates_before_registration(self):
        self.seed_selected(bronze=True)
        path = canonical_bronze_path("AhriVideo", self.config)
        before = path.read_bytes()
        self.assertFalse(inspect_existing_bronze("AhriVideo", catalog=self.sources, config=self.config).registered)
        with patch("league_knowledge_platform._bronze_filesystem.publish", side_effect=AssertionError("No rewrite")):
            self.assertTrue(recover_existing_bronze("AhriVideo", catalog=self.sources, config=self.config).registered)
            self.assertFalse(recover_existing_bronze("AhriVideo", catalog=self.sources, config=self.config).registered)
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b"corrupt after inspection")
        with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Validate again")):
            with self.assertRaises(BronzeIntegrityError):
                recover_existing_bronze("AhriVideo", catalog=self.sources, config=self.config)

    def crash(self, phase, operation="new"):
        script = r'''
import os, sys, sqlite3
from pathlib import Path
from league_knowledge_platform import (AcquisitionRunService, SQLiteRunRepository, SQLiteSourceCatalog,
    BronzeConfig, SelectionConfig, RunTargetStatus)
from league_knowledge_platform import _bronze_filesystem as fs
from run_fixtures import Captions, Discovery, Selection, NOW
root, phase, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if phase == 'before-recovery-commit':
    raw = sqlite3.connect
    class Connection(sqlite3.Connection):
        def commit(self):
            exists = self.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_run_targets'").fetchone()
            if exists and self.execute("SELECT 1 FROM acquisition_run_targets WHERE status='RUNNING' AND selected_video_id IS NOT NULL").fetchone():
                os._exit(81)
            return super().commit()
    def connect(*a, **kw): return raw(*a, **kw, factory=Connection)
    sqlite3.connect = connect
class Repository(SQLiteRunRepository):
    def finish_attempt(self, *a, **kw):
        if phase == 'before-selected' and kw['status'] is RunTargetStatus.SELECTED: os._exit(81)
        result = super().finish_attempt(*a, **kw)
        if phase == 'after-selected' and kw['status'] is RunTargetStatus.SELECTED: os._exit(81)
        return result
    def finish_integration(self, *a, **kw):
        if phase == 'before-integrated': os._exit(81)
        result = super().finish_integration(*a, **kw)
        if phase == 'after-integrated': os._exit(81)
        return result
if phase == 'during-write':
    def stage(directory, encoded_id, content):
        (directory / ('.bronze-tmp-' + encoded_id + '-' + 'a'*32 + '.tmp')).write_bytes(content[:20])
        os._exit(81)
    fs.stage = stage
if phase in ('before-publication', 'after-publication'):
    publish = fs.publish
    def interrupted(*a):
        if phase == 'after-publication': publish(*a)
        os._exit(81)
    fs.publish = interrupted
catalog = SQLiteSourceCatalog(root / 'sources.sqlite3')
if phase in ('before-registration', 'after-registration'):
    register = catalog.register_integrated
    def interrupted(video_id):
        if phase == 'after-registration': register(video_id)
        os._exit(81)
    catalog.register_integrated = interrupted
service = AcquisitionRunService(Repository(root/'runs.sqlite3'), discovery=Discovery(), catalog_reader=catalog,
    caption_provider=Captions(), selection_capability=Selection(), bronze_config=BronzeConfig(root/'bronze'),
    selection_config=SelectionConfig(retry_delays_seconds=(0,0)), clock=lambda:NOW, id_factory=lambda:'run')
if operation == 'new': service.start_new_run(root/'catalog.json')
else: service.resume_run('run')
'''
        return subprocess.run([sys.executable, "-B", "-c", script, str(self.root), phase, operation],
                              env=process_environment(), capture_output=True, text=True, timeout=30)

    def test_actual_crash_windows_preserve_budget_identity_and_durable_bytes(self):
        # Each subtest has an independent local store and one original target.
        for phase in ("before-selected", "after-selected", "during-write", "before-publication",
                      "after-publication", "before-registration", "after-registration",
                      "before-integrated", "after-integrated"):
            with self.subTest(phase=phase), TemporaryDirectory() as root:
                self.root = Path(root)
                self.catalog_path = write_catalog(self.root / "catalog.json", CHAMPIONS[:1])
                self.repository = initialize_run_repository(self.root / "runs.sqlite3")
                self.sources = initialize_source_catalog(self.root / "sources.sqlite3")
                self.config = BronzeConfig(self.root / "bronze")
                result = self.crash(phase)
                self.assertEqual(result.returncode, 81, result.stderr)
                path = canonical_bronze_path("AhriVideo", self.config)
                before = path.read_bytes() if path.exists() else None
                service = self.service()
                summary = service.resume_run("run")
                self.assertEqual(summary.status, RunStatus.COMPLETED)
                expected_count = 1 if phase in ("after-registration", "before-integrated", "after-integrated") else 2
                self.assertEqual(self.row().attempt_count, expected_count)
                if before is not None:
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(service.discovery.calls)
                else:
                    self.assertEqual(len(service.discovery.calls), 1)
                self.assertEqual(list(path.parent.glob("*.json")), [path])

    def test_repeated_process_death_in_registration_recovery_cannot_claim_fourth(self):
        self.seed_selected(bronze=True)
        for count in (2, 3):
            result = self.crash("before-registration", "resume")
            self.assertEqual(result.returncode, 81, result.stderr)
            self.assertEqual((self.row().status, self.row().attempt_count), (RunTargetStatus.RUNNING, count))
            self.assertEqual(self.row().selected_video_id, "AhriVideo")
        with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Fourth call")):
            self.assertEqual(self.resume_without_pipeline().status, RunStatus.COMPLETED_WITH_FAILURES)

    def test_registration_committed_on_third_recovery_claim_repairs_for_free(self):
        self.seed_selected(bronze=True)
        for phase in ("before-registration", "after-registration"):
            result = self.crash(phase, "resume")
            self.assertEqual(result.returncode, 81, result.stderr)
        self.assertEqual(self.row().attempt_count, 3)
        with patch.object(self.sources, "register_integrated", side_effect=AssertionError("Already registered")):
            self.assertEqual(self.resume_without_pipeline().integrated, 1)
        self.assertEqual(self.row().attempt_count, 3)

    def test_crash_before_registration_claim_commit_does_not_consume_attempt(self):
        self.seed_selected(bronze=True)
        result = self.crash("before-recovery-commit", "resume")
        self.assertEqual(result.returncode, 81, result.stderr)
        self.assertEqual((self.row().status, self.row().attempt_count), (RunTargetStatus.SELECTED, 1))
        self.assertEqual(self.resume_without_pipeline().integrated, 1)
        self.assertEqual(self.row().attempt_count, 2)
