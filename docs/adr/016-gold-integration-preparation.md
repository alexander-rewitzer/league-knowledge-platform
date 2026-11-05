# ADR 016: Separate Gold preparation from durable integration commit

**Status:** Accepted

## Context

Semantic integration requires model judgments and corrective feedback. Canonical Gold publication and successful audit history require a separate durable boundary. Publishing a semantic candidate alone would leave no reliable evidence for replay or successful no-mutation integrations.

## Decision

Prepare one compatible canonical source artifact against one pinned Gold snapshot X. Reuse the seven governance operation contracts and read-only inspection. Require complete source-item accounting and exactly eight binary semantic gates; only all PASS permits deterministic candidate-state construction. Refinement replaces the entire plan against unchanged source/X. At most three completed valid plan/evaluation pairs consume rounds; operational failures abort without consuming the failed round or retrying silently.

The preparer returns an immutable, noncanonical in-memory result containing the complete validated candidate, exact source/base references, accepted plan, completed rounds, logical hashes and deterministic effects. Existing IDs, provenance, directional relations, conflicts and MERGED history are preserved. Organizational pruning and document-version rules reuse the canonical document layer. Candidate Markdown/metadata pairs are checked in memory. No temporary result becomes durable success merely by existing.

Read-only inspection is bounded by call and serialized observation budgets. The Agent can request additional whitelisted queries; the Validator receives relevant observed Gold and full touched-document before-state. Neither role automatically receives the whole corpus. Capacity failures never silently truncate source or Gold content. New declarations use application-allocated IDs; model-authored paths and file edits are forbidden.

Valid empty source knowledge creates a prepared no-op without an Agent, Validator, invented plan or semantic round. Semantic NO_CHANGE may instead prepare actual supporting provenance; a literal no-mutation result leaves Gold/document versions unchanged. Preparation emits no audit events and allocates no durable integration identity.

The future durable layer owns successful-integration identity/history, replay lookup, events, current-Gold/history consistency and final canonical publication. It checks compatible successful replay before invoking preparation and verifies that X is still current immediately before committing candidate Gold and required history together. A stale result requires fresh inspection/preparation, never repair or silent rebase. No-mutation success still needs durable audit without a fabricated Gold change.

## Consequences

Preparation can be tested independently of durable commit and introduces no persistence migration. Full canonical Source → Gold integration remains incomplete until the durable layer exists. Complete in-memory state favors auditable validation over memory efficiency; relevant model context is separately bounded. Model judgments remain fallible and lexical inspection is not a semantic recall guarantee. The existing source-audit, Gold, governance and inspection contracts remain unchanged. Retrieval, workers and distributed infrastructure are outside this decision.
