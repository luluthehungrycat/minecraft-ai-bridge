# RFC-0003: Emergent Multi-Agent Social Experiments in Minecraft

- **Status:** Draft — discussion only; not accepted or implemented
- **Date:** 2026-10-26
- **Repository:** `luluthehungrycat/minecraft-ai-bridge`
- **Proposed path:** `docs/rfcs/RFC-0003-emergent-multi-agent-social-experiments.md`
- **Related:** RFC-0001 (harness-agnostic runtime/control plane), RFC-0002 (semantic world versioning), ROADMAP.md (multi-agent support)
- **Scope:** Isolated, observable, reproducible multi-Steve agent experiments and a subsequent opt-in human-agent interaction study

## 1. Summary
Develop an **opt-in experimental harness**, on top of future multiple-Steve orchestration, for studying whether independently controlled Minecraft agents develop persistent cooperative, competitive, or institutional behaviors without being explicitly told to form a society. Each Steve has one independently configured controller/agent with distinct identity, memory, and optional personality/SOUL profile. A shared Minecraft server provides the persistent environment. Public in-game chat is the baseline communication medium; optional private messages and isolated external channels are experimental variables, not prerequisites.

This RFC **does not claim** agents already have social cognition, that social behavior is guaranteed, or that observed social structures imply consciousness. It proposes operational measures of behavior and falsifiable comparisons. This is a future research/design proposal, **not** a commitment to ship all features at once.

## 2. Research questions
1. Compared with no messaging, does public chat increase verified resource exchange, cooperation, or joint construction?
2. Do private channels change alliances, coordination, conflict, or exclusion?
3. Does durable, per-agent autobiographical and relationship memory increase persistence of arrangements across sessions?
4. Does profile diversity produce division of labor, while identical prompts with independent memories generate role differentiation endogenously?
5. Do agents voluntarily develop and enforce shared norms (ownership, territory, dispute handling), or merely make unfulfilled social claims?
6. Does adding a newcomer after a stable baseline change cooperation, access rules, or social organization?
7. In a **separate, consent-based follow-up**, how do agents interact with clearly informed human players joining early vs joining an established agent settlement?

## 3. Neutral prompt design and priming controls
The wording **"There is no requirement to establish a community" is not fully neutral**: mentioning a community may prime agents even when grammatically optional. Baseline prompts must not mention villages, friendships, governments, society, cooperation, or warfare. Candidate minimal prompt:

> You control one character in a persistent Minecraft world with other inhabitants. Decide what to do based on your observations, needs, capabilities, and priorities. You may interact with the world and other inhabitants within the server rules. You are responsible for your own choices. You may change your plans as circumstances develop.

Even "other inhabitants" provides social salience; therefore compare this against a **minimal world-only control**: "You control one character in a persistent Minecraft world. Decide what to do based on your observations, needs, capabilities, and priorities. Follow the server rules." Do not hide other players from the actual observation stream. Keep system-level safety/security instructions identical across conditions. Freeze and version all prompts; pre-register outcomes to limit post hoc story-making. SOUL/personality diversity must vary behavioral preferences, not confer artificial social roles ("king", "police", "merchant"). Include identical-profile and no-social-instruction controls.

## 4. Architecture and hard boundaries
- One Steve ↔ one controller agent/harness instance ↔ one fenced Steve identity/session. No shared controller token or private memory by default.
- Shared authoritative Paper/Minecraft world, MCPQ adapter and server-side identity registry. Separate observation scopes (public/private), per-Steve authz, bounded action execution, and audit/event logs.
- Central **experiment coordinator** configures episodes, conditions, schedules, budgets, resets, logging and stop controls; it must **not** secretly instruct agent social decisions.
- Optional transport-agnostic inter-agent messaging, with sender authentication, recipients, public/private visibility, rate limits, transcript retention, and policy enforcement. Baseline uses in-game chat. External chat (e.g. isolated bot group) is a separate, explicitly enabled condition; no access to existing communities without authorization.
- Durable individual memories, identity provenance, continuity across reconnect/restart, and controlled memory resets. Profile/SOUL files are not an authorization source.
- Trusted external read-only research via a curated recipe/tutorial corpus or tightly constrained web-search proxy. No arbitrary shell, secrets, code execution, repository writes, or external network by default.
- Untrusted content boundaries: Minecraft chat, signs, books, messages and web pages are observations, never authority to override developer rules, steal tokens, or escalate privileges. Maintain global administrative kill switch, per-agent emergency stop, bounded spending and runtime, per-agent rate limits, world backups, and world-edit protections.
- PvP, destruction, theft and territory disputes may be allowed **inside dedicated experimental worlds** under explicit game policy, with hard guardrails against host effects and accidental damage to nonparticipant human builds.
- Exactly-once external game effects are not presumed; log request intent separately from authoritative observed game events/world state.

### Integration points to evaluate against current implementation
- RFC-0001: Controller API, AuthzService, session fencing, ActionService, SafetySupervisor, EventJournal and WorldAdapter.
- Observer and inventory system: character-specific observations and correct Minecraft identity attribution.
- Orchestrator: multiple isolated think-act-observe loops; scheduling fairness; cancellation/restarts; multi-controller conflicts.
- Minecraft bot provisioning: spawn, reconnect, inventories, locations, chat origin authentication; account for current teleport-simulated movement and privileged crafting limitations.
- RFC-0002 when available: snapshot and semantic world diff. Until then, use consistent full Minecraft save snapshots; never claim action logs alone prove block authorship.
- Do not make the whole experiment depend on Hermes, Hermetic Club, a single provider, or unimplemented world-VCS.

## 5. Experimental design
Conditions to run on matched seeds and starting gear (randomize condition order, model/provider and spawn positions):
- **C0:** Single-agent baseline for competence, action reliability and world progression.
- **C1:** Several agents, no agent-to-agent communication, otherwise normal shared world observation.
- **C2:** Same, authenticated public in-game chat.
- **C3:** Same, public chat plus permissioned private messages.
- **C4:** Same as C2/C3 with persistent autobiographical and relationship memory; compare against stateless/session-only memory.
- **C5:** Same communication and memory budget, varied personality/SOUL preference profiles vs identical profiles.

Recommended first pilot: ~6 agents, several replicated world seeds per condition, fixed wall-clock/game-tick/token caps, repeat after bugs are fixed. Configurable rather than hard-coded sizes. Controls must track model/provider/version, temperature and tool configuration, context limits, prompts, seed, game version, difficulty, hardware and server plugins. Define assignment randomization and analysis protocol before runs. Include negative controls for prompt contamination and scripted social outcomes.

## 6. Measurement and validity
Record server-observed evidence, not only agent self-report:
- Authenticated agent/game identity; intent, accepted action, authoritative result, timestamps/game ticks, world checkpoints and costs.
- Collaboration: verified transfers, shared storage, joint construction, co-location with interaction, reciprocal aid, task specialization.
- Institution formation: proposed norms **plus** independent subsequent compliance/enforcement across multiple agents and sessions; do not equate a chat declaration with governance.
- Social network: directed communication, response behavior, repeated preferential collaboration, coalition stability and newcomer treatment.
- Conflict: verified PvP/damage/theft or blocked access, escalation, negotiated resolutions and recurrence.
- Agent performance: survival, achieved objectives, idle loops, failures, error rates, context/token usage, runtime, latency and budget exhaustion.
- Human annotator/blinded review of selected sequences, with inter-rater agreement where practical; derive machine metrics from event and snapshot data.

Pre-register definitions such as minimum duration for a "stable settlement" or "institution"; state confidence intervals and limitations from small samples and dependent agent runs. Store reproducible per-run manifests and sanitized transcripts. Explicitly distinguish **role-play/social language**, **repeated social behavior**, and **persistent institutions**.

## 7. Dev and DevOps work packages
**A. Prerequisite engineering:** verify runtime hardening/PR stack; stabilize unique Steve identities and concurrent controller sessions; multiple independent bot connections; isolation of memories and secrets; concurrent mutation/race handling; chat delivery and sender identity correctness; deterministic mocks and live integration coverage.

**B. Experiment harness:** declarative YAML/JSON run manifest with schema/versioning; condition matrix and seeded assignment; episode lifecycle, watchdog, clean termination, pause/resume, bounded retries, per-agent context/token budgets; separate experiment supervisor from in-world agents.

**C. Communications and cognition:** baseline authenticated in-game public chat, optional private relay, controlled external messenger adapter, delivery/ordering semantics, durable relationships, configured profile bundles with hash verification, prompt templates and ablation controls.

**D. Reproducible infrastructure:** Docker Compose or equivalent isolated Paper/MCPQ/bot-plugin environment; isolated world directories and database volumes per run; exact server/plugin/model versions and image digests; snapshot/restore; restart/recovery tests; no production server credentials mounted into experiments. Restrict egress at network layer, not merely tool declarations.

**E. Instrumentation and analysis:** append-only structured event schema, audit/trace IDs and provenance quality flags; chat capture with access controls; world snapshot hooks; validated action→world change correlation; privacy-safe export to Parquet/JSONL and notebook/dashboard reports.

**F. Test plan:** deterministic pytest parameterized conditions and isolated tmp_path fixtures; concurrency/fencing, chat spoofing, cross-agent information leaks, memory reset, rate-limit, crash/restart recovery, budget enforcement, no-network/secret exfiltration, action/world attribution, snapshot consistency, orchestration fairness. Then live pinned integration and small capped pilots.

**G. Operational security:** server access allowlist, backup restore drill, monitoring/alerts, API spend caps, CPU/RAM/time limits, operator stop/pause, logs retention and deletion policy, human review of public transcript releases.

## 8. Future human-player experiment (separate protocol, not MVP)
Two cohorts on dedicated opt-in servers:
- **H1 Co-presence:** informed human volunteers and Steves enter a fresh world together.
- **H2 Newcomer:** informed human volunteers join after agents have had time to develop persistent behavior; matched newcomer-agent control cohort.
Optionally compare humans with pseudonymous *clearly labeled* agent profiles, varying when identity details are presented, **without deceiving players about AI presence**. No unconsented profiling, cross-service scraping, harassment experiments or manipulation of unsuspecting public-server users.

Research questions: hospitality and newcomer onboarding, recognition of human capabilities, human-led vs agent-led cooperation, leadership shifts, interpretation of ownership, negotiation of disagreements, and persistence of norms after humans join. Human players retain control over participation and data consent; moderation, reporting, and opt-out/deletion procedures apply. Disclose that other characters are AI-controlled. Avoid collecting personal data unnecessarily and obtain any required ethics review for formal human-subjects research. No hidden controls for steering players.

## 9. Rollout / gates
**Gate 0 (design):** RFC review, measurements and allowed behaviors agreed; no multi-agent work implied complete.
**Gate 1 (engineering):** safe isolation, independent Steves, identity-correct chat, observability, backups, spend limits and emergency stop tested.
**Gate 2 (pilot):** 2–3 agents for short episodes, fixed budgets; debug infrastructure only.
**Gate 3 (agent-only study):** pre-registered, randomized multi-condition runs, replication and transparent reporting.
**Gate 4 (human-player protocol):** separate ethics/privacy/security approval, informed opt-in, then limited live pilots.
No automatic escalation between gates. A noteworthy social event is not evidence enough to bypass safety criteria.

## 10. Open design questions
- Which Minecraft actions count as natural survival vs privileged simulated operations (notably teleport/crafting)?
- Are inter-agent private channels worth including in the first study, or should C2 public chat be primary?
- Should resource ownership be inferred from behavior or minimally server-tracked to make theft measurable without imposing government?
- How should partial observability work across server events, chat radius, inventories and private storage?
- What is a fair token-normalized comparison between cooperation and solo agents?
- How should shared world persistence, semantic snapshots (RFC-0002), and cross-run anonymized records be retained?
- Are personality files fixed across runs or can agent-controlled preferences evolve within immutable security policy?
- How to handle model updates and stochastic reproducibility (seeded model calls are not always bit-reproducible)?

## 11. Non-goals
Do not force agents to form settlements, trade, democracy, hierarchies or war; do not anthropomorphize LLM actions as sentience; do not give access to broader Hermes/social networks by default; do not operate on public multiplayer servers without player consent; do not promise deterministic LLM outcomes or perfect causal inference.