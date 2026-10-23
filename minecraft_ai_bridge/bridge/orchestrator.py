"""The bridge orchestrator — ties LLM, Minecraft, goals, and memory together
into a continuous think–act–observe loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import AppConfig
from ..llm.client import LLMClient, OpenCodeServerClient, create_llm_client
from ..llm.models import LLMResponse, Message, Role
from ..llm.prompts import SYSTEM_PROMPT, format_failure_hint, format_state
from ..minecraft import ActionResult, ActionType, McpqClient, Observer, WorldState
from ..runtime import (
    ActionPolicy,
    ActionService,
    AuthzControllerSession,
    AuthzService,
    EventJournal,
    McpqWorldAdapter,
    PolicyDecision,
    SafetySupervisor,
)
from .chat_commands import ChatCommandHandler
from .goal_manager import GoalManager
from .inventory_manager import InventoryManager
from .memory import AgentMemory
from .self_preservation import PreservationConfig, SelfPreservationLayer

logger = logging.getLogger(__name__)

_INTERNAL_ACTION_TYPES = frozenset(
    {
        ActionType.FOLLOW_TELEPORT,
        ActionType.PROVISION_BOT,
        ActionType.PREPARE_SAFE_SPAWN,
        ActionType.GOTO_TELEPORT,
        ActionType.COME_TELEPORT,
    }
)


@dataclass
class AgentContext:
    """Full context available to the LLM on each decision turn."""

    goal: str = ""
    current_task: str = ""
    state: str = ""
    memory: str = ""
    notable_facts: str = ""
    last_action_result: str = ""
    retry_hint: str = ""
    turn: int = 0


class Orchestrator:
    """Main agent loop.

    Usage::

        config = AppConfig.from_yaml("config.yaml")
        orch = Orchestrator(config)
        await orch.run("Build a house")
    """

    def __init__(
        self,
        config: AppConfig,
        action_policy: ActionPolicy | None = None,
    ) -> None:
        self._config = config
        self._llm: LLMClient = create_llm_client(config)
        self._mc: McpqClient | None = None
        self._world: McpqWorldAdapter | None = None
        self._action_policy = action_policy
        self._runtime_action_policy = action_policy
        self._action_service: ActionService | None = None
        # Capability object shared only with trusted in-process lifecycle and
        # chat handlers; it is never derived from action names or parameters.
        self._internal_action_authority = object()
        self._safety_action_authority = object()
        self._authz_service: AuthzService | None = None
        self._authz_session: AuthzControllerSession | None = None
        self._observer: Observer | None = None
        self._memory = AgentMemory(window=config.bridge.memory_window)
        self._goals = GoalManager(
            llm_client=self._llm,
            max_depth=config.goals.max_depth,
        )
        self._last_result: ActionResult | None = None
        self._retry_pending = False
        self._retry_action = ""
        self._retry_params: dict = {}
        self._retry_hint = ""
        self._turn = 0
        self._verbose = config.bridge.verbose
        self._max_iterations = config.bridge.max_iterations
        self._consecutive_failures = 0
        self._max_failures = 5

        # Chat command interface (N4)
        self._cmd_handler: ChatCommandHandler | None = None
        self._stop_requested = False
        self._follow_target: str | None = None

        # Inventory manager (N1)
        self._inventory: InventoryManager | None = None

        # Self-preservation layer — reflex attack + find-food injection
        # Initialised lazily in _connect() once we have an MC client.
        self._preservation: SelfPreservationLayer | None = None
        self._safety_supervisor: SafetySupervisor | None = None
        self._safety_stop_event: asyncio.Event | None = None
        self._safety_task: asyncio.Task[None] | None = None
        self._event_journal: EventJournal | None = None

    # ── Public API ───────────────────────────────────────────────────

    async def run(self, goal: str | None = None) -> None:
        """Run the full agent loop until the goal is complete or
        ``max_iterations`` is reached."""
        goal_text = goal or self._config.goals.default
        logger.info("╔══ Starting AI Agent ══╗")
        logger.info("║ Goal: %s", goal_text)
        logger.info("║ LLM:  %s / %s", self._config.llm.provider, self._config.llm.model)
        logger.info("╚════════════════════════╝")

        # 1. Connect to Minecraft via MCPQ
        api_cfg = self._config.mc_api
        logger.info(
            "Connecting to MCPQ plugin at %s:%s ...",
            api_cfg.host,
            api_cfg.port,
        )
        try:
            await self._connect()
            logger.info("MCPQ connected — controlling player: %s", api_cfg.player_name)
            self._start_safety_supervisor()

            # 2. Set up goal hierarchy
            logger.info("Decomposing goal into sub-tasks via LLM ...")
            await self._goals.set_goal(goal_text)
            logger.info("Goal decomposition complete — %d sub-goals", self._goals.sub_goal_count)

            # 3. Main loop
            done = False
            while not done and self._turn < self._max_iterations:
                self._turn += 1
                try:
                    done = await self._step()
                except Exception as exc:
                    self._consecutive_failures += 1
                    logger.exception(
                        "Fatal error on turn %d (consecutive failures: %d)",
                        self._turn,
                        self._consecutive_failures,
                    )
                    await self._chat(
                        f"Error on turn {self._turn}: {exc}. "
                        f"Consecutive failures: {self._consecutive_failures}"
                    )

                    # Backoff sleep after unexpected crashes
                    await asyncio.sleep(min(5 * self._consecutive_failures, 30))

                if self._stop_requested or self._consecutive_failures >= self._max_failures:
                    if self._stop_requested:
                        logger.info("Stop requested via in-game command.")
                        await self._chat("Shutting down as requested.")
                    else:
                        logger.error(
                            "Too many consecutive failures (%d). Shutting down.",
                            self._consecutive_failures,
                        )
                        await self._chat(
                            f"Too many errors ({self._consecutive_failures}). Shutting down."
                        )
                    break

            logger.info(
                "Agent finished after %d turns. Goal complete: %s",
                self._turn,
                self._goals.is_complete,
            )
        finally:
            await self._stop_safety_supervisor()
            await self._disconnect()

    # ── Core step ────────────────────────────────────────────────────

    async def _step(self) -> bool:
        """One iteration of the think–act–observe loop.

        Returns True if the agent should stop.
        """
        logger.info("─── Turn %d ───", self._turn)
        world_adapter = self._world
        if world_adapter is None:
            raise RuntimeError("Orchestrator must connect before starting a step")

        # ── Check for in-game commands (N4) ──────────────────────
        if self._cmd_handler:
            with contextlib.suppress(Exception):
                await self._cmd_handler.poll()
        if self._stop_requested:
            return True

        # ── Follow-mode support (N4) ─────────────────────────────
        if self._follow_target:
            try:
                my_pos = await world_adapter.get_player_pos()
                if my_pos is None:
                    pass
                else:
                    target_pos_raw = await world_adapter.run_command_blocking(
                        f"data get entity {self._follow_target} Pos"
                    )
                    if target_pos_raw:
                        import re as _re

                        m = _re.search(r"\[([^\]]+)\]", target_pos_raw)
                        if m:
                            parts = m.group(1).split(",")
                            if len(parts) >= 3:
                                tx = float(parts[0].strip().rstrip("dD"))
                                tz = float(parts[2].strip().rstrip("dD"))
                                dx = tx - my_pos[0]
                                dz = tz - my_pos[2]
                                dist = (dx * dx + dz * dz) ** 0.5
                                if dist > 4.0:
                                    if self._action_service is None:
                                        self._action_service = ActionService(
                                            world_adapter,
                                            policy=self._runtime_action_policy,
                                            internal_authority=self._internal_action_authority,
                                            safety_authority=self._safety_action_authority,
                                        )
                                    follow_result = await self._action_service.execute(
                                        ActionType.FOLLOW_TELEPORT,
                                        {"target": self._follow_target},
                                        authority=self._internal_action_authority,
                                    )
                                    if follow_result.success:
                                        logger.info(
                                            "Follow-mode teleport: %s", follow_result.message
                                        )
                                    else:
                                        logger.warning(
                                            "Follow-mode teleport failed: %s",
                                            follow_result.message,
                                        )
            except Exception:
                pass

        # ── Periodic inventory refresh (N1) ──────────────────────
        if self._inventory and self._turn % 5 == 0:
            with contextlib.suppress(Exception):
                await self._inventory.refresh()

        # ── Observe ──────────────────────────────────────────────
        world = await self._observe()

        # ── Self-preservation (reflex attack + find-food injection) ──
        # Runs after observe, before the LLM thinks.  If the layer
        # returns an ActionResult, we use it as the action for this
        # turn and skip the LLM call entirely.  The LLM still sees
        # the result via the regular last_action_result context on
        # the NEXT turn.
        reflex_result: ActionResult | None = None
        if self._safety_supervisor is not None:
            reflex_result = self._safety_supervisor.take_nowait()
        elif self._preservation is not None:
            reflex_result = await self._preservation.evaluate(world)

        retry_was_pending = self._retry_pending
        # ── Build context for LLM ─────────────────────────────────
        context = self._build_context(world)
        if self._verbose:
            self._log_context(context)

        # ── Think (LLM decides) ───────────────────────────────────
        # Build message list: goal + state + bounded memory context.
        messages: list[Message] = [
            Message(role=Role.USER, content=context.goal),
            Message(role=Role.USER, content=context.state),
        ]

        if context.notable_facts:
            messages.append(Message(role=Role.USER, content=context.notable_facts))
        messages.extend(self._memory.bounded_recent_messages())

        # Include the last action result explicitly so the LLM can see
        # what happened (especially failures).
        if context.last_action_result:
            messages.append(
                Message(
                    role=Role.USER, content=f"=== Last Action ===\n{context.last_action_result}"
                )
            )
        if context.retry_hint:
            messages.append(Message(role=Role.USER, content=context.retry_hint))

        if reflex_result is not None:
            response = LLMResponse(action="wait")
        elif self._safety_supervisor is None:
            response = await self._llm.decide(system_prompt=SYSTEM_PROMPT, messages=messages)
        else:
            response, safety_result = await self._decide_with_safety(messages)
            if safety_result is not None:
                reflex_result = safety_result

        if self._verbose:
            logger.info(
                "LLM decision: %s → %s", response.action, json.dumps(response.action_params)
            )

        completed_result: ActionResult | None = None
        if (
            reflex_result is None
            and self._safety_supervisor is not None
            and response.action == ActionType.WAIT.value
        ):
            completed_result, reflex_result = await self._wait_or_safety(response)

        # ── Act ───────────────────────────────────────────────────
        # A retry is evaluated on the following turn, but this block
        # still executes at most one action per turn.
        if reflex_result is not None:
            result = reflex_result
            response_action = result.action.value
            self._clear_retry()
        elif retry_was_pending and (
            response.action == self._retry_action and response.action_params == self._retry_params
        ):
            result = ActionResult(
                success=False,
                action=self._response_action_type(response.action),
                message=(
                    "Retry rejected: identical action and parameters would repeat a side effect."
                ),
                data={"retry_rejected": "identical_action"},
            )
            self._last_result = result
            response_action = response.action
            self._clear_retry()
        elif completed_result is not None:
            result = completed_result
            response_action = response.action
            self._clear_retry()
        else:
            result = await self._act(response)
            response_action = response.action
            self._clear_retry()
            if (
                not result.success
                and not retry_was_pending
                and response.action not in {"wait", "done"}
            ):
                self._retry_pending = True
                self._retry_action = response.action
                self._retry_params = dict(response.action_params)
                self._retry_hint = format_failure_hint(
                    response.action,
                    result.message,
                )

        # Track consecutive failures — count both action failures and exceptions
        if result.success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

        # ── Record ────────────────────────────────────────────────
        self._memory.record_action(
            response_action,
            {
                "success": result.success,
                "message": result.message,
                "data": result.data,
            },
        )

        if self._verbose:
            logger.info("Result: %s — %s", "✓" if result.success else "✗", result.message)

        # ── Check termination ─────────────────────────────────────
        if response_action == "done" and result.success:
            self._goals.mark_current_complete()
            if self._goals.is_complete:
                return True
            # Check if there's a next sub-goal
            if self._goals.current_goal:
                await self._chat(f"Starting next task: {self._goals.current_goal}")
                return False

        return self._turn >= self._max_iterations

    # ── Sub-routines ────────────────────────────────────────────────

    async def _observe(self) -> WorldState:
        """Gather world state and record it in memory."""
        assert self._observer is not None
        state = await self._observer.observe()
        self._memory.record_observation(state)
        return state

    async def _decide_with_safety(
        self, messages: list[Message]
    ) -> tuple[LLMResponse, ActionResult | None]:
        """Race one LLM decision against an independently scheduled intervention."""
        supervisor = self._safety_supervisor
        assert supervisor is not None
        pending = supervisor.take_nowait()
        if pending is not None:
            return LLMResponse(action="wait"), pending

        decision_task = asyncio.create_task(
            self._llm.decide(system_prompt=SYSTEM_PROMPT, messages=messages)
        )
        intervention_task = asyncio.create_task(supervisor.wait_for_intervention())
        try:
            done, _ = await asyncio.wait(
                {decision_task, intervention_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if intervention_task in done:
                intervention = intervention_task.result()
                decision_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await decision_task
                return LLMResponse(action="wait"), intervention

            response = decision_task.result()
            intervention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await intervention_task
            # Resolve a result published at the same instant as the LLM.
            pending = supervisor.take_nowait()
            if pending is not None:
                return LLMResponse(action="wait"), pending
            return response, None
        except BaseException:
            decision_task.cancel()
            intervention_task.cancel()
            await asyncio.gather(decision_task, intervention_task, return_exceptions=True)
            raise

    async def _wait_or_safety(
        self, response: LLMResponse
    ) -> tuple[ActionResult | None, ActionResult | None]:
        """Run a strategic WAIT unless the supervisor publishes an intervention."""
        supervisor = self._safety_supervisor
        assert supervisor is not None
        pending = supervisor.take_nowait()
        if pending is not None:
            return None, pending

        wait_task = asyncio.create_task(self._act(response))
        intervention_task = asyncio.create_task(supervisor.wait_for_intervention())
        try:
            done, _ = await asyncio.wait(
                {wait_task, intervention_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if intervention_task in done:
                intervention = intervention_task.result()
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
                return None, intervention

            result = wait_task.result()
            intervention_task.cancel()
            await asyncio.gather(intervention_task, return_exceptions=True)
            pending = supervisor.take_nowait()
            if pending is not None:
                return None, pending
            return result, None
        except BaseException:
            wait_task.cancel()
            intervention_task.cancel()
            await asyncio.gather(wait_task, intervention_task, return_exceptions=True)
            raise

    def _start_safety_supervisor(self) -> None:
        """Start periodic preservation checks after runtime wiring is ready."""
        if self._preservation is None or self._safety_task is not None:
            return
        if self._action_service is not None:
            self._action_service.enable_safety_preemption()
        self._safety_supervisor = SafetySupervisor(
            self._observe,
            self._preservation.evaluate,
            on_intervention_consumed=(
                self._action_service.acknowledge_safety_intervention
                if self._action_service is not None
                else None
            ),
            event_sink=(self._event_journal.append if self._event_journal is not None else None),
        )
        self._safety_stop_event = asyncio.Event()
        self._safety_task = asyncio.create_task(
            self._safety_supervisor.run(self._safety_stop_event),
            name="minecraft-ai-bridge-safety-supervisor",
        )

    async def _stop_safety_supervisor(self) -> None:
        """Stop the polling task before disconnecting the world adapter."""
        if self._safety_stop_event is not None:
            self._safety_stop_event.set()
        if self._safety_task is not None:
            await self._safety_task
        if self._safety_supervisor is not None:
            await self._safety_supervisor.close()
        if self._action_service is not None:
            self._action_service.disable_safety_preemption()
        if self._event_journal is not None:
            self._event_journal.close()
            self._event_journal = None
        self._safety_task = None
        self._safety_stop_event = None
        self._safety_supervisor = None

    def _build_context(self, world: WorldState) -> AgentContext:
        """Build structured context for the LLM prompt."""
        state_dict = world.__dict__ if hasattr(world, "__dict__") else {}
        state_str = format_state(state_dict)

        # Goal context
        goal_context = (
            f"=== Goal ===\n{self._goals.progress}\n\nCurrent task: {self._goals.current_goal}"
        )

        # Memory context is bounded before it reaches logs or the LLM.
        recent_messages = self._memory.bounded_recent_messages()
        memory_str = "\n".join(message.content for message in recent_messages)
        if not memory_str.strip():
            memory_str = "(no recent actions)"

        facts = self._memory.bounded_notable_facts()

        # Last action result
        last_result = ""
        if self._last_result:
            last_result = (
                f"Last action: {self._last_result.action.value}\n"
                f"Success: {self._last_result.success}\n"
                f"Message: {self._last_result.message}"
            )

        return AgentContext(
            goal=goal_context,
            current_task=self._goals.current_goal,
            state=f"=== World State ===\n{state_str}",
            memory=f"=== Recent Actions ===\n{memory_str}",
            notable_facts=facts,
            last_action_result=last_result,
            retry_hint=self._retry_hint,
            turn=self._turn,
        )

    def _clear_retry(self) -> None:
        """Clear the single pending retry opportunity."""
        self._retry_pending = False
        self._retry_action = ""
        self._retry_params = {}
        self._retry_hint = ""

    @staticmethod
    def _response_action_type(action: str) -> ActionType:
        """Map a response action to an enum for synthetic results."""
        try:
            return ActionType(action)
        except ValueError:
            return ActionType.WAIT

    async def _act(self, response: LLMResponse) -> ActionResult:
        """Execute the LLM's chosen action through the runtime action gate."""
        assert self._mc is not None
        assert self._world is not None

        try:
            action_type = ActionType(response.action)
        except ValueError:
            logger.warning("Unknown action: %s", response.action)
            return ActionResult(
                success=False,
                action=ActionType.WAIT,
                message=f"Unknown action: {response.action}",
            )

        if action_type in _INTERNAL_ACTION_TYPES:
            # Internal-only actions (follow teleport, startup provisioning,
            # and the !goto/!come chat-command teleports) must never be
            # reachable from an LLM response, even though they exist in the
            # ActionType enum. Reject them at the dispatch boundary so
            # policy/audit cannot be reached with the identity of a trusted
            # orchestrator-issued action.
            logger.warning("LLM attempted internal action: %s", response.action)
            return ActionResult(
                success=False,
                action=ActionType.WAIT,
                message=f"Internal action not available: {response.action}",
            )

        if self._action_service is None:
            self._action_service = ActionService(
                self._world,
                policy=self._runtime_action_policy,
                internal_authority=self._internal_action_authority,
                safety_authority=self._safety_action_authority,
            )

        result = await self._action_service.execute(
            action_type,
            response.action_params,
            preemptible=True,
        )

        self._last_result = result

        # Record important discoveries
        if action_type == ActionType.CHECK_POSITION and result.success:
            pos = result.data.get("position_raw", "")
            if pos:
                self._memory.remember_fact(f"Position data: {pos}")

        # Rate-limit delay
        await asyncio.sleep(self._config.bridge.cycle_delay)

        return result

    async def _chat(self, message: str) -> None:
        """Send a chat message as the agent."""
        if self._world and self._world.connected:
            with contextlib.suppress(Exception):
                await self._world.post_to_chat(message)

    # ── Connection management ────────────────────────────────────────

    async def _connect(self) -> None:
        """Connect to the Minecraft server via the MCPQ plugin."""
        self._config.authz.validate_controller_credentials()
        api_cfg = self._config.mc_api
        self._mc = McpqClient(
            host=api_cfg.host,
            port=api_cfg.port,
            player_name=api_cfg.player_name,
        )
        await self._mc.connect()
        # Exactly one adapter wraps the connected client; raw MCPQ access
        # stays confined to connection lifecycle (connect/disconnect).
        self._world = McpqWorldAdapter(self._mc)
        policy = self._action_policy
        if self._config.authz.enabled:
            authz_config = self._config.authz
            self._authz_service = AuthzService(authz_config.database_path)
            token = authz_config.controller_token.get_secret_value()
            credential = self._authz_service.authenticate(token)
            self._authz_session = self._authz_service.acquire_session(
                token,
                authz_config.steve_id,
                owner_id=credential.owner_id,
                lease_expires_at=datetime.now(UTC)
                + timedelta(seconds=authz_config.session_lease_seconds),
            )
            authz_policy = self._authz_service.action_policy(self._authz_session)
            policy = self._combine_action_policies(authz_policy, self._action_policy)
        self._runtime_action_policy = policy
        self._action_service = ActionService(
            self._world,
            policy=policy,
            internal_authority=self._internal_action_authority,
            safety_authority=self._safety_action_authority,
        )
        self._observer = Observer(self._world)
        self._event_journal = EventJournal(self._config.bridge.event_journal_path)
        logger.info("Connected to MCPQ — player name: %s", api_cfg.player_name)

        # ── Ensure a fake player entity exists ────────────────────────
        # The MCPQ-Bot Paper plugin (/botsummon) creates a ServerPlayer
        # that MCPQ can detect and control. Without this, player-relative
        # selectors like @p and MCPQ's getPlayers won't work.
        player_name = api_cfg.player_name
        logger.info("Checking if player '%s' exists …", player_name)

        try:
            # Try getting the player's position — if it works, they exist
            pos = await self._world.get_player_pos()
            if pos is not None:
                logger.info("Player '%s' already present at %s", player_name, pos)
            else:
                raise ValueError("pos is None")
        except Exception:
            # Player doesn't exist yet — spawn via /botsummon
            logger.info("Player '%s' not found — summoning bot …", player_name)

            provision_result = await self._provision_bot(player_name)
            logger.warning(
                "Bot provisioning: %s",
                provision_result.message,
            )
            if not provision_result.success:
                logger.warning(
                    "Continuing anyway — some MCPQ ops will fail without a player entity.",
                )
            # Poll until the player entity is confirmed present
            logger.info("Waiting for player '%s' to be registered …", player_name)
            for _poll_attempt in range(10):
                await asyncio.sleep(1)
                try:
                    pos = await self._world.get_player_pos()
                    if pos is not None:
                        logger.info("Player '%s' confirmed at %s", player_name, pos)
                        break
                except Exception:
                    pass
            else:
                logger.warning(
                    "Player '%s' not confirmed after polling. Continuing — some MCPQ ops may fail.",
                    player_name,
                )

        # ── Teleport to a safe location ─────────────────────────────
        # The fake player often spawns underwater or in unsafe terrain.
        # We teleport to a high Y (above any terrain) and build a solid
        # platform underneath so the bot stands on dry land.

        # Routed through the action service as the internal
        # `prepare_safe_spawn` action so policy and audit can distinguish
        # trusted startup provisioning from ordinary gameplay mutations.
        await self._prepare_safe_spawn()
        # Initialise sub-systems (N1 inventory, N4 chat commands)
        self._inventory = InventoryManager(self._world)
        try:
            await self._inventory.refresh()
            logger.info("Initial inventory: %s", self._inventory.summary)
        except Exception:
            logger.debug("Initial inventory refresh failed (expected if player just spawned)")

        self._cmd_handler = ChatCommandHandler(self)

        # Self-preservation layer (reflex attack + find-food injection).
        # The shared action service is passed so the layer's mutating
        # reflexes (ATTACK, EAT, HEAL) use the same policy/audit boundary
        # as LLM-selected actions; its observation checks stay direct.
        self._preservation = SelfPreservationLayer(
            mc=self._world,
            goal_manager=self._goals,
            memory=self._memory,
            config=PreservationConfig(),
            action_service=self._action_service,
            safety_authority=self._safety_action_authority,
        )

        # Persist the goal in memory database (N3)
        root_desc = self._goals.root_description
        self._memory.save_goal(root_desc if root_desc else "Unknown goal")

        # Greet
        await self._chat("AI Agent online and ready!")

    async def _disconnect(self) -> None:
        """Clean up the MCPQ connection, memory database, and other resources."""
        try:
            if self._authz_service is not None:
                try:
                    if self._authz_session is not None:
                        with contextlib.suppress(PermissionError):
                            self._authz_service.release_session(self._authz_session)
                finally:
                    try:
                        self._authz_service.close()
                    finally:
                        self._authz_service = None
                        self._authz_session = None
        finally:
            try:
                if self._mc:
                    try:
                        await self._chat("AI Agent signing off.")
                    finally:
                        await self._mc.disconnect()
            finally:
                try:
                    if self._event_journal is not None:
                        self._event_journal.close()
                        self._event_journal = None
                finally:
                    try:
                        # Persist memory database (N3).
                        if hasattr(self._memory, "close"):
                            with contextlib.suppress(Exception):
                                self._memory.close()
                    finally:
                        # Clean up OpenCodeServerClient HTTP session if applicable.
                        if isinstance(self._llm, OpenCodeServerClient):
                            with contextlib.suppress(Exception):
                                await self._llm.close()

    @staticmethod
    def _combine_action_policies(
        authz_policy: ActionPolicy,
        additional_policy: ActionPolicy | None,
    ) -> ActionPolicy:
        """Require local session authorization and any caller-supplied policy."""

        async def combined(request):
            authorization_decision = None
            for index, policy in enumerate((authz_policy, additional_policy)):
                if policy is None:
                    continue
                decision = policy(request)
                if inspect.isawaitable(decision):
                    decision = await decision
                if not decision.allowed:
                    return decision
                if index == 0:
                    authorization_decision = decision
                if index == 1:
                    # A caller policy may await while the session is revoked,
                    # expires, or is fenced by ownership transfer. Revalidate
                    # at the dispatch boundary after that await completes.
                    final_decision = authz_policy(request)
                    if inspect.isawaitable(final_decision):
                        final_decision = await final_decision
                    if not final_decision.allowed:
                        return final_decision
                    authorization_decision = final_decision
            return PolicyDecision(
                allowed=True,
                allow_privileged=bool(
                    authorization_decision and authorization_decision.allow_privileged
                ),
                identity_scope=(
                    authorization_decision.identity_scope if authorization_decision else None
                ),
            )

        return combined

    # ── Logging ──────────────────────────────────────────────────────

    async def _provision_bot(self, player_name: str) -> ActionResult:
        """Summon the bot entity through the action gate (startup-only).

        Uses the internal `provision_bot` action so the trusted startup
        provisioning path is visible to policy and audit. The player name
        is validated as a strict Minecraft username before command
        interpolation. Returns the service's ActionResult unchanged: a
        failed result is logged by the caller and startup continues,
        preserving the previous degraded-start behavior.
        """
        assert self._mc is not None
        assert self._world is not None
        assert self._action_service is not None

        try:
            return await self._action_service.execute(
                ActionType.PROVISION_BOT,
                {"player_name": player_name},
                authority=self._internal_action_authority,
            )
        except Exception as provision_err:
            logger.warning(
                "Failed to summon bot via /botsummon: %s  "
                "Continuing anyway — some MCPQ ops will fail "
                "without a player entity.",
                provision_err,
            )
            return ActionResult(
                success=False,
                action=ActionType.PROVISION_BOT,
                message=f"Failed to summon bot via /botsummon: {provision_err}",
            )

    async def _prepare_safe_spawn(self) -> None:
        """Teleport to the startup spawn point and place a 3x3 dirt platform."""
        assert self._mc is not None
        assert self._world is not None
        assert self._action_service is not None
        player_name = self._config.mc_api.player_name
        safe_x, safe_y, safe_z = 0, 65, 0
        logger.info(
            "Teleporting player '%s' to (%d, %d, %d) …",
            player_name,
            safe_x,
            safe_y,
            safe_z,
        )
        for attempt in range(3):
            try:
                result = await self._action_service.execute(
                    ActionType.PREPARE_SAFE_SPAWN,
                    {"x": safe_x, "y": safe_y, "z": safe_z},
                    authority=self._internal_action_authority,
                )
                if not result.success:
                    logger.warning(
                        "Safe teleport attempt %d: %s",
                        attempt + 1,
                        result.message,
                    )
                    await asyncio.sleep(2)
                    continue
                await asyncio.sleep(2)
                safe_pos = await self._world.get_player_pos()
                if safe_pos:
                    logger.info(
                        "Player at (%.1f, %.1f, %.1f) — feet: %s",
                        *safe_pos,
                        await self._world.get_block(
                            int(safe_pos[0]),
                            int(safe_pos[1]) - 1,
                            int(safe_pos[2]),
                        ),
                    )
                    return
                logger.warning("Teleport attempt %d: position unknown", attempt + 1)
            except Exception as safe_err:
                logger.warning(
                    "Safe teleport attempt %d failed: %s",
                    attempt + 1,
                    safe_err,
                )
                await asyncio.sleep(2)

    def _log_context(self, ctx: AgentContext) -> None:
        """Pretty-print context in verbose mode."""
        border = "─" * 60
        logger.debug(
            "\n%s\n[TURN %d]\n%s\n%s\n%s\n%s\n%s\n%s",
            border,
            ctx.turn,
            ctx.goal,
            ctx.state,
            ctx.memory,
            ctx.notable_facts,
            ctx.last_action_result,
            border,
        )
