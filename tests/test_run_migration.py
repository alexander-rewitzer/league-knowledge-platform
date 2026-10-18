"""Known legacy SQLite fixture, explicit backup migration and rollback/resume tests."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    AcquisitionRunService, BronzeConfig, RunStateError, RunStatus, RunTargetStatus,
    SQLiteRunRepository,
    initialize_run_repository, initialize_source_catalog, load_catalog, migrate_run_repository_v1_to_v2,
)
from league_knowledge_platform.run_models import catalog_digest
from run_fixtures import NOW, Captions, Discovery, Selection, write_catalog

# Frozen merged v1 DDL, not generated from the production v2 schema.
V1_DDL = (
    """CREATE TABLE schema_metadata (component TEXT NOT NULL PRIMARY KEY COLLATE BINARY, version INTEGER NOT NULL CHECK (version > 0))""",
    """CREATE TABLE acquisition_runs (
        run_id TEXT NOT NULL PRIMARY KEY COLLATE BINARY,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE','RESUMABLE','COMPLETED','COMPLETED_WITH_FAILURES')),
        max_target_attempts INTEGER NOT NULL CHECK (max_target_attempts = 3),
        snapshot_target_count INTEGER NOT NULL CHECK (snapshot_target_count > 0),
        catalog_schema_version INTEGER NOT NULL,
        roster_version TEXT NOT NULL,
        catalog_generated_at_utc TEXT NOT NULL,
        source_provider TEXT NOT NULL,
        source_url TEXT NOT NULL,
        source_locale TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        CHECK ((status IN ('COMPLETED','COMPLETED_WITH_FAILURES')) =
               (finished_at_utc IS NOT NULL))
    )""",
    """CREATE TABLE acquisition_run_targets (
        run_id TEXT NOT NULL COLLATE BINARY REFERENCES acquisition_runs(run_id),
        champion_id TEXT NOT NULL COLLATE BINARY,
        champion_key TEXT NOT NULL COLLATE BINARY,
        champion_name TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','SELECTED','NO_SELECTION','FAILED')),
        attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 3),
        selected_video_id TEXT,
        selection_method TEXT CHECK (selection_method IN ('ONLY_PASSING_CANDIDATE','COMPARATIVE')),
        no_selection_reason TEXT CHECK (no_selection_reason IN ('NO_TRANSCRIPTS','ALL_REJECTED')),
        failure_stage TEXT CHECK (failure_stage IN ('DISCOVERY','COLLECTION','RANKING','TRANSCRIPTS','QUALITY','COMPARISON','ORCHESTRATION')),
        failure_kind TEXT CHECK (failure_kind IN ('CONFIGURATION','AUTHENTICATION','AUTHORIZATION','QUOTA','NETWORK','SERVICE','TIMEOUT','MALFORMED_RESPONSE','RATE_LIMIT','BLOCKED','PARSING','CONTRACT','INVALID_OUTPUT','CONTEXT_LIMIT','BALANCE','INVALID_REQUEST','PROVIDER_CONTRACT','INTERRUPTED','UNEXPECTED_COMPONENT_EXCEPTION')),
        first_started_at_utc TEXT,
        attempt_started_at_utc TEXT,
        updated_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        PRIMARY KEY (run_id, champion_id),
        UNIQUE (run_id, ordinal), UNIQUE (run_id, champion_key),
        CHECK ((status = 'PENDING') = (attempt_count = 0)),
        CHECK ((status = 'PENDING') = (first_started_at_utc IS NULL)),
        CHECK ((status = 'PENDING') = (attempt_started_at_utc IS NULL)),
        CHECK ((status IN ('SELECTED','NO_SELECTION','FAILED')) = (finished_at_utc IS NOT NULL)),
        CHECK ((status = 'SELECTED') = (selected_video_id IS NOT NULL)),
        CHECK (selected_video_id IS NULL OR length(selected_video_id) > 0),
        CHECK ((status = 'SELECTED') = (selection_method IS NOT NULL)),
        CHECK ((status = 'NO_SELECTION') = (no_selection_reason IS NOT NULL)),
        CHECK ((status = 'FAILED') = (failure_stage IS NOT NULL)),
        CHECK ((status = 'FAILED') = (failure_kind IS NOT NULL))
    )""",
    """CREATE TRIGGER run_context_immutable BEFORE UPDATE OF
        run_id, max_target_attempts, snapshot_target_count, catalog_schema_version,
        roster_version, catalog_generated_at_utc, source_provider, source_url,
        source_locale, snapshot_sha256, started_at_utc ON acquisition_runs
        BEGIN SELECT RAISE(ABORT, 'Immutable run context'); END""",
    """CREATE TRIGGER run_target_immutable BEFORE UPDATE OF
        run_id, champion_id, champion_key, champion_name, ordinal ON acquisition_run_targets
        BEGIN SELECT RAISE(ABORT, 'Immutable run target'); END""",
    """CREATE TRIGGER run_target_no_delete
        BEFORE DELETE ON acquisition_run_targets
        BEGIN SELECT RAISE(ABORT, 'Immutable run membership'); END""",
    """CREATE TRIGGER run_target_bounded_insert
        BEFORE INSERT ON acquisition_run_targets
        WHEN NEW.ordinal >= (SELECT snapshot_target_count FROM acquisition_runs
                             WHERE run_id = NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'Run target outside original snapshot'); END""",
)


def legacy_database(path, catalog, *, run_id="legacy", states=("SELECTED",) * 3, counts=(1, 2, 3)):
    timestamp = NOW.isoformat()
    if "RUNNING" in states:
        status = "ACTIVE"
    elif all(s in ("SELECTED", "NO_SELECTION") or s == "FAILED" and c == 3
             for s, c in zip(states, counts)):
        status = "COMPLETED_WITH_FAILURES" if "FAILED" in states else "COMPLETED"
    else:
        status = "RESUMABLE"
    with closing(sqlite3.connect(path)) as connection, connection:
        for sql in V1_DDL:
            connection.execute(sql)
        connection.execute("INSERT INTO schema_metadata VALUES ('acquisition_runs',1)")
        connection.execute("INSERT INTO acquisition_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run_id, status, 3, len(catalog.targets), catalog.schema_version, catalog.roster_version,
            catalog.generated_at_utc.isoformat(), catalog.source_provider, catalog.source_url,
            catalog.source_locale, catalog_digest(catalog), timestamp, timestamp,
            timestamp if status.startswith("COMPLETED") else None,
        ))
        for ordinal, (target, state, count) in enumerate(zip(catalog.targets, states, counts)):
            connection.execute("INSERT INTO acquisition_run_targets VALUES (" + ",".join("?" * 16) + ")", (
                run_id, target.id, target.key, target.name, ordinal, state, count,
                target.id + "Video" if state == "SELECTED" else None,
                "ONLY_PASSING_CANDIDATE" if state == "SELECTED" else None,
                "ALL_REJECTED" if state == "NO_SELECTION" else None,
                "DISCOVERY" if state == "FAILED" else None,
                "TIMEOUT" if state == "FAILED" else None,
                None if state == "PENDING" else timestamp,
                None if state == "PENDING" else timestamp, timestamp,
                timestamp if state in ("SELECTED", "NO_SELECTION", "FAILED") else None,
            ))


class RunMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "runs.sqlite3"
        self.backup = self.root / "runs-v1.backup.sqlite3"
        self.catalog_path = write_catalog(self.root / "catalog.json")
        self.catalog = load_catalog(self.catalog_path)
        legacy_database(self.path, self.catalog)

    def version(self, path=None):
        with closing(sqlite3.connect(path or self.path)) as connection:
            return connection.execute("SELECT version FROM schema_metadata").fetchone()[0]

    def test_v1_is_not_silently_migrated_and_backup_is_required(self):
        before = self.path.read_bytes()
        with self.assertRaises(RunStateError):
            initialize_run_repository(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(TypeError):
            migrate_run_repository_v1_to_v2(self.path)
        for backup in (self.path, *(self.path.with_name(self.path.name + suffix)
                                   for suffix in (".lock", "-journal", "-wal", "-shm"))):
            with self.assertRaises(RunStateError):
                migrate_run_repository_v1_to_v2(self.path, backup_path=backup)
        self.backup.write_bytes(b"existing backup")
        with self.assertRaises(RunStateError):
            migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.assertEqual(self.backup.read_bytes(), b"existing backup")
        self.assertEqual(self.version(), 1)

    def test_complete_legacy_selected_run_becomes_unresolved_without_resetting_counts(self):
        repository = migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.assertEqual((self.version(), self.version(self.backup)), (2, 1))
        self.assertEqual(repository.get_run("legacy").catalog, self.catalog)
        self.assertEqual(repository.summary("legacy").status, RunStatus.RESUMABLE)
        rows = repository.list_targets("legacy")
        self.assertEqual([t.attempt_count for t in rows], [1, 2, 3])
        self.assertEqual([t.selected_video_id for t in rows], [t.id + "Video" for t in self.catalog.targets])
        self.assertTrue(all(t.status is RunTargetStatus.SELECTED and not t.terminal
                            and t.finished_at_utc is None and t.selected_at_utc == NOW for t in rows))
        with closing(sqlite3.connect(self.backup)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM acquisition_runs").fetchone()[0], "COMPLETED")
        with self.assertRaises(RunStateError):
            migrate_run_repository_v1_to_v2(self.path, backup_path=self.root / "another.sqlite3")
        self.assertFalse((self.root / "another.sqlite3").exists())

    def test_migrated_selected_without_bronze_uses_remaining_budget_only(self):
        repository = migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.catalog_path.unlink()
        service = AcquisitionRunService(
            repository, discovery=Discovery(), catalog_reader=initialize_source_catalog(self.root / "sources.sqlite3"),
            caption_provider=Captions(), selection_capability=Selection(),
            bronze_config=BronzeConfig(self.root / "bronze"), clock=lambda: NOW,
        )
        result = service.resume_run("legacy")
        self.assertEqual((result.status, result.integrated, result.failed),
                         (RunStatus.COMPLETED_WITH_FAILURES, 2, 1))
        self.assertEqual([t.attempt_count for t in repository.list_targets("legacy")], [2, 3, 3])
        self.assertEqual([t.id for t, _ in service.discovery.calls], ["Ahri", "Aatrox"])
        self.assertEqual(repository.get_run("legacy").catalog, self.catalog)

    def test_legacy_pending_running_no_selection_and_failure_remain_interpretable(self):
        for states, counts, expected in (
            (("PENDING", "RUNNING", "FAILED"), (0, 1, 2), RunStatus.ACTIVE),
            (("NO_SELECTION",) * 3, (1, 1, 1), RunStatus.COMPLETED),
            (("FAILED",) * 3, (3, 3, 3), RunStatus.COMPLETED_WITH_FAILURES),
            (("SELECTED", "NO_SELECTION", "FAILED"), (2, 1, 1), RunStatus.RESUMABLE),
        ):
            with self.subTest(states=states), TemporaryDirectory() as directory:
                path = Path(directory) / "runs.sqlite3"
                legacy_database(path, self.catalog, states=states, counts=counts)
                repository = migrate_run_repository_v1_to_v2(path, backup_path=Path(directory) / "backup.sqlite3")
                self.assertEqual(repository.summary("legacy").status, expected)
                self.assertEqual([t.status.value for t in repository.list_targets("legacy")], list(states))
                self.assertEqual([t.attempt_count for t in repository.list_targets("legacy")], list(counts))
                if "RUNNING" in states:
                    repository.recover_run("legacy", NOW)
                    self.assertEqual(repository.list_targets("legacy")[1].attempt_count, 1)

    def test_transactional_ddl_failure_keeps_original_and_valid_backup(self):
        before = self.path.read_bytes()
        with patch.object(SQLiteRunRepository, "_settle", side_effect=RunStateError("injected migration failure")):
            with self.assertRaises(RunStateError):
                migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.version(self.backup), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM acquisition_run_targets").fetchone()[0], 3)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_run_targets_v1'").fetchone())
        # A fresh explicit invocation can migrate the preserved original.
        self.assertEqual(migrate_run_repository_v1_to_v2(
            self.path, backup_path=self.root / "retry-backup.sqlite3").summary("legacy").selected, 3)

    def test_unknown_version_or_changed_snapshot_fails_before_backup_or_migration(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE schema_metadata SET version=999")
        with self.assertRaises(RunStateError):
            migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE schema_metadata SET version=1")
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='run_target_immutable'").fetchone()[0]
            connection.execute("DROP TRIGGER run_target_immutable")
            connection.execute("UPDATE acquisition_run_targets SET champion_name='tampered' WHERE ordinal=0")
            connection.execute(sql)
        with self.assertRaises(RunStateError):
            migrate_run_repository_v1_to_v2(self.path, backup_path=self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.version(), 1)

    def test_explicit_indexes_and_views_are_rejected_without_source_changes(self):
        index = "CREATE INDEX custom_target_status ON acquisition_run_targets(status)"
        view = "CREATE VIEW custom_target_view AS SELECT champion_id,status FROM acquisition_run_targets"
        for additions in ((index,), (view,), (index, view)):
            with self.subTest(additions=additions), TemporaryDirectory() as directory:
                path = Path(directory) / "runs.sqlite3"
                backup = Path(directory) / "backup.sqlite3"
                legacy_database(path, self.catalog)
                with closing(sqlite3.connect(path)) as connection, connection:
                    for statement in additions:
                        connection.execute(statement)
                before = path.read_bytes()
                with self.assertRaises(RunStateError):
                    migrate_run_repository_v1_to_v2(path, backup_path=backup)
                self.assertFalse(backup.exists())
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.version(path), 1)
                with closing(sqlite3.connect(path)) as connection:
                    if index in additions:
                        self.assertIsNotNone(connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='custom_target_status'"
                        ).fetchone())
                    if view in additions:
                        self.assertEqual(connection.execute("SELECT count(*) FROM custom_target_view").fetchone()[0], 3)
