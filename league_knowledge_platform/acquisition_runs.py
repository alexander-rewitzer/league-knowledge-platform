"""Sequential run orchestration over existing discovery-to-selection components."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from .defaults import MAX_TARGET_ATTEMPTS
from .bronze import (
    DEFAULT_BRONZE_CONFIG, BronzeConfig, BronzeContractError, BronzeFilesystemError,
    BronzeIntegrationContext, BronzeIntegrityError, BronzeRegistrationError,
    IntegratedSourceCatalog,
    inspect_existing_bronze, integrate_selected_source, recover_existing_bronze,
)
from .ranking import DEFAULT_RANKING_CONFIG, RankingConfig, rank_candidates
from .run_models import (
    RunConfigurationError, RunStage, RunStateError, RunSummary, RunTargetStatus,
    TargetFailure, TargetFailureKind, require_run_id, require_utc,
)
from .run_repository import SQLiteRunRepository
from .selection_models import NoSelection, SelectedSource, SelectionFailed
from .source_selection import (
    DEFAULT_SELECTION_CONFIG, SelectionConfig, SourceSelectionCapability, select_source,
)
from .targets import DEFAULT_CATALOG, load_catalog
from .transcripts import CaptionProvider, TranscriptAcquisitionError, acquire_transcripts
from .unseen import (
    CollectionFailureKind, IntegratedSourceReader, PageDiscovery, UnseenCollectionError,
    collect_unseen_candidates,
)


def _utc_now():
    return datetime.now(timezone.utc)


def _new_run_id():
    return str(uuid4())


class RunSourceCatalog(IntegratedSourceReader, IntegratedSourceCatalog, Protocol):
    """One catalog supplies read-only collection snapshots and post-Bronze registration."""


class AcquisitionRunService:
    """Sequential acquisition through Bronze, with explicit bounded resume/reconciliation.

    Providers are injected and retain ownership of their configuration, connections,
    and local retry policies. The caller closes provider resources when appropriate.
    """

    def __init__(
        self, repository: SQLiteRunRepository, *, discovery: PageDiscovery,
        catalog_reader: RunSourceCatalog, caption_provider: CaptionProvider,
        selection_capability: SourceSelectionCapability,
        ranking_config: RankingConfig = DEFAULT_RANKING_CONFIG,
        selection_config: SelectionConfig = DEFAULT_SELECTION_CONFIG,
        bronze_config: BronzeConfig = DEFAULT_BRONZE_CONFIG,
        clock: Callable[[], datetime] = _utc_now, id_factory: Callable[[], str] = _new_run_id,
    ):
        if (not isinstance(repository, SQLiteRunRepository)
                or not isinstance(ranking_config, RankingConfig)
                or not isinstance(selection_config, SelectionConfig)
                or not isinstance(bronze_config, BronzeConfig)
                or not callable(clock) or not callable(id_factory)):
            raise RunConfigurationError("Invalid acquisition run configuration")
        for value, methods in (
            (discovery, ("discover_page",)),
            (catalog_reader, ("load_integrated_ids", "is_integrated", "register_integrated")),
            (caption_provider, ("list_tracks", "fetch_track")),
            (selection_capability, ("assess_quality", "compare_sources")),
        ):
            if any(not callable(getattr(value, name, None)) for name in methods):
                raise RunConfigurationError("Missing acquisition component capability")
        # Never initialize or write run tables into the integrated-source file.
        source_path = getattr(catalog_reader, "path", None)
        if source_path is not None and Path(source_path).expanduser().resolve() == repository.path:
            raise RunConfigurationError("Run progress and integrated sources require separate files")
        self.repository = repository
        self.discovery = discovery
        self.catalog_reader = catalog_reader
        self.caption_provider = caption_provider
        self.selection_capability = selection_capability
        self.ranking_config = ranking_config
        self.selection_config = selection_config
        self.bronze_config = bronze_config
        self.clock = clock
        self.id_factory = id_factory

    def _now(self):
        now = self.clock()
        require_utc(now)
        return now

    def start_new_run(self, catalog_path: str | Path = DEFAULT_CATALOG) -> RunSummary:
        with self.repository.execution_guard():
            catalog = load_catalog(Path(catalog_path))
            run_id = self.id_factory()
            require_run_id(run_id)
            self.repository.create_run(run_id, catalog, self._now())
            return self._drive(run_id)

    def resume_run(self, run_id: str) -> RunSummary:
        require_run_id(run_id)
        with self.repository.execution_guard():
            self.repository.recover_run(run_id, self._now())
            return self._drive(run_id)

    def inspect_run(self, run_id: str) -> RunSummary:
        """Recover stale state under the guard, without any provider call or new attempt."""
        require_run_id(run_id)
        with self.repository.execution_guard():
            return self.repository.recover_run(run_id, self._now())

    def _drive(self, run_id):
        self.repository.activate_run(run_id, self._now())
        # A single fixed pass: failures are retried only by a later explicit resume.
        for target in self.repository.list_targets(run_id):
            # Check selected identity even on exhausted FAILED work: registration may
            # have committed immediately before process death on the third attempt.
            if target.selected_video_id is not None and target.status is not RunTargetStatus.INTEGRATED:
                if self._reconcile(target):
                    continue
                target = next(t for t in self.repository.list_targets(run_id)
                              if t.target.id == target.target.id)
            if target.terminal:
                continue
            attempt = self.repository.begin_attempt(run_id, target.target.id, self._now())
            outcome = self._process_target(attempt.target, attempt.attempt_started_at_utc)
            # Storage is deliberately outside the component-exception boundary.
            fields = {}
            if isinstance(outcome, SelectedSource):
                status = RunTargetStatus.SELECTED
                fields = {"selected_video_id": outcome.source.ranked_candidate.candidate.video_id,
                          "selection_method": outcome.method}
            elif isinstance(outcome, NoSelection):
                status = RunTargetStatus.NO_SELECTION
                fields = {"no_selection_reason": outcome.reason}
            else:
                status = RunTargetStatus.FAILED
                fields = {"failure": outcome}
            stored = self.repository.finish_attempt(run_id, attempt.target.id, attempt.attempt_count,
                                                    self._now(), status=status, **fields)
            if isinstance(outcome, SelectedSource):
                context = BronzeIntegrationContext(run_id, stored.selected_at_utc, stored.target)
                self._integrate(stored, lambda: integrate_selected_source(
                    outcome, catalog=self.catalog_reader, config=self.bronze_config, context=context,
                ))
            del outcome
        return self.repository.finish_invocation(run_id, self._now())

    def _fail_integration(self, target, stage, kind):
        return self.repository.fail_integration(
            target.run_id, target.target.id, target.attempt_count, target.selected_video_id,
            self._now(), TargetFailure(stage, kind),
        )

    def _integrate(self, target, operation):
        # Database commits deliberately remain outside the component-error catcher.
        try:
            result = operation()
            if result.video_id != target.selected_video_id:
                raise BronzeContractError(target.selected_video_id, "integration_result")
        except BronzeIntegrityError:
            self._fail_integration(target, RunStage.BRONZE_VALIDATION, TargetFailureKind.INTEGRITY)
            raise
        except BronzeRegistrationError:
            self._fail_integration(target, RunStage.SOURCE_REGISTRATION, TargetFailureKind.REGISTRATION)
            return
        except BronzeFilesystemError:
            self._fail_integration(target, RunStage.BRONZE_PERSISTENCE, TargetFailureKind.FILESYSTEM)
            return
        except BronzeContractError:
            self._fail_integration(target, RunStage.BRONZE_VALIDATION, TargetFailureKind.CONTRACT)
            return
        except (RunStateError, MemoryError):
            raise
        except Exception:
            self._fail_integration(target, RunStage.BRONZE_PERSISTENCE,
                                   TargetFailureKind.UNEXPECTED_COMPONENT_EXCEPTION)
            return
        self.repository.finish_integration(
            target.run_id, target.target.id, target.attempt_count, target.selected_video_id, self._now(),
        )

    def _reconcile(self, target):
        """True means this invocation is done with this target; False permits acquisition."""
        try:
            state = inspect_existing_bronze(target.selected_video_id,
                                            catalog=self.catalog_reader, config=self.bronze_config)
        except BronzeIntegrityError:
            self._fail_integration(target, RunStage.BRONZE_VALIDATION, TargetFailureKind.INTEGRITY)
            raise
        except BronzeRegistrationError as error:
            raise RunStateError("Integrated-source state could not be inspected safely") from error
        except BronzeFilesystemError:
            self._fail_integration(target, RunStage.BRONZE_PERSISTENCE, TargetFailureKind.FILESYSTEM)
            return True
        if state is not None and state.registered:
            self.repository.finish_integration(target.run_id, target.target.id, target.attempt_count,
                                               target.selected_video_id, self._now())
            return True
        if target.attempt_count >= MAX_TARGET_ATTEMPTS:
            if target.status is not RunTargetStatus.FAILED:
                self._fail_integration(
                    target, RunStage.SOURCE_REGISTRATION if state else RunStage.ORCHESTRATION,
                    TargetFailureKind.REGISTRATION if state else TargetFailureKind.INTERRUPTED,
                )
            return True
        if state is not None:
            attempt = self.repository.begin_attempt(target.run_id, target.target.id, self._now(),
                                                     registration_only=True)
            self._integrate(attempt, lambda: recover_existing_bronze(
                target.selected_video_id, catalog=self.catalog_reader, config=self.bronze_config,
            ))
            return True
        if target.status is RunTargetStatus.SELECTED:
            self._fail_integration(target, RunStage.ORCHESTRATION, TargetFailureKind.INTERRUPTED)
        return False

    def _process_target(self, target, reference_time):
        stage = RunStage.COLLECTION
        try:
            candidates = collect_unseen_candidates(
                target, discovery=self.discovery, catalog_reader=self.catalog_reader,
            )
            stage = RunStage.RANKING
            shortlist = rank_candidates(candidates, reference_time=reference_time,
                                        config=self.ranking_config)
            stage = RunStage.TRANSCRIPTS
            transcripts = acquire_transcripts(shortlist, provider=self.caption_provider)
            stage = RunStage.QUALITY
            outcome = select_source(target, transcripts, capability=self.selection_capability,
                                    config=self.selection_config)
            if not isinstance(outcome, (SelectedSource, NoSelection, SelectionFailed)):
                raise ValueError("Invalid selection outcome")
            if outcome.target_id != target.id:
                raise ValueError("Selection outcome belongs to another target")
            if isinstance(outcome, SelectionFailed):
                return TargetFailure(RunStage(outcome.stage.value), TargetFailureKind(outcome.kind.value))
            return outcome
        except UnseenCollectionError as error:
            if error.kind is CollectionFailureKind.CATALOG:
                raise RunStateError("Integrated-source state could not be read safely") from error
            if error.discovery_failure_kind is not None:
                return TargetFailure(RunStage.DISCOVERY,
                                     TargetFailureKind(error.discovery_failure_kind.value.upper()))
            return TargetFailure(RunStage.COLLECTION, TargetFailureKind.CONTRACT)
        except TranscriptAcquisitionError as error:
            return TargetFailure(RunStage.TRANSCRIPTS, TargetFailureKind(error.kind.value.upper()))
        except (RunStateError, MemoryError):
            raise
        except Exception:
            # Persist no exception text, request, response, transcript, or credentials.
            return TargetFailure(stage, TargetFailureKind.UNEXPECTED_COMPONENT_EXCEPTION)
