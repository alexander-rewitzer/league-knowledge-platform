"""Real application composition and real process termination, entirely offline."""

from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    BronzeConfig, AcquisitionRunService, RunStatus, RunTargetStatus, SelectionConfig, SQLiteRunRepository,
    YouTubeDiscovery, initialize_run_repository, initialize_source_catalog,
)
from run_fixtures import NOW, Captions, Discovery, Selection, process_environment, write_catalog


class FixtureTransport:
    def __init__(self, state_path):
        self.state_path = state_path
        self.calls = []
        self.failed_once = False
        self.unlocked_calls = 0

    def get_json(self, endpoint, params, timeout):
        self.calls.append((endpoint, dict(params)))
        # A second writer can obtain a transaction during every external call.
        with closing(sqlite3.connect(self.state_path, timeout=0.1)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE acquisition_runs SET updated_at_utc=updated_at_utc")
            connection.rollback()
            self.unlocked_calls += 1
        if endpoint.endswith("/search"):
            target = params["q"].removeprefix("League of Legends ").removesuffix(" guide")
            if target == "Ahri" and not self.failed_once:
                self.failed_once = True
                raise TimeoutError("Synthetic discovery failure")
            ids = (["integrated", target + "_0"] if "pageToken" not in params
                   else [target + "_0", target + "_1", target + "_2"])
            response = {"items": [{"id": {"videoId": video_id}} for video_id in ids]}
            if "pageToken" not in params:
                response["nextPageToken"] = "next-page"
            return response
        if endpoint.endswith("/videos"):
            return {"items": [{"id": video_id, "snippet": {"title": "Synthetic guide",
                                "publishedAt": "2026-10-01T12:00:00Z"},
                                "statistics": {"viewCount": "1000"},
                                "contentDetails": {"duration": "PT10M"}}
                               for video_id in params["id"].split(",")]}
        raise AssertionError("Unexpected endpoint; no live fallback")


class RunIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = write_catalog(self.root / "catalog.json")
        self.repository = initialize_run_repository(self.root / "runs.sqlite3")
        self.sources = initialize_source_catalog(self.root / "sources.sqlite3")

    def service(self, **overrides):
        values = dict(discovery=Discovery(), catalog_reader=self.sources,
                      bronze_config=BronzeConfig(self.root / "bronze"),
                      caption_provider=Captions(), selection_capability=Selection(),
                      selection_config=SelectionConfig(retry_delays_seconds=(0, 0)), clock=lambda: NOW)
        values.update(overrides)
        return AcquisitionRunService(self.repository, **values)

    def test_real_discovery_collection_ranking_caption_selection_persistence_and_resume(self):
        with closing(sqlite3.connect(self.sources.path)) as connection, connection:
            connection.execute("INSERT INTO integrated_sources VALUES (?,?)", ("integrated", NOW.isoformat()))
        original_ids = self.sources.load_integrated_ids()
        transport = FixtureTransport(self.repository.path)
        selection = Selection()
        captions = Captions(unavailable=("Aatrox_1", "Anivia_1", "Ahri_1"))
        service = self.service(discovery=YouTubeDiscovery(api_key="offline-key", transport=transport),
                               caption_provider=captions, selection_capability=selection)
        with patch("socket.create_connection", side_effect=AssertionError("Live access forbidden")):
            first = service.start_new_run(self.catalog)
            self.assertEqual((first.status, first.integrated, first.failed), (RunStatus.RESUMABLE, 2, 1))
            self.catalog.unlink()
            second = service.resume_run(first.run_id)
        self.assertEqual((second.status, second.integrated, second.failed), (RunStatus.COMPLETED, 3, 0))
        self.assertEqual(transport.unlocked_calls, len(transport.calls))
        targets = self.repository.list_targets(first.run_id)
        self.assertEqual([t.attempt_count for t in targets], [2, 1, 1])
        self.assertEqual([t.selected_video_id for t in targets], ["Ahri_0", "Aatrox_0", "Anivia_0"])
        self.assertEqual(len(selection.comparison_calls), 3)
        for request in selection.quality_calls:
            self.assertEqual(len(request.candidates), 2)
            self.assertNotIn("integrated", [c.video_id for c in request.candidates])
            self.assertTrue(all("Synthetic complete transcript" in c.transcript_text for c in request.candidates))
        self.assertEqual(self.sources.load_integrated_ids(), original_ids | {"Ahri_0", "Aatrox_0", "Anivia_0"})
        self.assertNotIn(b"Synthetic complete transcript", self.repository.path.read_bytes())
        self.assertNotIn(b"offline-key", self.repository.path.read_bytes())

    def crash(self, phase, operation="new"):
        code = """
import os, sys
import sqlite3
from pathlib import Path
from league_knowledge_platform import (AcquisitionRunService, SQLiteRunRepository,
    SQLiteSourceCatalog, SelectionConfig, RunTargetStatus, BronzeConfig)
from run_fixtures import Captions, Discovery, Selection, NOW
phase, operation, database, sources, catalog = sys.argv[1:]
if phase == 'before-running-commit':
    raw_connect = sqlite3.connect
    class CrashBeforeCommit(sqlite3.Connection):
        def commit(self):
            exists = self.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_run_targets'").fetchone()
            if exists and self.execute("SELECT 1 FROM acquisition_run_targets WHERE status='RUNNING'").fetchone():
                os._exit(76)
            return super().commit()
    def crash_connection(*args, **kwargs):
        return raw_connect(*args, **kwargs, factory=CrashBeforeCommit)
    sqlite3.connect = crash_connection
class Repository(SQLiteRunRepository):
    def finish_attempt(self, *args, **kwargs):
        if phase == 'before-result':
            assert kwargs['status'] is RunTargetStatus.SELECTED
            print('SELECTION_FINISHED', flush=True)
            os._exit(74)
        result = super().finish_attempt(*args, **kwargs)
        if phase == 'after-result':
            os._exit(75)
        return result
def interrupt(target):
    if phase == 'after-running':
        os._exit(73)
service = AcquisitionRunService(Repository(database), discovery=Discovery(hook=interrupt),
    catalog_reader=SQLiteSourceCatalog(sources), caption_provider=Captions(),
    bronze_config=BronzeConfig(Path(sources).parent / "bronze"),
    selection_capability=Selection(), selection_config=SelectionConfig(retry_delays_seconds=(0,0)),
    clock=lambda: NOW, id_factory=lambda:'crash-run')
if operation == 'new':
    service.start_new_run(catalog)
else:
    service.resume_run('crash-run')
"""
        return subprocess.run(
            [sys.executable, "-B", "-c", code, phase, operation, str(self.repository.path),
             str(self.sources.path), str(self.catalog)], env=process_environment(),
            capture_output=True, text=True, timeout=30,
        )

    def test_actual_repeated_process_death_never_exceeds_three_attempts(self):
        for attempt in (1, 2, 3):
            process = self.crash("after-running", "new" if attempt == 1 else "resume")
            self.assertEqual(process.returncode, 73, process.stderr)
            target = SQLiteRunRepository(self.repository.path).list_targets("crash-run")[0]
            self.assertEqual((target.status, target.attempt_count), (RunTargetStatus.RUNNING, attempt))
        service = self.service()
        self.catalog.unlink()
        result = service.resume_run("crash-run")
        self.assertEqual((result.status, result.failed, result.integrated),
                         (RunStatus.COMPLETED_WITH_FAILURES, 1, 2))
        self.assertEqual([t.id for t, _ in service.discovery.calls], ["Aatrox", "Anivia"])
        saved = self.repository.list_targets("crash-run")[0]
        self.assertEqual((saved.attempt_count, saved.failure.kind.value), (3, "INTERRUPTED"))
        self.assertEqual(service.resume_run("crash-run"), result)

    def test_crash_before_running_commit_preserves_pending_and_zero_attempts(self):
        process = self.crash("before-running-commit")
        self.assertEqual(process.returncode, 76, process.stderr)
        saved = self.repository.list_targets("crash-run")[0]
        self.assertEqual((saved.status, saved.attempt_count), (RunTargetStatus.PENDING, 0))
        service = self.service()
        self.assertEqual(service.resume_run("crash-run").status, RunStatus.COMPLETED)
        self.assertEqual([t.attempt_count for t in self.repository.list_targets("crash-run")], [1, 1, 1])

    def test_crash_after_external_success_before_result_commit_retries_whole_target(self):
        process = self.crash("before-result")
        self.assertEqual(process.returncode, 74, process.stderr)
        self.assertIn("SELECTION_FINISHED", process.stdout)
        saved = self.repository.list_targets("crash-run")[0]
        self.assertEqual((saved.status, saved.attempt_count), (RunTargetStatus.RUNNING, 1))
        self.assertIsNone(saved.selected_video_id)
        service = self.service()
        self.assertEqual(service.resume_run("crash-run").status, RunStatus.COMPLETED)
        self.assertEqual([t.id for t, _ in service.discovery.calls], ["Ahri", "Aatrox", "Anivia"])
        self.assertEqual([t.attempt_count for t in self.repository.list_targets("crash-run")], [2, 1, 1])

    def test_crash_after_selected_commit_without_bronze_retries_target_on_restart(self):
        process = self.crash("after-result")
        self.assertEqual(process.returncode, 75, process.stderr)
        before = self.repository.list_targets("crash-run")[0]
        self.assertEqual(before.status, RunTargetStatus.SELECTED)
        service = self.service()
        self.assertEqual(service.resume_run("crash-run").status, RunStatus.COMPLETED)
        self.assertEqual([t.id for t, _ in service.discovery.calls], ["Ahri", "Aatrox", "Anivia"])
        self.assertEqual(self.repository.list_targets("crash-run")[0].attempt_count, 2)


if __name__ == "__main__":
    unittest.main()
