# V1 scope

## Purpose

The Knowledge Platform investigates whether upstream agentic knowledge preparation can reduce query-time complexity and latency for previously unseen multi-hop questions while preserving answer quality. This is a hypothesis to evaluate, not a demonstrated result. League of Legends was selected as the experimental domain for its large, evolving, interconnected knowledge space, multi-hop relationships, overlapping sources, and substantial public unstructured educational material. Additional sources can inform incremental knowledge preparation; building a League application is not the primary technical purpose.

This repository currently builds the acquisition and knowledge-preparation side. Implemented Source Acquisition reaches transient selection outcomes through English transcript acquisition, absolute semantic assessment, deterministic quality gating, and comparative selection only when needed. Resumable runs pass the selected source directly to Bronze and register it only after durable validation; Silver processing, curation, Gold persistence, and retrieval indexing remain planned. A separate Voice Coach may later retrieve the prepared knowledge and reason over game context supplied by the player; its application and evaluation remain separate from the knowledge-preparation experiment.

V1 is an incremental, local-first system rather than a fixed source corpus. Its initial acquisition targets are all Champions in a versioned, machine-readable local catalog. There is no Champion × Role target matrix: role-specific guidance and reusable general concepts may emerge from selected sources and be represented in Gold. Systematic acquisition for general-concept targets is future scope.

## Source responsibilities

Riot Games Data Dragon establishes the authoritative Champion target catalog / target universe. The repository loads the committed versioned artifact locally without a runtime Riot request. This structured target source is not the main knowledge corpus. YouTube is the V1 public source pool: relevant educational knowledge is primarily in unstructured video/transcript content, with structured metadata used for discovery and ranking. Knowledge preparation starts from that source material, not a pre-structured knowledge base.

## Included in V1

The following is the accepted V1 target scope, not an inventory of delivered functionality. The [current implementation](../README.md#current-implementation) lists the available components, including resumable acquisition through Bronze and source registration. Silver/Gold processing, broader operational state, indexing, and knowledge-change measurements are not yet implemented; source lookup and acquisition progress do not provide those stages.

- **Repeated acquisition:** Manually initiate a logical run that attempts all configured Champion targets. Each run keeps its original catalog context and full target data across resumes. The implemented service integrates at most one source per target/run and stores progress, with a shared budget of at most three active target attempts and target-level failure isolation. The same attempt directly invokes Bronze integration and records `INTEGRATED` only after registration succeeds; downstream knowledge curation remains planned; no target is permanently complete after a source count or run.
- **Source acquisition and quality-controlled selection:** Seek 20 unique unseen YouTube candidates per target where available, excluding integrated source IDs and refilling as needed. Apply duration eligibility, then deterministic popularity/recency ranking to return up to three candidates. Acquire available English transcripts transiently. Stage 1 semantically assesses each transcript against an absolute minimum standard and returns structured attributes. Application code derives PASS only when target relevance, educational substance, and transcript usability are true and noise does not dominate. Zero PASS yields normal `NO_SELECTION`; one PASS is selected directly; two or three PASS enter a separate comparative LLM call. Only the selected source is handed to Bronze; the full payload remains transient until Bronze persistence. Normal search exhaustion is not a technical run failure; normal absence of a usable source is persisted as `NO_SELECTION`.
- **Bronze and Silver:** Preserve the admitted source's raw transcript and available provenance in Bronze. Clean reliably identifiable noise, organize logical sections, and retain timestamps and deterministic provenance in Silver without synthesizing League knowledge or imposing a semantic ontology.
- **Knowledge curation:** A Knowledge Curator inspects relevant existing Gold knowledge while processing one selected Silver source at a time. Gold is the canonical, versioned Markdown Knowledge Wiki with an adaptive structure, source provenance, and preserved contradictions. The curator may make no change, enrich, rewrite, create, merge, link, or flag a conflict; it does not autonomously delete knowledge in V1.
- **Local state and retrieval:** Local SQL state tracks integrated sources, run progress and per-target outcomes, raw knowledge-change counts, and index synchronization. A local retrieval/vector index is derived from Gold and updates changed documents incrementally, including removal of stale vectors where applicable. It is a serving layer, not the source of truth.
- **Knowledge-gain evaluation:** Compare raw per-run/per-target counts of new, enriched, rewritten, and merged concepts; links; conflicts; created or updated Wiki pages; and no-change integrations where applicable. V1 does not require a synthetic aggregate score or an automatic stopping threshold.

The V1 design uses local Bronze, Silver, and Gold files, local operational SQL state, and a local retrieval index. External source/search and LLM APIs are used where needed. Azure infrastructure, including Azure AI Search, is not required for V1; Azure remains a possible future deployment target.

Neither semantic selection stage receives popularity, recency, scores, discovery order, or original shortlist rank. The comparative stage receives only passing transcripts and no Stage-1 assessments, booleans, reason codes, or rationales. Full transcript content and original identity/timing remain available; technical selection failures produce `SELECTION_FAILED`, distinct from normal `NO_SELECTION`. Selection does not perform persistence or register integrated sources.

## Incremental acquisition and knowledge evolution

Already integrated IDs are excluded from later candidate collection. **Selected** means chosen by the selection operation; **integrated** means the Bronze API has successfully persisted the selected source and registered it in the catalog. Selection alone does not make a source integrated. Lookup/registration, separate resumable run state and end-to-end Bronze integration exist. Selected identity is reconciled before retry: already registered durable Bronze allows free state repair; missing registration or Bronze requires a bounded active attempt from the same three-attempt budget. Run schema v1 requires an explicit backed-up migration to v2. Bronze preserves one immutable artifact per global video ID, with the original integration provenance; later compatible target/run contexts do not rewrite it. New runs use the then-current catalog; existing runs retain their copied targets and totals even after catalog changes or removal of the original file.

In the intended repeated-run flow, one run may integrate Source A, a later run excludes A and may integrate B, and another excludes A and B while seeking C for the same Champion. The implemented run service coordinates successful Bronze persistence and registration; run selection alone does not exclude a source. Additional sources need not add useful new knowledge: planned curation may create, enrich, identify overlap, flag a conflict, or make no change after inspecting existing Gold. Persistent knowledge is intended to evolve over time instead of being rebuilt independently for each source. General concepts may emerge from Champion sources; this does not add concept-target acquisition to V1.

## Future comparative evaluation

A conventional fixed-size chunking plus retrieval baseline is planned and still needs implementation. It will be compared with agentically prepared, structured knowledge using the same source material and the same previously unseen multi-hop questions. Query-time comparison and full evaluation follow later, considering answer/retrieval quality, query-time efficiency, and preparation effort without assuming either approach wins.

The planned experiment supplements the accepted preparation architecture. Neither the baseline nor evaluation logic is implemented, and no latency reduction has been measured. Existing raw knowledge-gain requirements remain part of the V1 target scope; they are not completed evaluation results.

## Outside V1

- Systematic general-concept or dedicated Champion Matchup target acquisition. Such knowledge may still emerge from Champion sources and be represented in Gold.
- Automatic patch tracking or knowledge invalidation after game updates.
- Automatic scheduling, event-driven ingestion, or distributed workers.
- A fixed Champion × Role target matrix, fixed source quota, permanent Champion-completion state, or automatic marginal-gain stop threshold.
- A mandatory fixed Gold ontology, semantic labels on Silver chunks, or autonomous destructive deletion of Gold knowledge.
- Automatic live game-state reading; the player supplies relevant context.
- Persistent cross-game user memory. The separate Voice Coach may maintain current-game session state.

The [architecture](architecture.md) describes the flow and the [ADRs](adr/001-medallion-architecture.md) record the decisions and their history.
