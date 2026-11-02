# Project Roadmap

> **Minecraft AI Bridge** - Development Roadmap and Priority Tracking
> Last Updated: 2026-11-01

This document outlines the development priorities, completed work, and future plans for the Minecraft AI Bridge project.

---

## 🎯 Current Status

**Version**: 0.5.1 released 2026-07-11; 0.6.0 unreleased on `master`
**Test Coverage**: 878 deterministic tests plus 14 live integration tests (Paper 26.2 build 123/MCPQ, an LLM provider for the agent loop)
**Overall Health**: ✅ Built-in agent stable; RFC-0001 runtime and MCP controller surface landed through Phase B and most of Phase C

---

## ✅ Completed Milestones

### P0: Critical Fixes (All Complete)
- [x] Fixed all confirmed bugs (B1-B19, see `docs/bugs.md`)
- [x] B1: Turn left/right command format
- [x] B2: Craft item uses `/give` (documented limitation)
- [x] B3: Drop item now spawns entities
- [x] B4: Goal tree parent tracking uses object references
- [x] B5-B8: OpenCode session management, config validation, logging
- [x] B9: Inventory NBT parsing into structured data
- [x] B12: Biome detection via surface block heuristics
- [x] B13: Consecutive failure protection with graceful shutdown
- [x] B14: Safe string formatting (string.Template)
- [x] B15: Ollama JSON mode with fallback
- [x] B16: botsummon retry with polling
- [x] B17: Memory deduplication
- [x] B18-B19: Chat commands and inventory refresh

### P1: High Priority (All Complete)
- [x] I2: Structured inventory parsing (`InventorySlot`, `_parse_inventory_nbt()`)
- [x] I3: Health parsing as float (`_parse_nbt_value()`)
- [x] I4: Biome detection (`McpqClient.get_biome()`, `WorldState.biome`)
- [x] I5: Safe string formatting for LLM prompts
- [x] I6: Error loop protection (consecutive failures → graceful shutdown)
- [x] I7: Type annotations for handlers
- [x] I8: RCON deprecation (marked in docs)
- [x] I9: Pre-commit hooks configuration
- [x] I11: Ollama compatibility (graceful fallback for JSON mode)
- [x] I12: Configurable botsummon retry (10 attempts at 1s intervals)
- [x] I13: Memory deduplication (skip unchanged observations)

### P1: New Features (Phase 4 - All Complete)
- [x] I25: Persistent memory (SQLite-backed facts survive restarts)
- [x] I26: In-game chat commands (`!status`, `!stop`, `!goal`, `!goto`, `!follow`, `!come`)
- [x] I27: Structured inventory manager (`has_item()`, `count_item()`, `get_summary()`)
- [x] I28: Structure respect rules (prompt guidance to not build over existing builds)

### RFC-0001 Runtime and MCP Controllers (umbrella issue #18)
- [x] Phase A: contract schemas, `WorldAdapter`, shared `ActionService` gate with audit and request-ID idempotency (durable via `SqliteActionOutcomeStore`)
- [x] Phase B: local credentials and scopes, one exclusive fenced controller session per Steve, `ControllerService`, `minecraft-ai-bridge-mcp` over stdio and Streamable HTTP
- [x] Per-credential world delegation scopes (#24) and reach limits (#25)
- [x] Session pruning and close/renew races (#26, #36); `!follow` target validation (#21)
- [x] Independent safety supervisor for the built-in agent and MCP-controlled Steves; stale queued actions refused with `SAFETY_PREEMPTED`
- [x] Durable event journal, MCP event polling/ack, signed webhook sender
- [x] Durable bounded task plans with pause/resume/cancel and restart recovery (#40)

### Testing
- [x] 878 deterministic tests passing (MockMcpqClient-based, no server needed)
- [x] Integration tests require Docker-provisioned pinned Paper 26.2 build 123/MCPQ and an LLM provider
- [x] Goal-verification helpers (`actions_taken()`, `position_reached()`)

### P2: Observer and Prompt Reliability (Complete)
- [x] Observer hardening: robust inventory SNBT, health parsing, and authoritative dimension-aware biome detection
- [x] Bounded prompt context and notable facts
- [x] Explicit failure-recovery hints with one guarded next-turn retry
- [x] Identical retry rejection without repeating side effects

---

## 🚀 Upcoming Priorities

### P1: Release Readiness (Current)

#### CI/CD Setup
- [x] **Set up GitHub Actions secrets** for integration tests
  - `OPENROUTER_API_KEY` for LLM inference tests
  - Consider `MISTRAL_API_KEY` as alternative
- [x] Run deterministic unit tests on all pushes and pull requests
- [x] Gate live integration tests on `RUN_LIVE_INTEGRATION=true` plus the provider secret
- [x] Run Ruff linting and formatting checks
- [x] Provision Paper 26.2 build 123/MCPQ/bot-plugin infrastructure for live CI integration tests
- [x] Re-enable mypy in CI (#2)
- [x] **Test matrix** for Python versions (3.11, 3.12, 3.13)

#### Code Improvements
- [x] Fix auto-step logic to not step over hazards
- [x] Update command format tests to match current code
- [x] Add fallback plans for Nether portal construction, End portal activation,
  redstone circuits, animal farming, and villager trading

### P1: Finish RFC-0001 Phase C (Next)
- [ ] Event producers beyond `safety.intervened`, `controller.changed`, and `task.*`: damage/attack received, creeper damage, health and hunger warnings, unexpected world changes
- [ ] Wire webhook subscriptions into admin config and run the sender from `minecraft-ai-bridge-mcp`
- [ ] Live test for the Phase C gate: emergency response without an LLM answer, continuing across an MCP client disconnect; no duplicate mutations from replays across restarts

### P1: RFC-0001 Phase D — Mineflayer Backend
- [ ] Design spike: backend capability parity matrix, sidecar protocol, `steve_id` to in-game identity mapping, Mineflayer version support vs Paper 26.2
- [ ] Optional Node.js sidecar behind `WorldAdapter`; observe/move/dig/place/stop through the same `ActionService`

### P2: Medium Priority

#### Movement System
- [ ] **WASD-style movement** (Phase 4 feature)
  - Replace teleport-based movement with physics-based walking
  - Requires MCPQ plugin support or command-based simulation
  - Implement proper collision detection
  - Add momentum and inertia

#### Survival Mode Support
- [x] **Health and hunger management**
  - Monitor and maintain health
  - Find and consume food
  - Avoid dangerous situations
- [x] **Mob detection and reflex combat**
  - Detect nearby mobs and avoid dangerous situations
  - Attack manageable threats and flee from overwhelming threats
- [ ] **Weapon selection and usage**
  - [x] Select and equip strongest supported melee weapon in the hotbar
  - [ ] Extend selection to main-inventory transfer and broader loadout policy
- [ ] **Armor management**
- [ ] **Proper crafting** (not just `/give`)
  - Recipe matching
  - Crafting table interaction
  - Smelting and processing

#### Pathfinding Enhancements
- [ ] **3D pathfinding** (not just XZ plane)
- [ ] **Jump/parkour capabilities**
- [ ] **Avoidance of player-built structures**
- [ ] **Better obstacle detection**

#### Planning System
- [ ] **Multi-turn planning**
  - Allow LLM to plan multiple steps ahead
  - Implement plan validation

### P3: Low Priority (Future)

#### Infrastructure
- [ ] **Health check endpoint** for bridge service
- [ ] **Log rotation / structured logging**
- [ ] **mypy strict mode** configuration
- [ ] **PyPI publishing pipeline**
  - Set up `pypi-publish` workflow
  - Version management
  - Package metadata
- [ ] **Add `__all__`** to all public modules
- [ ] **Config schema generation** (JSON Schema from pydantic)

#### Multi-Agent Support
- [ ] **Multiple AI agents** in same world
- [ ] **Coordination between agents**
- [ ] **Task delegation**

#### Advanced Features
- [ ] **Structure preservation**
  - Detect existing player builds
  - Avoid building over structures
  - Respect village boundaries
- [ ] **Biome awareness**
  - Detect current biome
  - Biome-specific strategies
  - Resource location by biome
- [ ] **Time-based behaviors**
  - Day/night cycles
  - Sleep in beds at night
  - Avoid hostile mobs at night

---

## 📋 Release Plan

### v0.5.0
- All critical bugs fixed
- Phase 4 features complete (chat commands, inventory manager, persistent memory)

### v0.5.1 (Released 2026-07-11)
**Focus**: Observer hardening and prompt recovery

- [x] Robust inventory, health, and biome observation
- [x] Bounded prompt context and notable facts
- [x] Failure hints and one guarded next-turn retry

### v0.6.0 (Next Minor Release)
**Focus**: CI/CD matrix, survival reflexes, and the RFC-0001 runtime with MCP controllers

- [x] GitHub Actions CI with deterministic unit tests
- [x] Configure the opt-in `OPENROUTER_API_KEY` repository secret for integration tests
- [x] Add Python 3.11, 3.12, and 3.13 test matrix
- [x] Health and hunger management
- [x] Mob detection and reflex combat
- [x] Shared action gate, authorization, controller sessions, and `minecraft-ai-bridge-mcp`
- [x] Independent safety supervisor, durable events, and durable tasks
- [ ] Live integration run against the Podman stack before tagging

### v0.7.0
**Focus**: Phase C completion, movement, pathfinding, and survival crafting

- [ ] Remaining Phase C event producers and webhook wiring
- [ ] Weapon selection, armor management, and proper crafting/smelting

- [ ] WASD-style movement
- [ ] 3D pathfinding
- [ ] Jump/parkour capabilities
- [ ] Multi-turn planning
- [ ] PyPI publishing

### v1.0.0 (Stable Release)
**Target**: 3-6 months
**Focus**: Production readiness, full feature set

- [ ] All P1 and P2 items complete
- [ ] Comprehensive documentation
- [ ] Performance optimization
- [ ] Security audit
- [ ] User testing and feedback

---

## 🧪 Testing Strategy

### Unit Tests (878 deterministic tests)
- **Run in CI**: ✅ Yes (on all pushes/PRs)
- **Dependencies**: None (MockMcpqClient)
- **Coverage**: Action handlers, NBT parsing, memory, goals, config, inventory, chat commands, pathfinding, orchestrator recovery, runtime gate, authorization, sessions, safety, events, tasks, webhooks, and the MCP adapter

### Integration Tests
- **Run in CI**: ⚠️ Only when `RUN_LIVE_INTEGRATION=true`
- **Dependencies**: Docker, pinned Paper 26.2 build 123 + MCPQ + bot plugin, and LLM provider

### Manual Testing
- **Local Docker setup**: Required for full end-to-end testing
- **LLM providers**: Test with OpenAI, Anthropic, Ollama, OpenRouter, OpenCode Server
- **Minecraft versions**: Test against pinned Paper 26.2 build 123; update the version/build and Docker image digest together.
---


## 🔧 Development Workflow

### Branching Strategy
- **main/master**: Stable releases only
- **feature/***: New features (PR to main)
- **fix/***: Bug fixes (PR to main)
- **vibe/***: AI agent work (PR to main)

### Pull Request Requirements
- [ ] All unit tests pass
- [ ] Code follows project conventions
- [ ] Documentation updated
- [ ] Changelog entry added

### Commit Messages
- Use imperative mood ("Fix bug" not "Fixed bug")
- Include issue/PR references
- Keep first line under 50 chars
- Add detailed description if needed

---

## 📊 Success Metrics

| Metric | Current | Target (v1.0) |
|--------|---------|---------------|
| Unit Test Coverage | 878 tests | Keep growing with features |
| Integration Tests | 14 tests | 30+ tests |
| Bug Count | ~5 minor | 0 critical |
| Documentation | 90% complete | 100% complete |
| CI Status | ✅ Lint, mypy, unit matrix; live tests opt-in | ✅ Full |
| PyPI Package | ❌ Not published | ✅ Published |

---

## 🤝 Contribution Guidelines

### Getting Started
1. Fork the repository
2. Clone and install: `pip install -e ".[dev]"`
3. Run tests: `pytest tests/ -k "not integration"`
4. Pick an issue from the roadmap

### Code Review
- Follow project conventions (see `AGENTS.md`)
- Add tests for new functionality
- Update documentation
- Keep changes focused and small

### Reporting Issues
- Include steps to reproduce
- Expected vs actual behavior
- Config (provider, model, mode)
- Relevant log output

---

## 📞 Support

- **GitHub Issues**: Bug reports and feature requests
- **Discussions**: General questions and ideas
- **Documentation**: README.md, docs/, AGENTS.md

---

*This roadmap is a living document and will be updated as priorities shift and new requirements emerge.*
