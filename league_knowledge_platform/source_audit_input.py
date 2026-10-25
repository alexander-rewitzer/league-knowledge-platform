"""Read-only snapshots of every required validated section of one source."""

from hashlib import sha256
import os
from pathlib import Path

from .extraction import extraction_section_from_artifacts
from .extraction_models import ArtifactReference, ExtractionProvenance, ExtractionError
from .models import _require_video_id
from .section_knowledge import canonical_section_knowledge_path, decode_section_checkpoint
from .section_validation_models import DEFAULT_SECTION_VALIDATION_CONFIG, SectionValidationError
from .segmentation import canonical_segmentation_path, decode_segmentation_artifact, validate_segmentation_against_silver
from .segmentation_models import DEFAULT_SEGMENTATION_CONFIG, SegmentationError
from .silver import DEFAULT_SILVER_CONFIG, SilverError, canonical_silver_path, decode_silver_artifact
from .source_audit_models import (
    SectionCheckpointReference, SectionItemReference, SourceAuditError, SourceAuditInput,
    SourceAuditInputError, SourceAuditIntegrityError, SourceFileSnapshot, SourceInputItem,
)


def resolved_source_path(path):
    result = Path(path).expanduser().resolve()
    if os.name == "nt":
        # During concurrent directory creation, Windows realpath can return an
        # equivalent extended-length spelling for only one side of a comparison.
        # Normalize that spelling AFTER resolving links; never bypass containment.
        value = str(result)
        if value[:8].upper() == "\\\\?\\UNC\\":
            result = Path("\\\\" + value[8:])
        elif value.startswith("\\\\?\\") and len(value) >= 7 and value[4].isalpha() and value[5:7] == ":\\":
            result = Path(value[4:])
    return result


def validate_source_path(path, root, *, phase="INPUT"):
    if Path(path).is_symlink() or not resolved_source_path(Path(path).parent).is_relative_to(resolved_source_path(root)):
        raise SourceAuditIntegrityError("ARTIFACT_PATH", phase=phase)


def read_source_file(path, root):
    path = Path(path)
    validate_source_path(path, root)
    return path.read_bytes()


def verify_source_snapshot(inputs):
    """Never flush, regenerate or clean upstream state."""
    try:
        for entry in inputs.snapshots:
            if sha256(read_source_file(entry.path, entry.root)).hexdigest() != entry.sha256:
                raise SourceAuditIntegrityError("UPSTREAM_CHANGED")
    except OSError:
        raise SourceAuditIntegrityError("UPSTREAM_CHANGED") from None


def load_source_audit_input(video_id, *, silver_config=DEFAULT_SILVER_CONFIG,
                            segmentation_config=DEFAULT_SEGMENTATION_CONFIG,
                            section_validation_config=DEFAULT_SECTION_VALIDATION_CONFIG):
    snapshots = []

    def capture(path, root):
        content = read_source_file(path, root)
        digest = sha256(content).hexdigest()
        snapshots.append(SourceFileSnapshot(path, resolved_source_path(root), digest))
        return content, digest

    try:
        _require_video_id(video_id)
        for config in (silver_config, segmentation_config, section_validation_config):
            config.__post_init__()
        silver_bytes, silver_hash = capture(canonical_silver_path(video_id, silver_config), silver_config.root)
        section_bytes, section_hash = capture(canonical_segmentation_path(video_id, segmentation_config), segmentation_config.root)
        silver = decode_silver_artifact(silver_bytes)
        segmented = decode_segmentation_artifact(section_bytes)
        validate_segmentation_against_silver(segmented, silver, silver_sha256=silver_hash)
        if silver["source"]["video_id"] != video_id:
            raise SourceAuditIntegrityError("SOURCE_IDENTITY")
        relative = f"youtube/id-{video_id.encode('ascii').hex()}.json"
        provenance = ExtractionProvenance(video_id, ArtifactReference(relative, section_hash, segmented["schema_version"]),
            ArtifactReference(relative, silver_hash, silver["schema_version"]), ArtifactReference(**silver["bronze"]),
            segmented["segmentation_contract_version"], silver["cleaning"]["rule_version"])
        sections, references, items = [], [], []
        for definition in segmented["sections"]:
            if definition["kind"] != "CONTENT":
                continue
            section = extraction_section_from_artifacts(segmentation_content=section_bytes, silver_content=silver_bytes,
                                                        section_id=definition["section_id"])
            path = canonical_section_knowledge_path(video_id, section.section_id, section_validation_config)
            content, digest = capture(path, section_validation_config.root)
            checkpoint = decode_section_checkpoint(content, section)
            sections.append(section)
            references.append(SectionCheckpointReference(section.section_id,
                f"youtube/id-{video_id.encode('ascii').hex()}/{section.section_id}.json", digest,
                checkpoint.schema_version, checkpoint.validation_contract_version))
            for item in checkpoint.accepted_proposal.items:
                items.append(SourceInputItem(f"input-{len(items)+1:06d}", SectionItemReference(section.section_id, item.item_id), item))
        result = SourceAuditInput(provenance, segmented["outcome"], tuple(sections), tuple(references), tuple(items), tuple(snapshots))
        verify_source_snapshot(result)
        return result
    except SourceAuditError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError,
            SilverError, SegmentationError, ExtractionError, SectionValidationError):
        raise SourceAuditInputError("UPSTREAM_INPUT") from None
