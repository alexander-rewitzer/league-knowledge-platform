"""Read-only section loading and single-invocation transient knowledge extraction."""

from hashlib import sha256
from pathlib import Path

from .extraction_models import (
    DEFAULT_EXTRACTION_CONFIG, EXTRACTION_CONTRACT_VERSION, ArtifactReference, EvidenceSpan,
    ExtractionCapability, ExtractionCapabilityError, ExtractionConfig, ExtractionInputError,
    ExtractionIntegrityError, ExtractionProposal, ExtractionRefinementContext,
    ExtractionProvenance, ExtractionRequest, ExtractionResponse, ExtractionSection,
    ExtractionSegment, ExtractionValidationError, KnowledgeEvidence, KnowledgeItem,
)
from .extraction_prompts import EXTRACTION_PROMPT_VERSION, EXTRACTION_REFINEMENT_PROMPT_VERSION
from .models import _require_video_id
from .segmentation import (
    canonical_segmentation_path, decode_segmentation_artifact, validate_segmentation_against_silver,
)
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG, SectionKind, SegmentationError, SegmentationIntegrityError
from .silver import DEFAULT_SILVER_CONFIG, SilverError, canonical_silver_path, decode_silver_artifact


def extraction_section_from_artifacts(*, segmentation_content: bytes, silver_content: bytes,
                                      section_id: str) -> ExtractionSection:
    """Bind a full CONTENT unit to validated immutable upstream byte snapshots."""
    if type(segmentation_content) is not bytes or type(silver_content) is not bytes:
        raise ExtractionInputError("ARTIFACT_BYTES")
    try:
        silver = decode_silver_artifact(silver_content)
        segmented = decode_segmentation_artifact(segmentation_content)
        validate_segmentation_against_silver(segmented, silver, silver_sha256=sha256(silver_content).hexdigest())
        if segmented["outcome"] == "NO_SEGMENTABLE_CONTENT":
            raise ExtractionInputError("NO_SEGMENTABLE_CONTENT")
        section = next((s for s in segmented["sections"] if s["section_id"] == section_id), None)
        if section is None or section["kind"] != "CONTENT":
            raise ExtractionInputError("CONTENT_SECTION_REQUIRED")
        video_id = silver["source"]["video_id"]
        relative_path = f"youtube/{canonical_silver_path(video_id).name}"
        provenance = ExtractionProvenance(
            video_id,
            ArtifactReference(relative_path, sha256(segmentation_content).hexdigest(), segmented["schema_version"]),
            ArtifactReference(relative_path, sha256(silver_content).hexdigest(), silver["schema_version"]),
            ArtifactReference(**silver["bronze"]), segmented["segmentation_contract_version"],
            silver["cleaning"]["rule_version"],
        )
        members = silver["transcript"]["segments"][section["start_position"]:section["end_position_exclusive"]]
        return ExtractionSection(section_id, SectionKind.CONTENT, silver["transcript"]["language_code"],
                                 section["text"], tuple(ExtractionSegment(**s) for s in members), provenance)
    except SegmentationIntegrityError:
        raise ExtractionIntegrityError("UPSTREAM_SNAPSHOT") from None
    except (SilverError, SegmentationError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise ExtractionInputError("UPSTREAM_CONTRACT") from None


def _read(path, root):
    # Resolve the directory before following any candidate file; never create/flush/clean it.
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path(root).expanduser().resolve()):
        raise ExtractionIntegrityError("UPSTREAM_PATH")
    return path.read_bytes()


def load_extraction_section(video_id: str, section_id: str, *, silver_config=DEFAULT_SILVER_CONFIG,
                            segmentation_config=DEFAULT_SEGMENTATION_CONFIG) -> ExtractionSection:
    try:
        _require_video_id(video_id)
        silver_bytes = _read(canonical_silver_path(video_id, silver_config), silver_config.root)
        segmented_bytes = _read(canonical_segmentation_path(video_id, segmentation_config), segmentation_config.root)
        section = extraction_section_from_artifacts(segmentation_content=segmented_bytes,
                                                    silver_content=silver_bytes, section_id=section_id)
        if section.provenance.video_id != video_id:
            raise ExtractionIntegrityError("SOURCE_IDENTITY")
        return section
    except OSError:
        raise ExtractionInputError("UPSTREAM_READ") from None
    except (ValueError, TypeError, AttributeError):
        raise ExtractionInputError("INPUT_CONFIGURATION") from None


def _validate_spans(spans, section):
    positions = {s.source_index: i for i, s in enumerate(section.segments)}
    if not isinstance(spans, tuple) or not spans:
        raise ValueError("Missing evidence")
    for span in spans:
        if not isinstance(span, EvidenceSpan):
            raise ValueError("Invalid evidence span")
        span.__post_init__()
        first, last = positions.get(span.first_source_index), positions.get(span.last_source_index)
        if first is None or last is None or first > last:
            raise ValueError("Evidence outside current section")


def validate_extraction_proposal(proposal: ExtractionProposal, section: ExtractionSection, *,
                                 supported_prompt_versions=(EXTRACTION_PROMPT_VERSION,)) -> ExtractionProposal:
    """Structural validation only; reference validity is not semantic acceptance."""
    try:
        if not isinstance(section, ExtractionSection) or not isinstance(proposal, ExtractionProposal):
            raise ValueError("Expected typed section and proposal")
        section.__post_init__()
        proposal.__post_init__()
        if (not isinstance(supported_prompt_versions, tuple) or not supported_prompt_versions
                or not set(supported_prompt_versions) <= {EXTRACTION_PROMPT_VERSION, EXTRACTION_REFINEMENT_PROMPT_VERSION}):
            raise ValueError("Unsupported extraction prompt contract")
        if (proposal.section_id != section.section_id or proposal.provenance != section.provenance
                or proposal.metadata.prompt_version not in supported_prompt_versions):
            raise ValueError("Proposal context disagrees")
        statements = set()
        for position, item in enumerate(proposal.items, 1):
            if item.item_id != f"item-{position:06d}" or item.statement in statements:
                raise ValueError("Invalid local identity or identical statement")
            statements.add(item.statement)
            for evidence in item.evidence:
                if evidence.section_id != section.section_id:
                    raise ValueError("Evidence section disagrees")
                _validate_spans(evidence.spans, section)
        return proposal
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise ExtractionValidationError("PROPOSAL_CONTRACT") from None


def extract_knowledge(section: ExtractionSection, *, capability: ExtractionCapability,
                      config: ExtractionConfig = DEFAULT_EXTRACTION_CONFIG,
                      refinement: ExtractionRefinementContext | None = None) -> ExtractionProposal:
    """One capability invocation; no retries, semantic Judge, cache or persistence."""
    try:
        if not isinstance(section, ExtractionSection) or not isinstance(config, ExtractionConfig):
            raise ValueError("Expected complete section and configuration")
        section.__post_init__()
        config.__post_init__()
        if refinement is not None:
            if not isinstance(refinement, ExtractionRefinementContext):
                raise ValueError("Expected structured refinement context")
            refinement.__post_init__()
            validate_extraction_proposal(refinement.previous_proposal, section,
                supported_prompt_versions=(EXTRACTION_PROMPT_VERSION, EXTRACTION_REFINEMENT_PROMPT_VERSION))
            ids = {i.item_id for i in refinement.previous_proposal.items}
            for issue in refinement.feedback:
                if not set(issue.affected_item_ids) <= ids:
                    raise ValueError("Feedback refers to another proposal")
                if issue.relevant_source_spans:
                    _validate_spans(issue.relevant_source_spans, section)
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        raise ExtractionInputError("SECTION_CONTRACT") from None
    if not callable(getattr(capability, "extract", None)):
        raise ExtractionCapabilityError("CONFIGURATION", retryable=False)
    request = ExtractionRequest(section.language_code, section.segments, config.output_limit, refinement)
    try:
        response = capability.extract(request)
    except ExtractionCapabilityError:
        raise
    except Exception:
        raise ExtractionCapabilityError("PROVIDER_CONTRACT", retryable=False) from None
    try:
        if not isinstance(response, ExtractionResponse):
            raise ValueError("Expected structured extraction response")
        response.__post_init__()
        items = []
        for position, draft in enumerate(response.items, 1):
            _validate_spans(draft.spans, section)
            # The model never supplies section provenance or local item identity.
            evidence = KnowledgeEvidence(section.section_id, draft.spans)
            items.append(KnowledgeItem(f"item-{position:06d}", draft.statement, (evidence,)))
        proposal = ExtractionProposal(EXTRACTION_CONTRACT_VERSION, section.section_id, section.provenance,
                                      response.outcome, tuple(items), response.metadata)
        expected_prompt = EXTRACTION_PROMPT_VERSION if refinement is None else EXTRACTION_REFINEMENT_PROMPT_VERSION
        return validate_extraction_proposal(proposal, section, supported_prompt_versions=(expected_prompt,))
    except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError):
        raise ExtractionValidationError("RESPONSE_CONTRACT") from None
