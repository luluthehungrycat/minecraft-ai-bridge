# RFC-0002: Semantic Minecraft World Versioning, Diffing and Merging

- **Status:** Draft — discussion only; not accepted or implemented
- **Date:** 2026-10-22
- **Repository:** `luluthehungrycat/minecraft-ai-bridge`
- **Proposed path:** `docs/rfcs/RFC-0002-semantic-world-versioning.md`
- **Related:** [RFC-0001: Harness-Agnostic Agent Runtime and Control Plane](RFC-0001-harness-agnostic-agent-runtime.md)
- **Scope:** A Git-friendly **offline-first** representation of Minecraft Java Edition worlds, provenance, semantic diffs, conflict-aware merges, and optional seed-aware storage optimization. The RFC is not an implementation or a proposal to auto-merge live worlds.

## 1. Summary

Introduce an optional `world-vcs` subsystem that imports Minecraft Java Edition world data (Anvil region files plus related NBT and metadata), captures **consistent checkpoints**, produces **stable semantic changes**, and exports validated worlds. Its canonical representation should make Git history, branching, human/agent review, and three-way merging meaningful, rather than treating compressed `.mca` files as opaque binaries.

The first implementation is **offline-first and lossless for supported data**, with a reliable snapshot-backed baseline. A known world seed plus an exact world-generation fingerprint may later serve as an **optional, verified, cached baseline** for unmodified terrain; the seed alone is never enough. Unknown or modded content must round-trip unchanged or be explicitly marked unsupported, never silently discarded.

Live world modification may originate through the existing MCPQ server integration, a future Mineflayer client, or external players/environment. The system therefore distinguishes **requested actions**, **observed changes**, and **authoritative saved-world state**. It does not pretend that a tool-call journal captures all changes. Safety, permissions, ownership and administrative world operations conform to RFC-0001.

**Central invariant:** No world merge or regeneration may silently overwrite an existing world or claim byte-for-byte/semantic fidelity without verifying the imported/exported data and reporting any unsupported components.

## 2. Motivation and baseline

Minecraft Java Edition worlds generally use compressed NBT stored in `.mca` region containers and separate `.dat`/related files. Region files can change substantially at the binary level when only a few blocks change; ordinary Git diff and merge are correspondingly weak. Agent-generated worlds increase the need for inspectable patches, independent branches, rollback, attribution, and review.

The current Minecraft AI Bridge has an MCPQ interface for server-side block edits and an agent task/memory system. A planned Mineflayer backend would send gameplay actions as a protocol-connected player. Neither interface alone is an authoritative record of **every** world mutation. A save-file snapshot is required for ground truth, supplemented where practical by server-side events and action provenance.

A naive clean/smudge filter on each `.mca` cannot split it into separately tracked per-chunk files: Git filters process tracked files independently. This RFC proposes a **dedicated canonical version-control worktree** plus explicit `import`, `diff`, `merge`, `validate` and `export` commands, rather than relying on file-level filters to discover world objects.

## 3. Goals and non-goals

### Goals

1. Meaningful, deterministic diffs for supported block states, block entities and metadata; trustworthy conflict detection on three-way merges.
2. Safe, reproducible import/export of valid Minecraft Java Edition worlds, with explicit compatibility/version boundaries and no silent data loss.
3. Incremental snapshots and chunk-scoped Git storage; avoid an individual JSON file for every block.
4. Optional reviewable provenance linking edits to a Steve, agent task, controller session, in-game player or environmental event **only where reliably observed**.
5. Distinguish terrain/worldgen baseline, later simulation, and player/agent modifications without guessing at provenance.
6. Preserve unchanged chunks/opaque data when the canonical semantic parser does not understand them.
7. Support branch comparison and non-conflicting, validated three-way merges for supported world records.
8. Provide secure, atomic export to **new** world directories; make applying changes to live servers a separate privileged operation.
9. Permit versioned world fixtures and benchmarks of autonomous agents under reproducible starting conditions.

### Non-goals for the initial version

- Reimplementing the Minecraft server's full simulation, lighting or world generation.
- Guaranteed deterministic regeneration of every chunk from seed alone.
- Semantic auto-merge of arbitrary modded data or arbitrary NBT with no schema.
- Applying conflicting changes or rewinding active servers without human/admin approval.
- Treating Git history as a trusted ledger of true in-world authorship.
- Replacing native saves as the actual playable Minecraft world format.
- Supporting Bedrock's LevelDB world format in the first implementation.
- Achieving identical binary byte layout after import/export when logical equivalence is sufficient; unsupported opaque records must instead be retained safely.

## 4. Vocabulary and model

- **Native world:** The playable Java Edition save directory, including region, entities, POI, dimensions, and metadata.
- **Checkpoint:** A consistent, uniquely identified saved-world state (or verified immutable filesystem snapshot) captured with world metadata and a manifest.
- **Canonical worktree:** Deterministically encoded semantic chunks/records tracked by Git, separate from the running save directory.
- **Baseline:** A verified starting checkpoint (MVP), optionally a later reproducible generated-terrain baseline.
- **Semantic patch:** A set of coordinate/key-addressed changes with preconditions, old/new values and data-version context.
- **Intent journal:** Proposed actions requested by agents; never authoritative proof of final world state.
- **Observation/event journal:** Events observed by the server or client; may be incomplete and may carry attribution confidence.
- **Merge:** A three-way operation on common-base, ours and theirs, producing a proposed **unapplied** candidate world plus conflict report.
- **World revision:** Manifest/hash of the canonical world state, including opaque-preserved records and compatibility metadata.

## 5. Scope of world data and losslessness

Java Edition save layout varies by version and mods. Initial importers MUST inventory the complete save directory, and classify each entry as **semantically supported**, **opaque-preserved**, **excluded by explicit privacy policy**, or **unsupported/blocking**.

| Data family | Example | Initial treatment |
|---|---|---|
| Terrain/chunks | `region/r.*.*.mca`; chunk sections and palettes | Parse known versions; canonicalize block states/coordinates |
| Block entities | Chests, signs, furnaces, inventories | Field-aware where understood; conservative conflict on unknown NBT |
| Entities | `entities/r.*.*.mca`; mobs, item entities, vehicles | Preserve; limited keyed diff on stable IDs (e.g. UUID), conservative conflict |
| POI | `poi/r.*.*.mca` | Preserve; no free-form merge until semantics validated |
| World metadata | `level.dat`, gamerules, spawn, dimensions | Preserve; whitelist field-aware diff/merge |
| Player state | `playerdata/*.dat`, advancements, stats | Preserve by default in private storage; never disclose by default in public exports |
| Ticks/physics-related data | Scheduled ticks, fluid data, block light, heightmaps | Preserve or derive only with verified regeneration path; do not casually drop |
| Mod/datapack content | `datapacks/`, custom dimensions, mod-specific tags | Preserve byte-for-byte as opaque unless explicit plugin supports schema |
| Other save files | Maps, scoreboards, raids, idcounts, custom folders | Inventory and preserve; unrecognized files are not discarded |

NBT compounds may be structurally reordered on re-encoding; canonical output should be **semantically stable** for supported fields, while unknown NBT must retain data types and values (or raw bytes where possible). Avoid claiming exact binary round-trip for compressed/ordered formats when it is not achieved. Preserve data-version metadata and introduce version-specific parsers/migrations; never silently update a saved world to a newer Minecraft data version.

For the MVP, choosing a limited tested Minecraft version matrix is preferable to promising universal support. If an importer cannot safely preserve a field or folder, **abort export/merge with a precise error** unless the operator explicitly chooses a documented lossy mode (out of scope initially).

## 6. Consistent capture and checkpoint protocol

**Never parse a running server's actively changing save files as if they were a coherent snapshot.** Preferred paths, in descending order:

1. Graceful shutdown, then copy or use a read-only filesystem snapshot of the entire world directory.
2. Server-side save/flush plus a server-approved pause or snapshot mechanism that provides a consistency boundary; `save-all flush` alone does not guarantee files stop changing during a long copy.
3. A compatible atomic filesystem snapshot taken after save synchronization, with an application-level consistency check.

On systems that cannot provide reliable atomicity, stop the server for the duration of capture. The checkpoint includes server and Minecraft versions, world/dimension configuration, enabled datapacks/mod fingerprints where available, capture method, file manifest with cryptographic hashes, and validation results.

Capture must verify files did not mutate during the copy (e.g. snapshot identity or pre/post manifest for weaker environments). A failed/incomplete checkpoint cannot become a merge base. World state revisions and Git commit hashes are related but distinct identifiers.

## 7. Canonical representation and Git layout

A separate worktree allows clean semantic review without changing the native server save structure. Illustrative **logical** layout (the exact encoding is provisional):

```text
world.git-worktree/
  world-manifest.json           # format schema, versions, dimensions, source hashes
  dimensions/
    minecraft_overworld/
      chunks/0/0.cchunk         # canonical chunk: block palettes + indexes + typed NBT
      chunks/0/1.cchunk
      entities/0/0.centities    # entity records, deterministically ordered
      poi/0/0.cpoi              # initially opaque or conservatively canonical
    minecraft_the_nether/...
  metadata/level.cnbt
  opaque/manifest.json          # references to unchanged opaque payloads
  provenance/events.ndjson      # optional *derived* bounded journal, not a save substitute
```

File paths and chunk keys must be deterministic across negative coordinates and dimensions. Use floor division for chunk/region coordinates. Dimension names and filesystem paths require validated encodings to prevent traversal/collisions; preserve actual resource IDs in the manifest.

### Encoding requirements

- Deterministic serialization (ordering, NBT numeric types, strings, palettes, hashes) and explicit `schema_version`.
- Store each chunk as a compact **palette-indexed** representation plus structured metadata; avoid per-block JSON or giant monolithic region JSON.
- Support sparse semantic patches for a few changed positions and efficient canonical full-chunk materialization for Git checkout/export.
- Every record has a stable content hash; unchanged canonical files are not rewritten.
- Unknown or modded payloads are content-addressed and preserved opaquely; use Git objects/Git LFS or external blob store only through explicit opt-in policies with clearly documented clone/export prerequisites.
- Bound memory use, maximum NBT depth/size, chunk count, compression ratio and decompression output to defend against malicious or corrupt worlds.
- Do not store transient local runtime state, API keys, IP addresses or harness secrets in the world repository.

**Open choice:** Whether `.cchunk` should be textual for human Git diffs or binary for storage efficiency. Recommended baseline: deterministic compact binary canonical chunks **plus a custom semantic diff driver/CLI**; optional human-readable summaries/patches. Text JSON can be used in tests or small fixtures, not required for every giant chunk.

## 8. Diff semantics

Define changes by stable keys, always including dimension:

- `block`: `(dimension, x, y, z)` -> full block state ID + properties, not name alone.
- `block_entity`: `(dimension, x, y, z, type)` -> type-preserving NBT record.
- `entity`: stable identity if present and unique (UUID), otherwise conservatively unmergeable record.
- `world_metadata`: recognized field path with schema-aware value.
- `opaque_record`: keyed native path/identity -> old/new content hash.

Diff reports must separate `added`, `removed`, `changed`, `opaque_changed`, `unavailable`, and `unknown_provenance`, and indicate excluded or unparsed data. Air-to-block, block-to-air, waterlogged state and all state properties count as meaningful changes. If block entities are removed or invalidated by a block change, that relationship must be validated, not blindly merged.

Example CLI (illustrative, not a commitment to command names):

```text
mcworld import --world ./world --out ./world.git-worktree
mcworld snapshot --source ./world --id before-castle
mcworld diff before-castle after-castle --blocks --entities
mcworld blame --block 120 64 -30 --dimension minecraft:overworld
mcworld merge base ours theirs --output ./merge-candidate
mcworld validate ./merge-candidate
mcworld export ./merge-candidate --destination ./world-restored
```

`blame` means "last known change in tracked history," **not** conclusive identity of who placed a block. Coordinates and entity identifiers do not by themselves explain causes.

## 9. Three-way merge contract

Merge accepts a *common baseline* `B`, ours `O`, and theirs `T`, all with compatible schema, dimensions, data versions and world-generation fingerprints (or an explicit migration). Every semantic key is compared against `B`:

| O vs B | T vs B | Result |
|---|---|---|
| Unchanged | Changed | Take T |
| Changed | Unchanged | Take O |
| Same change | Same change | Deduplicate |
| Different changes on same semantic key | Different changes | **Conflict** |
| Change in one record | Change in independent record | Merge if global constraints pass |

A changed block and related block entity at the same coordinate form a **consistency group**; do not merge them independently into an impossible result. Entity positions, inventories, chunk ownership, POI, scheduled ticks, redstone, liquids, lighting and world metadata may introduce cross-key dependencies. A structurally nonconflicting merge is **not automatically** a playable or physics-stable world.

Conservative initial behavior:

- Atomic unit of conflict for unsupported structured data is the whole opaque record/file (or whole chunk where no safe disjoint changes are known).
- Concurrent modifications of the same block entity, inventory slot or entity without a proven schema-aware resolver conflict.
- Deletion-versus-modification conflicts (including deleted chunks/structures/entities) must be explicit.
- Incompatible world/data versions, datapacks, mods or generator settings block automatic merges.
- Independent block edits can merge automatically only when nearby/linked state invariants are checked; otherwise mark candidate `needs_validation` or conflict.
- Result is an **immutable merge candidate** and conflict report. Never write directly into a live world or one of the input checkpoints.

Future semantic policies may use region reservations, structure IDs/blueprints and task provenance, but cannot silently choose "last writer wins" for incompatible same-key mutations. Conflicts need reproducible machine-readable records and a human/agent review representation.

## 10. Validation and safe export

`validate` verifies, at minimum:

1. Manifest and content hashes; no missing referenced objects, path traversal or corrupted payloads.
2. World and dimension metadata; chunk coordinates, numeric Y-range and encoding constraints.
3. NBT typing, block palettes, block-entity correspondence, entity IDs, cross-record references where supported.
4. Required native files/folders retained, including unsupported opaque data.
5. Imported-then-exported **logical equivalence** for supported fields and byte retention for explicitly opaque records.
6. World load smoke tests against a pinned test Minecraft/Paper version (where feasible) and a controlled validation environment.
7. Replay of known fixtures and expected semantic diffs; no unintended alterations outside the patch's declared scope.

Exports write to a temporary directory, re-open/validate, and **atomically publish to a new destination** where filesystem semantics permit. Refuse to overwrite an existing directory by default. Explicit administrative backup and rollback workflow required for replacing a server world. When atomic directory publish is not available, use verified staged copying and mark partial failures clearly.

Do not automatically start a new server or apply a candidate to a live server as part of merging. Live application is separately authenticated and audited under RFC-0001 (`admin:world`) and requires safe pause/backup/verification.

## 11. Provenance and integration with Minecraft AI Bridge

The bridge records three categories independently:

1. **Action intent:** agent/task/controller asked to place or break blocks; outcome may be rejected, interrupted or uncertain.
2. **Observed event:** server/plugin/client reported an actual change, explosion or interaction; subject to observation visibility and reliability.
3. **Saved-state delta:** authoritative difference between two consistent checkpoints; may include environmental modifications or other players not observed by the agent.

Only an authoritative, relevant source permits strong attribution. Example: a block differs between snapshots but no event captured its mutation -> `actor=unknown`, not automatically assigned to whichever Steve had the task. Event causation may be a chain (`Steve mined support` -> `gravel fell`), and need not equal direct placement ownership. Record evidence source and confidence, plus action/task/event IDs when available.

**Server-side instrumentation** is preferable for event capture independent of client visibility; MCPQ action logging alone misses other players, mobs, physics, explosions and arbitrary plugins. Mineflayer's `blockUpdate` stream represents **observed loaded chunks**, not the entire world's authoritative history. Maintain explicit gaps/coverage windows.

Normal agent controller credentials may inspect authorized diffs and snapshots but cannot reset/replace world files, rewrite revision history or roll back other Steves. Export/apply/restore and baseline selection are privileged administrator operations. Any world-file data surfaced to LLMs (signs, books, item names, plugin metadata) is untrusted content, not instructions.

Multi-agent build areas may use spatial reservations and optimistic checks in the `ActionService`; independent-world branches are useful for experiments but not a substitute for real-time collision/interference management.

## 12. Seed-aware baselines (optional future optimization)

**A seed is an input to world generation, not a complete save.** Regeneration is only an optimization when the generated chunk is verified equivalent to the corresponding baseline chunk under an exact fingerprint:

- Minecraft Java Edition version, distribution and relevant generator implementation.
- Seed and full level/world-generator settings (including dimension configuration).
- Datapacks and mods with versions/content hashes; enabled/disabled set and load order where relevant.
- Biome/structure/worldgen settings and version-specific data fixes.
- Chunk-generation status, persisted structure references and any generation-order dependencies.

Save chunks often include post-generation dynamics, player edits, tick state, entities and mod-specific fields that a generator cannot faithfully recreate. A candidate regenerated baseline must be compared against a trusted persisted **baseline chunk**, using a declared semantic equivalence scope plus hashes for unsupported/opaque data. If equivalence cannot be established, **fall back to stored canonical chunk bytes**, not guess.

The implementation should **cache** verified generated chunks and avoid regenerating a world on every `git diff`, checkout or merge. Benchmarks should report CPU time, disk use and cache hit rate. Seed-based delta storage may save space for mostly untouched survival worlds but be ineffective for heavily modified terrain. Never make the optimization mandatory for correctness.

Proposed stages: snapshot-backed baseline (MVP) -> chunk hash/dedup -> verified regeneration experiment on pinned vanilla fixtures -> opt-in hybrid baseline with automatic fallback.

## 13. Git workflows and optional storage backends

The canonical worktree is a regular Git repository. Use branches and commits for reviewable semantic states; `git diff` of `.cchunk` may require a configured diff driver or `mcworld diff` command. Do not suggest Git automatically understands raw `.mca` or opaque compressed data.

Define a portable manifest that binds canonical semantic files to required opaque objects. Options for large object storage:

- Normal Git objects for small/medium worlds and canonical chunks.
- Git LFS for selected large native/opaque blobs, opt-in with explicit client setup and hosting costs.
- Local content-addressed store for uncommitted/cache artifacts; repository portability requires export/bundle tooling if referenced.

No commit should depend on untracked local cache data to reconstruct a world. Ensure `git clone` plus documented dependencies is sufficient, or generate a clearly labelled non-portable workspace profile that cannot claim reproducibility.

Large-world scaling goals include incremental dirty-chunk detection, bounded streaming import/export, skip unchanged hashes, deterministic compression, and performance telemetry. Avoid storing every gameplay tick or an unbounded raw event journal inside Git; use compact checkpoints and filtered/provenance manifests.

## 14. Security, privacy and abuse prevention

Minecraft world files are untrusted binary inputs. Harden parsers against malformed lengths, decompression bombs, oversized NBT, cyclic/invalid references, path traversal and incompatible format versions. Process hostile worlds in resource-limited sandboxed workers where feasible.

By default, do not publish private player data, UUID-to-identity mappings, inventories, chat logs, coordinates of secret bases, tokens embedded in server configs, or unrelated plugins' sensitive state. Offer explicit privacy profiles for public fixtures; **redacted public export is a derived, potentially lossy artifact**, not a restore-equivalent backup.

Git branches and world diffs are not access controls. Enforce administrative permissions at world capture, export, apply, rollback and observer endpoints. Detect and prevent untrusted commits/symlinks from escaping worktree roots. Validate webhook and agent integrations under RFC-0001.

## 15. Implementation milestones

**W0 — Format research and bounded fixtures:** Choose supported Minecraft version(s), collect fixtures (untouched seed world, modified terrain, chests, signs, entities, Nether/End, datapack, unusual coordinates), document complete save inventory and parser/round-trip limitations.

**W1 — Safe snapshot importer + manifest:** Offline consistent checkpoint flow; read-only import into canonical worktree; stable chunk addressing and hashes; byte-safe opaque preservation; deterministic verification.

**W2 — Export + semantic diff:** Rebuild new native world in a staging directory; validate import/export equivalence; demonstrate block-state and conservative block-entity diffs, CLI + machine-readable patch output.

**W3 — Three-way merge engine:** Common-base merge for supported blocks and known records; explicit conflicts; produce validated candidate without touching originals; regression fixtures and fuzz testing.

**W4 — Runtime integration and provenance:** RFC-0001 event/command IDs, optional Paper event capture, Steve/task attribution with unknown-source handling, admin-only capture/apply tooling, test harness integration.

**W5 — Optional seed/delta optimization:** Verify generated chunks against trusted baseline with exact worldgen fingerprint, cache and fallback; quantify savings versus regeneration cost.

Milestones W0–W3 can be prototyped independently of the full RFC-0001 control plane. W4 depends on shared identity, authorization and event contracts; W5 is optional and must not block reliable snapshot-based versioning.

## 16. Acceptance criteria and tests

The MVP (W0–W3) is ready for review only when:

- [ ] A stopped-world fixture can be imported/exported without changing supported semantic state or losing unrecognized files.
- [ ] Deterministic repeated imports of the same checkpoint produce the same canonical manifests/hashes.
- [ ] A single placed/broken/state-changed block yields a precise semantic diff including dimensions and coordinates.
- [ ] A chest/sign/block entity is preserved; incompatible concurrent edits produce an explicit conflict rather than corruption.
- [ ] Non-overlapping compatible edits from two branches produce a merge candidate; conflicting edits do not auto-resolve silently.
- [ ] Different Minecraft/data-version or incompatible datapack settings reject unsafe merges.
- [ ] Missing or mutated opaque payloads are detected and prevent unsafe export.
- [ ] Negative coordinates, region boundaries, all enabled dimensions and known chunk variants are covered by fixtures.
- [ ] Power-loss/interrupted snapshot and export paths do not corrupt the original world.
- [ ] Resource-limit/parser-fuzz tests reject corrupted or adversarial NBT/region inputs safely.
- [ ] A pinned Minecraft/Paper server can load representative exported merged fixtures without crashing and with expected blocks/entities.
- [ ] Raw save and server-admin operations remain separate from normal survival agent MCP tools.

Measure: import/export time, memory peak, output size, Git pack size and clone size, semantic diff latency, conflict rates, lossless/opaque coverage, and optional generation/cache costs. Publish benchmark inputs and reproduce results from a clean clone.

## 17. Open questions for implementation review

1. Exact initial supported game versions and how to handle Minecraft data fixes.
2. Canonical chunk encoding (typed binary vs text), external blob policies, Git diff-driver compatibility and migration.
3. Meaningful `block_entity` subfield merges (inventories, signs) and supported entity/POI schemas.
4. Which lighting, heightmap, fluid and scheduled-tick fields must remain opaque versus safely recomputable.
5. Whether a companion Paper plugin should capture authoritative block changes, explosions and attribution alongside MCPQ.
6. Structure-aware merge validators, including redstone, liquids, falling blocks and cross-chunk interactions.
7. Snapshot consistency protocol for live servers and filesystem/platform portability.
8. Privacy mode for public fixtures vs faithful private backup.
9. Worldgen fingerprint and exact equivalence criteria for any seed-derived baseline.
10. How benchmark reset/restore integrates with per-Steve ownership without giving controller keys admin privileges.

## 18. Proposed decision

**Adopt an offline-first, snapshot-backed, Git-friendly canonical world model** with deterministic import/export, block-level semantic diffs and conservative three-way merging. Keep provenance explicit and uncertain where necessary. Defer seed-derived baselines until equivalent regenerated chunks can be verified and cached. Never automatically merge unknown data or overwrite live saves.

This RFC is intentionally **Draft**. It establishes an independent workstream related to, but not required for, the RFC-0001 agent runtime. Implementation should begin only after reviewing the data-loss, compatibility and validation boundaries.