# ADR 017: Atomically select Gold and successful integration history

**Status:** Accepted

## Context

The separate preparation boundary returns validated candidate Gold in memory. Publishing it without complete durable audit would leave no reliable successful identity, replay protection or evidence for literal no-mutation success. Existing document storage already provides immutable staging and atomic local selection.

## Decision

Explicitly opt a store into selector version 2, retaining the exact legacy selector backup and an event-free baseline. Select the Gold reference and immutable hash-linked integration-history head through one atomic `CURRENT.json` replacement under the existing local publication guard. Gold manifests/document contracts stay unchanged. Legacy stores remain readable; coordinated stores reject standalone Gold publication. Readers do not silently migrate, repair or reconstruct history.

Validate canonical source admission and compatible successful replay before semantic preparation. Reuse the original successful identity/events without new semantic calls, mutation or history. For new work, consume the existing complete prepared result and recheck its exact base X immediately before publication. Stage complete Gold only if changed, then complete history; flush and validate everything before selection. A stale proposal requires fresh preparation. No provider calls occur under the commit guard.

Derive exactly eight event types from accepted operations and actual effects. Persist minimal accepted-operation audit facts, source-item accounting, affected-unit before/after state, applicable document versions/hashes and compact model/prompt/run metadata. Preserve merge identities and unresolved conflicts. New support emits `PROVENANCE_ADDED`; literal no-mutation success emits `NO_CHANGE_RECORDED`, with reason `NO_SOURCE_KNOWLEDGE` for the empty-input fast path. That path invents no plan, semantic call or Gold change. Structural side effects belong to originating operation events; unaccounted structure is rejected.

Read the selector once, validate the complete selected history chain and every referenced immutable Gold snapshot, and expose pinned read-only event queries. Unsupported versions, corruption or inconsistent recorded success fail explicitly. Do not persist full plans, rejected candidates, prompts, responses or transcripts. No derived database/index or additional canonical pointer is introduced.

## Consequences

One atomic selector makes complete Gold/history success visible together, including no-op success with unchanged Gold pairs. Unselected staging remains noncanonical after interruption. A replacement error is resolved only by validating visible durable state; compatible retries use replay, not fresh semantic integration. Existing local filesystem ownership and OS durability assumptions remain applicable.

Full-chain validation and retained historical snapshots favor auditability over storage and large-corpus efficiency. Missing history is not reconstructed from current knowledge. Restoring the legacy backup after new commits would lose successful history and is not a normal rollback. Future compaction/migration needs its own explicit design. This delivers explicit component APIs, not automatic acquisition-to-curation orchestration, Core Pipeline end-to-end acceptance, metrics aggregation, retrieval synchronization or comparative evaluation.
