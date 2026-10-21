# ADR 011: Single semantic Silver segmentation stage

**Status:** Accepted

## Context

Deterministic Silver cleaning preserves the complete source transcript and provenance. Downstream knowledge extraction needs coherent logical sections without losing source material or treating model-authored text as original content.

## Decision

Introduce one explicit semantic SEGMENT stage after durable cleaned Silver. An LLM proposes contiguous boundaries over indivisible existing segments and exactly one kind per section: CONTENT or ADVERTISEMENT. This is a narrow exception to the [general Silver metadata guidance](004-minimal-deterministic-metadata.md), not a broader semantic ontology.

Application code enforces complete exactly-once ordered coverage and derives section text, membership and timing from Silver. No generated titles, summaries, content rewriting, section-size rules, fixed-size fallback or advertisement deletion. Clearly promotional material may be ADVERTISEMENT; mixed substantive/promotional or ambiguous sections remain CONTENT. Structural validation cannot establish semantic correctness.

Invalid proposals receive concrete feedback within one budget of at most three semantic/provider capability invocations. Transport retries are disabled. Provider/resource failures are explicit and produce no partial canonical success.

Publish a durable source-centric versioned processing result. SEGMENTED retains all logical sections; valid all-empty input deterministically produces NO_SEGMENTABLE_CONTENT with zero sections/calls and exact upstream provenance. Canonical reuse validates the same Silver snapshot and supported contract without further inference. The first valid durable result remains unchanged despite possible nondeterminism; incompatible or corrupt artifacts are never silently replaced.

Downstream extraction consumes validated CONTENT sections and begins EXTRACT / VALIDATE / REFINE, not another semantic SEGMENT phase. Ads remain durable even when mechanically excluded from extraction input. NO_SEGMENTABLE_CONTENT is terminal without extraction.

## Consequences

The durable Silver-to-segmentation-to-extraction boundary remains inspectable and usable by later pipeline designs. This stage adds model cost, availability and classification risks while keeping source text auditable. It introduces no acquisition coupling, queue, worker, Gold processing, embeddings or retrieval-index updates. Bronze and canonical cleaned Silver remain immutable.
