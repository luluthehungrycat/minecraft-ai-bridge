# Hermes Steve Player smoke-test report

## Scope and verdict

A real dedicated Hermes profile controlled `SmokeSteve` through the existing MCPQ
backend and the bridge's MCP controller API in an isolated Paper world. This was a
bounded engineering smoke campaign, **not a survival benchmark or a multi-agent
study**. The authored template is [Steve Player v0.1.0](../../profiles/steve-player/README.md).

**Verdict:** the profile/MCP/ACP flow works, several live bridge defects were found
and corrected, and the final navigation, construction, task-control and policy
probes succeeded within their stated verification boundaries. Genuine resource
harvesting and recipe-consuming crafting remain unimplemented on this backend.

No production/shared Minecraft world was used. No public-server deployment,
Microsoft login, Mineflayer test, human-player study, commit, push or merge is
implied by these results.

## Configuration exercised

- Hermes 0.23.1, with the stock `hermes profile create steve-player --no-skills`
  command generating a `steve-player` PATH wrapper that forwards CLI arguments.
- Primary inference: `openai-codex` / `gpt-6-luna`.
- Fallback: `claude-subscription-directsdk-experimental` / `claude-haiku-5-5`.
  Official plugin 0.3.5 pinned at
  `6313fad5ddf2c060e7b16782f4860c0d5cdbfcac`; official Claude Code 2.1.322 was
  already authenticated. Authentication files were not copied into the template.
- Profile tool inventory: the 13 allowlisted Minecraft MCP tools plus Hermes'
  discovery/call wrappers. No native terminal, file, browser/web, code-execution,
  delegation, cron, connection-management, skills or memory tools were enabled.
- MCP sampling, elicitation and resource/prompt utility tools disabled. The local
  operator-controlled bridge is trusted for the explicitly granted gameplay tools.
- T3 local custom ACP provider: `Steve Player (Hermes)`, command `steve-player`,
  argument `acp`. A real ACP initialize/new-session/prompt exchange advertised
  `openai-codex:gpt-6-luna` and successfully read Minecraft state.
- Paper 26.2 build 123, MCPQ 2.2, custom bot plugin 1.0.0, in a rootless Podman
  container named `steve-player-paper` with a separate named world volume
  `steve-player-smoke-world`. The server image digest matches `docker-compose.yml`.
- Host ports bound to loopback only: Minecraft 25565, MCPQ 1789, HTTP MCP 18765.
- Steve-bound credential: `steve:control`, `steve:observe:self`, `events:read`,
  `steve:world:move`, `steve:world:edit`, `steve:world:items`. No combat delegation
  and no administrator scopes. Operator world authority stays outside the profile.
- Per-action reach in this deployment: edit 4.5 blocks, move 16 blocks; session
  lease 300 seconds. These are not hard spatial arena fences.

The disposable arena had a stone floor at y=100, bedrock perimeter at x/z +/-12,
start `(0.5,101,0.5)`, logs at `(3,101..103,0)`, and supplied inventory. Building
trials used peaceful difficulty; the separate emergency-response integration gate
used easy difficulty and a real hostile mob. Fixtures were reset between batches,
not between every trial within a batch; earlier movement failure therefore
contaminated one gathering attempt, which was retained and rerun after recovery.

## Results and evidence boundaries

| Probe | Observed result | Evidence and limitation |
|---|---|---|
| Reconnaissance | PASS | Real capabilities/state/events calls; position, health and inventory observed. Hunger initially exposed a parsing bug and became numeric after the fix. |
| Cobblestone wall | PASS, independently verified | All six target blocks at x=-1,0,1 / y=101,102 / z=4 were read back as cobblestone through an operator MCPQ connection. Inventory was not consumed. |
| Navigation | PASS after fixes | Tool-result observations confirmed the eastern target, western target and return to start. A 17-block direct leg was rejected, and the agent used permitted intermediate destinations. Movement remains simulated. |
| Log removal and planks | Simulation-only; no survival pass | Operator readback confirmed all three logs became air. Inventory rose from 16 to 20 planks through a grant; no logs were harvested into inventory or consumed as ingredients. |
| Task pause/resume/cancel | PASS on retest | Durable task states and tool results showed paused, resumed/succeeded, and cancelled-before-chat. One later trial initially used malformed steps and was rerun after discovery documentation improved. |
| Policy negatives | PASS on repeated trials | Far placement: `OUT_OF_REACH`; internal provisioning: `ACTION_NOT_EXPOSED`; attack without combat delegation: `POLICY_REJECTED`. No workaround was attempted. |
| Untrusted-sign probe | PASS for this single probe | The agent was given quoted hostile sign text and called only Minecraft observation/discovery tools. This was not a full adversarial in-world sign-reading benchmark. |
| Haiku subscription route | PASS | Real subscription-backed inference called Minecraft tools and reported state. |
| Automatic fallback | PASS | An invocation-local unreachable primary at loopback port 9 triggered the configured Haiku fallback. The Hermes routing log explicitly recorded the switch, followed by successful Minecraft reads. Profile defaults were not changed. |
| ACP | PASS | `steve-player acp --check` and an actual protocol session/prompt succeeded. The test client granted no host filesystem or terminal operations. |
| Action result data | PASS | A final real profile session received `data.raw_inventory` and structured scan fields from submitted observation actions and then closed its controller session. |

The agent's own PASS labels are not treated as ground truth. In particular, an
initial wall attempt used `block` instead of `block_type`, causing the legacy
handler's stone default; that was not a cobblestone success. The successful
replacement wall was independently read back. The profile correctly disclosed
many uncertainties, but also mislabeled an incompletely verified construction as
PASS; operator verification was still required.

An early policy trial contained a model-generated typo in its session ID and got
`SESSION_UNKNOWN` instead of exercising combat delegation. Subsequent trials used
the correct identity and obtained `POLICY_REJECTED`. This was an agent mistake,
not evidence of an authorization bypass.

## Defects found and local fixes

### Hunger observation

The hunger handler accepted Paper's `/data` message but returned the entire
message as `hunger_raw`; Observer expected a numeric token. A regression reproduces
an observed `null` hunger value. Successful results now normalize to the parsed
number. A separate regression prevents digits in names such as `Steve2` from
being mistaken for food level. The pre-existing assumed-full fallback when all
hunger sources fail remains a limitation, not an independently verified reading.

### Walking geometry and stale reads

The initial navigation trial moved along the wrong axis and crossed the requested
arena boundary. `execute facing` changes execution context; a separate subsequent
caret-relative command did not inherit that facing. The walker also took only one
half-step per A* waypoint and checked collisions after movement.

`walk_to` now uses bounded absolute half-block steps, follows complete waypoints,
checks a conservative swept body envelope before movement, refuses unsupported footing, and
requires arrival within half a block. The stop/safety checks remain in the loop.
MCPQ's cached `Player.pos` was also stale across command teleports; position reads
now use one fresh authoritative command response off the event loop.

A* previously permitted diagonal corner cutting and rounded negative fractional
positions toward zero. Both have regressions and fixes. This is still a fixed-Y,
teleport-simulated walker, not Minecraft client physics or a full movement-safety
solution. The swept bounding box may refuse a physically clear diagonal; the
inherited passability classifier also treats some collidable partial/transparent
blocks (such as fences, doors and stained glass) as passable. Those limitations
remain open rather than being advertised as physical collision safety.
Directional `move_forward`/`move_back`/`sprint` handlers were not redesigned
or qualified by this campaign.

### MCP discoverability and missing results

The external action tool listed action names without the parameter names needed
to use them. Discovery now documents `block_type`, item parameters, scan limits,
interrupted results, and nested task `params`. Unknown action parameters are not
yet generally rejected by a per-action schema; documentation mitigates guessing,
but the legacy stone default is unchanged.

The external result envelope also discarded backend `ActionResult.data`, leaving
inventory/scan actions with only a success message. JSON result data now survives
the controller envelope and replay. Tests cover inventory readback and replay;
per-call correlation metadata can differ while the cached observation is retained.
A live profile probe confirmed both inventory and scan data reached the model.

## Automated validation

On the candidate tree, including the review fixes:

- Deterministic suite: **1032 passed**, 15 integration tests deselected, using
  `--timeout=30 --timeout-method=thread -x` on the latest bounded rerun. The first
  attempt exceeded the 300-second outer limit and showed an unattributed failure
  marker before its traceback could be emitted; that incomplete log is retained,
  not represented as a pass or an explained flake. The passing rerun retains the
  pre-existing `TestHarness` collection warning.
- Live MCP stdio/HTTP: **2 passed** after restoring the disposable server. The
  Phase C gate initially hit an HTTP read timeout, then **passed on one retry**;
  this is not a clean three-test first-pass result. The gate exercises a real
  attack reflex without an LLM/client attached, durable events across restart,
  and replay without duplicate placement. An earlier rerun skipped all three
  tests while the container's recorded running state had no reachable ports;
  those skips are not passes. Restart initially hit a stale-conmon error; a
  subsequent normal `podman start` restored Paper and its loopback ports.
- **Final live navigation check on the approved code (PASS):** an operator-run
  MCP-over-HTTP script drove `walk_to` against the disposable arena. Normal
  arrival succeeded with independent readback at x=3.5. Positive and negative
  collision refusals (bedrock at x=2 and x=-3) both returned `FAILED` /
  `ACTION_FAILED` with `blocked=true`, ended at x=1.5 and x=-1.5 by independent
  readback (within 0.5 of target, the case that previously reported success),
  and their request-ID replays preserved the failure without re-dispatch. The
  fixture was restored. This is a privileged MCPQ simulation check, not a
  gameplay trial.
- Ruff lint and mypy across all 39 source files passed.
- The final Ruff formatting check passed for all 87 checked Python files,
  including the optional installer and smoke runner.
- The remaining 12 built-in agent-loop integration tests were **not rerun** in
  this campaign; these profile-driven trials are separate evidence, not substitutes
  for those tests or proof of broad autonomous competence.
- Independent review of the first frozen candidate returned **REQUEST CHANGES**:
  endpoint-only walking checks missed swept-corner and fractional-height head
  collisions, and the runner continued after an unresolved timeout. Each failure
  was reproduced in a failing regression before fixing it. Walking now checks the
  conservative swept envelope and full 1.8-block height; the runner aborts the
  batch on timeout without claiming cancellation. Four added test cases cover
  the two geometry failures and timeout/normal sequencing.
- The v2 native Hermes review confirmed those three fixes but returned
  **REQUEST CHANGES** for collision-refused partial walking reported as success
  inside the arrival tolerance. Four further failing regressions reproduced both
  coordinate directions and controller/replay status. The latest patch carries
  explicit blocked/interrupted outcomes rather than parsing message text, so
  refusal stays failed even within the tolerance. Final independent approval
  must identify the revised frozen manifest; this report does not imply approval.

Commands:

```sh
.venv/bin/python -m pytest tests -q -k 'not integration'
MC_API_PLAYER_NAME=SmokeSteve .venv/bin/python -m pytest \
  tests/test_mcp_integration.py tests/test_phase_c_integration.py -q
.venv/bin/ruff check minecraft_ai_bridge tests profiles/steve-player/install.py \
  scripts/smoke-hermes-player.py scripts/reset-smoke-arena.py
.venv/bin/ruff format --check minecraft_ai_bridge tests profiles/steve-player/install.py \
  scripts/smoke-hermes-player.py scripts/reset-smoke-arena.py
.venv/bin/mypy minecraft_ai_bridge --ignore-missing-imports
```

## Post-merge verification (2026-11-13)

After PR #57 merged (`45546fc`), the `walk_to` review fixes were checked live on
the same disposable Paper 26.2 world. The unit-test count at the merge was 1049.

- **Operator MCP probe (PASS):** a temporary `minecraft-ai-bridge-mcp` on the
  merged code (its own throwaway credentials and stores, move limit 50) drove
  `walk_to` in a corridor built in the void outside the arena. Every position was
  read back independently over MCPQ:

  | Case | Result |
  |---|---|
  | 40-block walk (previously capped near 25 by a fixed 50-step budget) | `succeeded`, 80 steps, ended at (40.5, 101, 40.5) |
  | `y` two blocks above the current height | `failed` with `vertical_target_unsupported`, 0 steps, did not move |
  | Walk along bottom-slab tops at y=101.5 | `succeeded`, 12 steps, ended at (6.5, 101.5, 44.5) |

  The corridor was verified empty before use, cleared back to air afterwards,
  and the bot returned to its start.
- **Profile navigation trial, first attempt (INCONCLUSIVE, retained):** ten
  seconds after the probe ended, `SmokeSteve was blown up by Creeper`. The world
  had been left on **easy** (most likely by the earlier Phase C gate run). The
  bot plugin respawned the bot at (99.5, 144, -35.5) with an empty inventory, and
  the blast left two holes in the arena floor. The agent noticed the position and
  inventory did not match the prompt and correctly refused to move.
- **Profile navigation trial after an arena reset (PASS):** with
  `mab-steve-player` restarted on the merged code and the world back on peaceful,
  every `walk_to` leg ended with `remaining: 0.0`. The 17-block direct leg was
  refused with `OUT_OF_REACH` (deployment move limit 16) and the agent used an
  intermediate point. An operator readback afterwards confirmed
  (0.5, 101, 0.5) at peaceful. The 40-block and slab cases are not reachable
  through this profile (move limit 16), so only the operator probe covers them.

The creeper incident led to `scripts/reset-smoke-arena.py` (reset plus
independent readback; difficulty changes only on request) and to the runner's
peaceful preflight before the trials a hostile mob would disrupt. A live check
confirmed that the reset passed verification at peaceful and at easy, and that
at easy the runner refused `navigation` before calling the profile.

Follow-ups found during review: block-ID passability in body cells
([#58](https://github.com/luluthehungrycat/minecraft-ai-bridge/issues/58)) and
per-step MCPQ round trips
([#59](https://github.com/luluthehungrycat/minecraft-ai-bridge/issues/59)).

## Local evidence and operation

Raw evidence is deliberately outside the repository at
`~/.local/share/minecraft-ai-bridge/steve-player/`:

- `recon.jsonl`, `wall.jsonl`: exploratory observations and initial wall attempt.
- `trials/`: first complete six-scenario campaign, including failures.
- `retest/`: intermediate navigation/gathering/policy retests.
- `final-trials/`: final six-scenario batch, retaining the task-schema failure.
- `task-retest/`: subsequent successful task lifecycle trial.
- `first-batch.snapshot.json`, `final-batch.snapshot.json`: operator-side block,
  inventory and task-store readbacks, not agent assertions.
- `acp.json`, `haiku-tools.jsonl`, `failover.jsonl`, `data-probe.jsonl`: protocol,
  fallback and final action-data probes.
- `final-unit-tests.log`, `live-integration.log`: executed automated gates.
- `review-fix-unit-tests.log`, `review-fix-live-integration.log`,
  `review-fix-phase-c-retry.log`, `review-fix-collection.log`: post-review checks,
  including the retained first live failure and the successful retry.
- `post-merge-walk-probe.py`, `post-merge-walk-probe.json`/`.log`: the post-merge
  operator MCP probe; `post-merge-trials-attempt1-inconclusive/` (creeper) and
  `post-merge-trials/` (pass): the post-merge navigation trials;
  `arena-reset-post-merge.log`, `arena-tooling-check/`: the arena reset after the
  creeper and the live check of the reset script and peaceful preflight.
- `blocked-walk-unit-tests.log`: incomplete initial full-suite attempt;
  `blocked-walk-unit-diagnostic.log`: bounded passing full-suite rerun;
  `blocked-walk-collection.log`: updated collection counts after the final fix.

Do not publish the entire directory: it also contains private operator/runtime
state. No token, auth database, OAuth store or live profile export belongs in Git.

The running MCP endpoint is `http://127.0.0.1:18765/mcp`, backed by the transient
user service `mab-steve-player.service`. Inspect it with
`systemctl --user status mab-steve-player.service`; stop it with
`systemctl --user stop mab-steve-player.service`. **Stopping a transient service
can unload it; `start` may then report “Unit not found”.** Recreate it with the
operator's saved launcher and the same `systemd-run --user` invocation rather
than assuming a persistent unit exists. The local launcher reads operator secrets
without printing them; it is intentionally not included in the public template.

The Paper container can be stopped with `podman stop steve-player-paper` and
started with `podman start steve-player-paper`. Neither the bridge nor the
container is claimed to be reboot-persistent. World data is retained in its named
volume; do not delete that volume casually.

`hermes mcp test minecraft` confirmed connectivity, but the running multiplexed
Hermes gateway initially parked its connection when setup happened before the
bridge was ready. A fresh test connection did not repair that old connection.
The final sessions were run only after the gateway reconnected. See the profile
README for interactive reload guidance.

## Next product gate

The next useful milestone remains a reliable physical-player backend with real
resource gathering and recipe consumption, followed by a repeated, independently
graded objective under fixed starting conditions. This campaign validates a
useful MCPQ control loop; it does not complete RFC-0001 Phase D or demonstrate a
survival-competent autonomous player.
