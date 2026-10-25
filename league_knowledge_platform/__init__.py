"""Local League knowledge acquisition components."""

from .gold_models import (
    GOLD_CONTRACT_VERSION, GOLD_SCHEMA_VERSION, GoldContractError, GoldContractState,
    GoldDocumentVersion, GoldKnowledgeUnit, GoldProvenance, GoldReferenceError,
    GoldRelation, GoldRelationKind, GoldSourceArtifactReference, GoldStatus,
    GoldVersionError, new_document_id, new_knowledge_id,
)
from .gold_contract import (
    decode_gold_document_version, decode_gold_state, decode_gold_unit_document,
    encode_gold_state, get_gold_unit, gold_document_version_document,
    gold_state_document, gold_unit_document, resolve_knowledge_id,
    validate_gold_provenance, validate_gold_state, validate_gold_unit,
)

from .source_audit_models import (
    DEFAULT_SOURCE_AUDIT_CONFIG, SOURCE_AUDIT_CONTRACT_VERSION, SOURCE_KNOWLEDGE_SCHEMA_VERSION,
    SectionCheckpointReference, SectionItemReference, SourceAuditCapability, SourceAuditCapabilityError,
    SourceAuditConfig, SourceAuditDecision, SourceAuditError, SourceAuditExhaustedError, SourceAuditGate,
    SourceAuditInput, SourceAuditInputError, SourceAuditIntegrityError, SourceAuditIssue,
    SourceAuditOperationalError, SourceAuditRequest, SourceAuditResponse, SourceAuditRound,
    SourceAuditUpstreamCoverageError, SourceConflict, SourceConflictDraft, SourceGateEvaluation,
    SourceInputItem, SourceIssueKind, SourceItemDraft, SourceKnowledgeArtifact, SourceKnowledgeCandidate,
    SourceKnowledgeContractError, SourceKnowledgeFilesystemError, SourceKnowledgeItem,
    SourceKnowledgeOutcome, SourceKnowledgeResult, SourceRefinementContext, SourceValidatorCapability,
    SourceValidatorEvaluation, SourceValidatorRequest,
)
from .source_audit import process_source_knowledge, evaluate_source_candidate
from .source_audit_input import load_source_audit_input
from .source_knowledge import canonical_source_knowledge_path, load_source_knowledge
from .deepseek_source_audit import DeepSeekSourceAuditAdapter, DeepSeekSourceAuditConfig, DeepSeekSourceValidatorAdapter

from .extraction_models import ExtractionFeedback, ExtractionRefinementContext
from .section_validation_models import (
    DEFAULT_SECTION_VALIDATION_CONFIG, SECTION_KNOWLEDGE_SCHEMA_VERSION, SECTION_VALIDATION_CONTRACT_VERSION,
    GateEvaluation, GateVerdict, JudgeIssue, SectionDecision, SectionJudgeCapability,
    SectionJudgeCapabilityError, SectionJudgeEvaluation, SectionJudgeRequest, SectionJudgeResult,
    SectionKnowledgeContractError, SectionKnowledgeFilesystemError, SectionKnowledgeResult,
    SectionValidationConfig, SectionValidationError, SectionValidationExhaustedError,
    SectionValidationInputError, SectionValidationIntegrityError, SectionValidationOperationalError,
    SemanticGate, SemanticRoundRecord, ValidatedSectionKnowledge,
)
from .section_validation import evaluate_section, process_section_knowledge
from .section_knowledge import canonical_section_knowledge_path, load_validated_section_knowledge
from .deepseek_section_validation import DeepSeekSectionJudgeAdapter, DeepSeekSectionJudgeConfig

from .extraction_models import (
    DEFAULT_EXTRACTION_CONFIG, EXTRACTION_CONTRACT_VERSION, ArtifactReference, EvidenceSpan,
    ExtractionCapability, ExtractionCapabilityError, ExtractionConfig, ExtractionError,
    ExtractionInputError, ExtractionIntegrityError, ExtractionItemDraft, ExtractionOutcome,
    ExtractionProposal, ExtractionProvenance, ExtractionRequest, ExtractionResponse,
    ExtractionSection, ExtractionSegment, ExtractionValidationError, KnowledgeEvidence, KnowledgeItem,
)
from .extraction import (
    extract_knowledge, extraction_section_from_artifacts, load_extraction_section, validate_extraction_proposal,
)
from .deepseek_extraction import DeepSeekExtractionAdapter, DeepSeekExtractionConfig

from .bronze import (
    BRONZE_SCHEMA_VERSION, DEFAULT_BRONZE_CONFIG, BronzeConfig, BronzeContractError,
    BronzeError, BronzeFilesystemError, BronzeIntegrationContext, BronzeIntegrationResult,
    BronzeIntegrityError, BronzeRegistrationError, ExistingBronzeState, IntegratedSourceCatalog,
    canonical_bronze_path, construct_bronze_artifact, decode_bronze_artifact,
    inspect_existing_bronze, integrate_selected_source, recover_existing_bronze, validate_bronze_artifact,
)

from .models import (
    CandidateVideo, ChampionCatalog, ChampionTarget, DiscoveryCursor, DiscoveryPage,
    RankedCandidate, RankedCandidateSet, UnseenCandidateSet,
)
from .ranking import DEFAULT_RANKING_CONFIG, RankingConfig, rank_candidates
from .source_catalog import SourceCatalogError, SQLiteSourceCatalog, initialize_source_catalog
from .targets import TargetLoadError, load_catalog, load_targets
from .transcript_models import (
    CaptionKind, CaptionSegment, CaptionTrack, ProviderTranscript, TranscriptOutcome,
    TranscriptSegment, TranscriptStatus, TranscriptUnavailableReason, TransientTranscript,
    TransientTranscriptSet,
)
from .transcripts import (
    CaptionProvider, CaptionProviderError, CaptionUnavailable, TranscriptAcquisitionError,
    TranscriptFailureKind, acquire_transcripts,
)
from .youtube_caption_provider import (
    DEFAULT_TRANSCRIPT_CONFIG, TranscriptConfig, YouTubeCaptionProvider,
)
from .unseen import (
    CollectionFailureKind,
    IntegratedSourceReader,
    PageDiscovery,
    UnseenCollectionError,
    collect_unseen_candidates,
)
from .youtube import DiscoveryError, DiscoveryFailureKind, YouTubeDiscovery
from .selection_models import (
    CandidateQualityAssessment, ComparativeCandidateAssessment, ComparativeSelection,
    ComparativeSelectionRequest, NoSelection, NoSelectionReason, QualityAssessmentBatch,
    QualityAssessmentRequest, QualityGateResult, QualityReasonCode, SelectedSource,
    SelectionAttempt, SelectionFailed, SelectionFailureKind, SelectionMethod, SelectionStage,
    SelectionStatus, SemanticCallMetadata, SemanticCandidate, SourceSelectionOutcome,
)
from .source_selection import (
    DEFAULT_SELECTION_CONFIG, SelectionCapabilityError, SelectionConfig,
    SourceSelectionCapability, select_source,
)
from .deepseek_selection import DeepSeekSelectionAdapter, DeepSeekSelectionConfig
from .run_models import (
    AcquisitionRun, RunConfigurationError, RunNotFoundError, RunStage, RunStateError,
    RunStatus, RunSummary, RunTarget, RunTargetStatus, TargetFailure, TargetFailureKind,
)
from .run_lock import RunBusyError
from .run_repository import (RUN_STATE_SCHEMA_VERSION, SQLiteRunRepository, initialize_run_repository,
                             migrate_run_repository_v1_to_v2)
from .acquisition_runs import AcquisitionRunService
from .silver import (
    SILVER_SCHEMA_VERSION, SILVER_CLEANING_RULE_VERSION, DEFAULT_SILVER_CONFIG,
    SilverConfig, SilverCleaningResult, SilverError, SilverInputError,
    SilverContractError, SilverIntegrityError, SilverFilesystemError,
    canonical_silver_path, clean_bronze_source, construct_silver_artifact,
    decode_silver_artifact, validate_silver_artifact, validate_silver_against_bronze,
)
from .segmentation_models import (
    DEFAULT_SEGMENTATION_CONFIG, SectionKind, SectionSpan, SegmentationAttempt,
    SegmentationCapability, SegmentationCapabilityError, SegmentationConfig,
    SegmentationContractError, SegmentationError, SegmentationFilesystemError,
    SegmentationInputError, SegmentationIntegrityError, SegmentationOutcome,
    SegmentationProcessingResult, SegmentationProposal, SegmentationProviderError,
    SegmentationRequest, SegmentationSegment, SegmentationValidationError, SegmentationValidationIssue,
)
from .segmentation import (
    SEGMENTATION_SCHEMA_VERSION, SEGMENTATION_CONTRACT_VERSION, canonical_segmentation_path,
    segment_silver_source, construct_segmentation_artifact, decode_segmentation_artifact,
    validate_segmentation_artifact, validate_segmentation_proposal,
    validate_segmentation_against_silver, load_segmentation_artifact, content_sections,
)
from .deepseek_segmentation import DeepSeekSegmentationAdapter, DeepSeekSegmentationConfig

__all__ = [
    "GOLD_CONTRACT_VERSION", "GOLD_SCHEMA_VERSION", "GoldContractError", "GoldContractState",
    "GoldDocumentVersion", "GoldKnowledgeUnit", "GoldProvenance", "GoldReferenceError",
    "GoldRelation", "GoldRelationKind", "GoldSourceArtifactReference", "GoldStatus",
    "GoldVersionError", "new_document_id", "new_knowledge_id",
    "decode_gold_document_version", "decode_gold_state", "decode_gold_unit_document",
    "encode_gold_state", "get_gold_unit", "gold_document_version_document",
    "gold_state_document", "gold_unit_document", "resolve_knowledge_id",
    "validate_gold_provenance", "validate_gold_state", "validate_gold_unit",
    "DEFAULT_SOURCE_AUDIT_CONFIG", "SOURCE_AUDIT_CONTRACT_VERSION", "SOURCE_KNOWLEDGE_SCHEMA_VERSION",
    "SectionCheckpointReference", "SectionItemReference", "SourceAuditCapability", "SourceAuditCapabilityError",
    "SourceAuditConfig", "SourceAuditDecision", "SourceAuditError", "SourceAuditExhaustedError", "SourceAuditGate",
    "SourceAuditInput", "SourceAuditInputError", "SourceAuditIntegrityError", "SourceAuditIssue",
    "SourceAuditOperationalError", "SourceAuditRequest", "SourceAuditResponse", "SourceAuditRound",
    "SourceAuditUpstreamCoverageError", "SourceConflict", "SourceConflictDraft", "SourceGateEvaluation",
    "SourceInputItem", "SourceIssueKind", "SourceItemDraft", "SourceKnowledgeArtifact", "SourceKnowledgeCandidate",
    "SourceKnowledgeContractError", "SourceKnowledgeFilesystemError", "SourceKnowledgeItem", "SourceKnowledgeOutcome",
    "SourceKnowledgeResult", "SourceRefinementContext", "SourceValidatorCapability", "SourceValidatorEvaluation",
    "SourceValidatorRequest", "process_source_knowledge", "evaluate_source_candidate", "load_source_audit_input",
    "canonical_source_knowledge_path", "load_source_knowledge", "DeepSeekSourceAuditAdapter",
    "DeepSeekSourceAuditConfig", "DeepSeekSourceValidatorAdapter",
    "DEFAULT_SECTION_VALIDATION_CONFIG", "SECTION_KNOWLEDGE_SCHEMA_VERSION", "SECTION_VALIDATION_CONTRACT_VERSION",
    "ExtractionFeedback", "ExtractionRefinementContext", "GateEvaluation", "GateVerdict", "JudgeIssue",
    "SectionDecision", "SectionJudgeCapability", "SectionJudgeCapabilityError", "SectionJudgeEvaluation",
    "SectionJudgeRequest", "SectionJudgeResult", "SectionKnowledgeContractError", "SectionKnowledgeFilesystemError",
    "SectionKnowledgeResult", "SectionValidationConfig", "SectionValidationError", "SectionValidationExhaustedError",
    "SectionValidationInputError", "SectionValidationIntegrityError", "SectionValidationOperationalError",
    "SemanticGate", "SemanticRoundRecord", "ValidatedSectionKnowledge", "evaluate_section", "process_section_knowledge",
    "canonical_section_knowledge_path", "load_validated_section_knowledge", "DeepSeekSectionJudgeAdapter", "DeepSeekSectionJudgeConfig",
    "DEFAULT_SEGMENTATION_CONFIG", "SectionKind", "SectionSpan", "SegmentationAttempt",
    "SegmentationCapability", "SegmentationCapabilityError", "SegmentationConfig",
    "SegmentationContractError", "SegmentationError", "SegmentationFilesystemError",
    "SegmentationInputError", "SegmentationIntegrityError", "SegmentationOutcome",
    "SegmentationProcessingResult", "SegmentationProposal", "SegmentationProviderError",
    "SegmentationRequest", "SegmentationSegment", "SegmentationValidationError", "SegmentationValidationIssue",
    "SEGMENTATION_SCHEMA_VERSION", "SEGMENTATION_CONTRACT_VERSION", "canonical_segmentation_path",
    "segment_silver_source", "construct_segmentation_artifact", "decode_segmentation_artifact",
    "validate_segmentation_artifact", "validate_segmentation_proposal",
    "validate_segmentation_against_silver", "load_segmentation_artifact", "content_sections",
    "DeepSeekSegmentationAdapter", "DeepSeekSegmentationConfig",
    "SILVER_SCHEMA_VERSION", "SILVER_CLEANING_RULE_VERSION", "DEFAULT_SILVER_CONFIG",
    "SilverConfig", "SilverCleaningResult", "SilverError", "SilverInputError",
    "SilverContractError", "SilverIntegrityError", "SilverFilesystemError",
    "canonical_silver_path", "clean_bronze_source", "construct_silver_artifact",
    "decode_silver_artifact", "validate_silver_artifact", "validate_silver_against_bronze",
    "BRONZE_SCHEMA_VERSION", "DEFAULT_BRONZE_CONFIG", "BronzeConfig", "BronzeContractError",
    "BronzeError", "BronzeFilesystemError", "BronzeIntegrationContext", "BronzeIntegrationResult",
    "BronzeIntegrityError", "BronzeRegistrationError", "ExistingBronzeState", "IntegratedSourceCatalog",
    "canonical_bronze_path", "construct_bronze_artifact", "decode_bronze_artifact",
    "inspect_existing_bronze", "integrate_selected_source", "recover_existing_bronze", "validate_bronze_artifact",
    "AcquisitionRun", "AcquisitionRunService", "ChampionCatalog", "RunBusyError",
    "RunConfigurationError", "RunNotFoundError", "RunStage", "RunStateError", "RunStatus",
    "RunSummary", "RunTarget", "RunTargetStatus", "TargetFailure", "TargetFailureKind",
    "RUN_STATE_SCHEMA_VERSION", "SQLiteRunRepository", "initialize_run_repository",
    "migrate_run_repository_v1_to_v2", "load_catalog",
    "CandidateQualityAssessment", "ComparativeCandidateAssessment", "ComparativeSelection",
    "ComparativeSelectionRequest", "DEFAULT_SELECTION_CONFIG", "DeepSeekSelectionAdapter",
    "DeepSeekSelectionConfig", "NoSelection", "NoSelectionReason", "QualityAssessmentBatch",
    "QualityAssessmentRequest", "QualityGateResult", "QualityReasonCode", "SelectedSource",
    "SelectionAttempt", "SelectionCapabilityError", "SelectionConfig", "SelectionFailed",
    "SelectionFailureKind", "SelectionMethod", "SelectionStage", "SelectionStatus",
    "SemanticCallMetadata", "SemanticCandidate", "SourceSelectionCapability",
    "SourceSelectionOutcome", "select_source",
    "CaptionKind",
    "CaptionProvider",
    "CaptionProviderError",
    "CaptionSegment",
    "CaptionTrack",
    "CaptionUnavailable",
    "CandidateVideo",
    "ChampionTarget",
    "CollectionFailureKind",
    "DiscoveryCursor",
    "DiscoveryError",
    "DiscoveryFailureKind",
    "DiscoveryPage",
    "DEFAULT_RANKING_CONFIG",
    "DEFAULT_TRANSCRIPT_CONFIG",
    "IntegratedSourceReader",
    "PageDiscovery",
    "ProviderTranscript",
    "RankedCandidate",
    "RankedCandidateSet",
    "RankingConfig",
    "SourceCatalogError",
    "SQLiteSourceCatalog",
    "TargetLoadError",
    "TranscriptAcquisitionError",
    "TranscriptConfig",
    "TranscriptFailureKind",
    "TranscriptOutcome",
    "TranscriptSegment",
    "TranscriptStatus",
    "TranscriptUnavailableReason",
    "TransientTranscript",
    "TransientTranscriptSet",
    "YouTubeCaptionProvider",
    "YouTubeDiscovery",
    "UnseenCandidateSet",
    "UnseenCollectionError",
    "collect_unseen_candidates",
    "acquire_transcripts",
    "initialize_source_catalog",
    "load_targets",
    "rank_candidates",
]
