"""Actual SQLite transactions, immutable snapshots, recovery and integrity checks."""

from dataclasses import FrozenInstanceError
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    RunNotFoundError, RunStage, RunStateError, RunStatus, RunTargetStatus, SelectionMethod,
    NoSelectionReason, SQLiteRunRepository, TargetFailure, TargetFailureKind,
    initialize_run_repository, initialize_source_catalog, load_catalog,
)
from run_fixtures import NOW, write_catalog


class RunRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "runs.sqlite3"
        self.catalog = load_catalog(write_catalog(self.root / "catalog.json"))
        self.repository = initialize_run_repository(self.path)

    def create(self, run_id="run-one"):
        self.repository.create_run(run_id, self.catalog, NOW)
        return run_id

    def begin(self, run_id="run-one", target_id="Ahri"):
        self.repository.activate_run(run_id, NOW)
        return self.repository.begin_attempt(run_id, target_id, NOW)

    def record_failure(self, attempt, kind=TargetFailureKind.TIMEOUT):
        self.repository.finish_attempt(attempt.run_id, attempt.target.id, attempt.attempt_count,
                                       NOW, status=RunTargetStatus.FAILED,
                                       failure=TargetFailure(RunStage.DISCOVERY, kind))

    def test_constructor_is_inert_and_initialization_is_explicit_idempotent(self):
        path = self.root / "new" / "runs.sqlite3"
        SQLiteRunRepository(path)
        self.assertFalse(path.parent.exists())
        self.repository.initialize()
        with self.assertRaises(ValueError):
            SQLiteRunRepository(":memory:")
        for timeout in (0, -1, True, float("nan")):
            with self.assertRaises(ValueError):
                SQLiteRunRepository(self.path, timeout_seconds=timeout)

    def test_snapshot_and_pending_records_survive_repository_recreation(self):
        run_id = self.create()
        repository = SQLiteRunRepository(self.path)
        run = repository.get_run(run_id)
        self.assertEqual(run.catalog, self.catalog)
        self.assertEqual(run.max_target_attempts, 3)
        self.assertEqual(run.status, RunStatus.RESUMABLE)
        targets = repository.list_targets(run_id)
        self.assertEqual(tuple(t.target for t in targets), self.catalog.targets)
        self.assertTrue(all(t.status is RunTargetStatus.PENDING and t.attempt_count == 0 for t in targets))
        self.assertEqual(repository.summary(run_id).total_targets, 3)
        with self.assertRaises(FrozenInstanceError):
            targets[0].target.name = "changed"

    def test_current_committed_roster_is_copied_completely(self):
        catalog = load_catalog()
        self.repository.create_run("committed-roster", catalog, NOW)
        self.assertEqual(tuple(t.target for t in self.repository.list_targets("committed-roster")),
                         catalog.targets)
        self.assertEqual(self.repository.summary("committed-roster").total_targets, len(catalog.targets))

    def test_run_creation_rolls_back_if_complete_snapshot_validation_fails(self):
        with patch.object(self.repository, "_load", side_effect=RunStateError("injected validation")):
            with self.assertRaises(RunStateError):
                self.create()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM acquisition_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM acquisition_run_targets").fetchone()[0], 0)

    def test_context_membership_and_target_fields_are_sql_immutable(self):
        self.create()
        statements = (
            "UPDATE acquisition_run_targets SET champion_name='changed'",
            "UPDATE acquisition_run_targets SET champion_id='changed'",
            "UPDATE acquisition_run_targets SET ordinal=99",
            "DELETE FROM acquisition_run_targets",
            "UPDATE acquisition_runs SET roster_version='99.1.1'",
            "UPDATE acquisition_runs SET snapshot_target_count=99",
            "UPDATE acquisition_runs SET snapshot_sha256='changed'",
            "INSERT INTO acquisition_run_targets (run_id,champion_id,champion_key,champion_name,"
            "ordinal,status,attempt_count,updated_at_utc) VALUES ('run-one','New','1','New',3,'PENDING',0,'now')",
        )
        for statement in statements:
            with self.subTest(statement=statement), closing(sqlite3.connect(self.path)) as connection, connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
        self.assertEqual(self.repository.get_run("run-one").catalog, self.catalog)

    def test_removed_schema_protection_or_changed_snapshot_fails_explicitly(self):
        self.create()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='run_target_immutable'").fetchone()[0]
            connection.execute("DROP TRIGGER run_target_immutable")
            connection.execute("UPDATE acquisition_run_targets SET champion_name='changed' WHERE ordinal=0")
            connection.execute(sql)
        with self.assertRaisesRegex(RunStateError, "snapshot"):
            self.repository.get_run("run-one")

    def test_attempt_claim_and_result_require_exact_running_attempt(self):
        self.create()
        attempt = self.begin()
        self.assertEqual((attempt.status, attempt.attempt_count), (RunTargetStatus.RUNNING, 1))
        with self.assertRaises(RunStateError):
            self.repository.begin_attempt("run-one", "Aatrox", NOW)
        with self.assertRaises(RunStateError):
            self.repository.finish_attempt("run-one", "Ahri", 2, NOW,
                                           status=RunTargetStatus.NO_SELECTION,
                                           no_selection_reason=NoSelectionReason.NO_TRANSCRIPTS)
        self.repository.finish_attempt("run-one", "Ahri", 1, NOW + timedelta(seconds=1),
                                       status=RunTargetStatus.SELECTED, selected_video_id="video-A",
                                       selection_method=SelectionMethod.ONLY_PASSING_CANDIDATE)
        saved = self.repository.list_targets("run-one")[0]
        self.assertEqual(saved.selected_video_id, "video-A")
        self.assertIsNone(saved.finished_at_utc)
        self.assertEqual(saved.selected_at_utc, NOW + timedelta(seconds=1))
        self.assertFalse(saved.terminal)
        with self.assertRaises(RunStateError):
            self.repository.begin_attempt("run-one", "Ahri", NOW)

    def test_result_transaction_rolls_back_without_false_success(self):
        self.create()
        self.begin()
        with patch.object(self.repository, "_settle", side_effect=RunStateError("injected write failure")):
            with self.assertRaises(RunStateError):
                self.repository.finish_attempt("run-one", "Ahri", 1, NOW,
                                               status=RunTargetStatus.SELECTED, selected_video_id="video",
                                               selection_method=SelectionMethod.ONLY_PASSING_CANDIDATE)
        target = self.repository.list_targets("run-one")[0]
        self.assertEqual(target.status, RunTargetStatus.RUNNING)
        self.assertIsNone(target.selected_video_id)

    def test_interruption_consumes_budget_and_recovery_does_not_increment(self):
        self.create()
        for expected in (1, 2, 3):
            attempt = self.begin()
            self.assertEqual(attempt.attempt_count, expected)
            summary = self.repository.recover_run("run-one", NOW)
            target = self.repository.list_targets("run-one")[0]
            self.assertEqual(target.attempt_count, expected)
            self.assertEqual(target.failure, TargetFailure(RunStage.ORCHESTRATION,
                                                         TargetFailureKind.INTERRUPTED))
            self.assertEqual(summary.retryable_targets, int(expected < 3))
        with self.assertRaises(RunStateError):
            self.begin()

    def test_failed_roundtrip_and_exhausted_metadata_are_preserved(self):
        self.create()
        for expected in (1, 2, 3):
            attempt = self.begin()
            self.assertEqual(attempt.attempt_count, expected)
            self.record_failure(attempt)
            self.repository.finish_invocation("run-one", NOW)
        before = self.repository.list_targets("run-one")[0]
        self.repository.recover_run("run-one", NOW + timedelta(days=1))
        self.assertEqual(before, self.repository.list_targets("run-one")[0])
        self.assertEqual(before.failure.kind, TargetFailureKind.TIMEOUT)

    def test_summary_and_terminal_completion_are_derived_and_idempotent(self):
        self.create()
        self.repository.activate_run("run-one", NOW)
        for index, target in enumerate(self.catalog.targets):
            self.repository.begin_attempt("run-one", target.id, NOW)
            if index == 0:
                self.repository.finish_attempt("run-one", target.id, 1, NOW,
                                               status=RunTargetStatus.SELECTED, selected_video_id="shared",
                                               selection_method=SelectionMethod.ONLY_PASSING_CANDIDATE)
            else:
                self.repository.finish_attempt("run-one", target.id, 1, NOW,
                                               status=RunTargetStatus.NO_SELECTION,
                                               no_selection_reason=NoSelectionReason.ALL_REJECTED)
        summary = self.repository.finish_invocation("run-one", NOW)
        self.assertEqual((summary.status, summary.selected, summary.no_selection, summary.total_targets),
                         (RunStatus.RESUMABLE, 1, 2, 3))
        self.repository.finish_integration("run-one", "Ahri", 1, "shared", NOW)
        summary = self.repository.finish_invocation("run-one", NOW)
        self.assertEqual((summary.status, summary.selected, summary.integrated), (RunStatus.COMPLETED, 0, 1))
        before = self.repository.get_run("run-one")
        self.assertEqual(self.repository.recover_run("run-one", NOW + timedelta(days=1)), summary)
        self.assertEqual(self.repository.get_run("run-one"), before)

    def test_unknown_run_schema_version_and_source_catalog_adoption_are_rejected(self):
        with self.assertRaises(RunNotFoundError):
            self.repository.get_run("missing")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE schema_metadata SET version=999")
        with self.assertRaises(RunStateError):
            self.repository.initialize()
        source_path = self.root / "sources.sqlite3"
        source = initialize_source_catalog(source_path)
        with closing(sqlite3.connect(source_path)) as connection, connection:
            connection.execute("INSERT INTO integrated_sources VALUES (?,?)",
                               ("already-integrated", NOW.isoformat()))
        before = source_path.read_bytes()
        with self.assertRaises(RunStateError):
            initialize_run_repository(source_path)
        self.assertEqual(before, source_path.read_bytes())
        self.assertTrue(source.is_integrated("already-integrated"))

    def test_unreadable_corrupt_state_and_invalid_progress_are_global_errors(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a SQLite database")
        with self.assertRaises(RunStateError):
            SQLiteRunRepository(corrupt).summary("run-one")
        self.create()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE acquisition_run_targets SET updated_at_utc='invalid'")
        with self.assertRaises(RunStateError):
            self.repository.summary("run-one")


if __name__ == "__main__":
    unittest.main()
