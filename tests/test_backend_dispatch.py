"""RFC-0001 Phase D refactor: per-backend handlers, per-action support, unknown outcomes."""

from __future__ import annotations

import pytest

from minecraft_ai_bridge.minecraft import actions
from minecraft_ai_bridge.minecraft.actions import (
    ActionType,
    BackendOutcomeUnknownError,
    execute_action,
    handler_table,
)
from minecraft_ai_bridge.runtime import ActionStatus, AuthzService, ControllerService
from minecraft_ai_bridge.runtime.action_outcomes import SqliteActionOutcomeStore
from minecraft_ai_bridge.runtime.action_service import ActionService, PolicyDecision
from minecraft_ai_bridge.runtime.contracts import BackendKind, Capabilities, SupportLevel
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient
from tests.test_controller_service import _request


class _ReportingAdapter(McpqWorldAdapter):
    """MCPQ adapter whose reported capabilities a test can change."""

    __slots__ = ("profile",)

    def __init__(self, client: MockMcpqClient, profile: Capabilities) -> None:
        super().__init__(client)  # type: ignore[arg-type]
        self.profile = profile

    @property
    def capabilities(self) -> Capabilities:
        return self.profile


def _mcpq_profile() -> Capabilities:
    return McpqWorldAdapter(MockMcpqClient()).capabilities  # type: ignore[arg-type]


def _unknown_chat(chats: list[str]):
    async def handler(mc, params):
        chats.append(params.get("message", ""))
        raise BackendOutcomeUnknownError("connection lost after send")

    return handler


def _same_identity(_request):
    # One identity across both services, so the restart replays the record.
    return PolicyDecision(True, identity_scope=("controller", "a"))


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    token = auth.create_credential(
        "AIBot", "owner", {"steve:control", "steve:observe:self"}, admin_token=admin.token
    ).token
    yield auth, token
    auth.close()


# ── Handler tables and the MCPQ profile ─────────────────────────────


def test_mcpq_profile_lists_every_mcpq_handler_as_supported():
    profile = _mcpq_profile()
    assert set(profile.actions) == {*handler_table(BackendKind.MCPQ.value), ActionType.STOP}
    assert set(profile.actions.values()) == {SupportLevel.SUPPORTED}


def test_a_backend_without_a_table_has_no_handlers():
    assert dict(handler_table(BackendKind.MINEFLAYER.value)) == {}


def test_handler_table_is_read_only():
    with pytest.raises(TypeError):
        handler_table(BackendKind.MCPQ.value)[ActionType.CHAT] = None  # type: ignore[index]


async def test_dispatch_uses_the_adapter_backends_table():
    world = MockMcpqClient()
    other = _mcpq_profile().model_copy(update={"backend": BackendKind.MINEFLAYER})

    result = await execute_action(
        _ReportingAdapter(world, other), ActionType.CHAT, {"message": "x"}
    )

    assert not result.success
    assert "No handler registered" in result.message
    assert world.chat_messages_sent == []


async def test_execute_action_reraises_unknown_outcomes(monkeypatch):
    chats: list[str] = []
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, _unknown_chat(chats))

    with pytest.raises(BackendOutcomeUnknownError):
        await execute_action(MockMcpqClient(), ActionType.CHAT, {"message": "x"})


# ── ActionService records unknown outcomes ───────────────────────────


async def test_unknown_outcome_is_recorded_and_never_redispatched(tmp_path, monkeypatch):
    chats: list[str] = []
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, _unknown_chat(chats))
    store = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3")
    service = ActionService(
        McpqWorldAdapter(MockMcpqClient()),  # type: ignore[arg-type]
        policy=_same_identity,
        outcome_store=store,
    )

    first = await service.execute(ActionType.CHAT, {"message": "hi"}, request_id="r1")
    await service.flush()
    replay = await service.execute(ActionType.CHAT, {"message": "hi"}, request_id="r1")

    assert not first.success
    assert first.data["reason_code"] == replay.data["reason_code"] == "OUTCOME_UNKNOWN"
    assert chats == ["hi"]
    stored = store.load("r1")
    assert stored is not None and stored.outcome.status == "unknown"
    record = service.audit_records[-1]
    assert record.allowed and not record.succeeded

    # A fresh process sharing the database must not dispatch it again either.
    restarted = ActionService(
        McpqWorldAdapter(MockMcpqClient()),  # type: ignore[arg-type]
        policy=_same_identity,
        outcome_store=store,
    )
    again = await restarted.execute(ActionType.CHAT, {"message": "hi"}, request_id="r1")
    assert again.data["reason_code"] == "OUTCOME_UNKNOWN"
    assert chats == ["hi"]
    store.close()


async def test_unknown_outcome_without_request_id(monkeypatch):
    chats: list[str] = []
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, _unknown_chat(chats))
    service = ActionService(McpqWorldAdapter(MockMcpqClient()))  # type: ignore[arg-type]

    result = await service.execute(ActionType.CHAT, {"message": "hi"})

    assert not result.success
    assert result.data["reason_code"] == "OUTCOME_UNKNOWN"


# ── ControllerService ───────────────────────────────────────────────


async def test_controller_reports_unknown_outcome_as_interrupted(setup, monkeypatch):
    auth, token = setup
    chats: list[str] = []
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, _unknown_chat(chats))
    service = ControllerService(McpqWorldAdapter(MockMcpqClient()), auth, "AIBot")  # type: ignore[arg-type]
    session = await service.open_session(token)
    request = _request(session, ActionType.CHAT, {"message": "hi"})

    first = await service.submit_action(token, request)
    replay = await service.submit_action(token, request)

    for result in (first, replay):
        assert result.status is ActionStatus.INTERRUPTED
        assert result.reason_code == "OUTCOME_UNKNOWN"
    assert chats == ["hi"]


@pytest.mark.parametrize("missing", [True, False])
async def test_unsupported_action_is_refused_before_dispatch(setup, missing):
    auth, token = setup
    world = MockMcpqClient()
    profile = _mcpq_profile()
    actions_map = dict(profile.actions)
    if missing:
        del actions_map[ActionType.CHAT]
    else:
        actions_map[ActionType.CHAT] = SupportLevel.UNSUPPORTED
    adapter = _ReportingAdapter(world, profile.model_copy(update={"actions": actions_map}))
    service = ControllerService(adapter, auth, "AIBot")
    session = await service.open_session(token)

    result = await service.submit_action(
        token, _request(session, ActionType.CHAT, {"message": "hi"})
    )

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "ACTION_NOT_SUPPORTED"
    assert world.chat_messages_sent == []
    # Refused before ActionService, so nothing was audited.
    assert not service.action_service.audit_records


async def test_physical_support_is_dispatched(setup):
    auth, token = setup
    world = MockMcpqClient()
    profile = _mcpq_profile()
    actions_map = {**profile.actions, ActionType.CHAT: SupportLevel.PHYSICAL}
    adapter = _ReportingAdapter(world, profile.model_copy(update={"actions": actions_map}))
    service = ControllerService(adapter, auth, "AIBot")
    session = await service.open_session(token)

    result = await service.submit_action(
        token, _request(session, ActionType.CHAT, {"message": "hi"})
    )

    assert result.status is ActionStatus.SUCCEEDED


async def test_capabilities_list_only_controller_actions(setup):
    auth, token = setup
    service = ControllerService(McpqWorldAdapter(MockMcpqClient()), auth, "AIBot")  # type: ignore[arg-type]

    capabilities = await service.get_capabilities(token)

    assert ActionType.CHAT in capabilities.actions
    assert ActionType.WALK_TO in capabilities.actions
    for internal in (ActionType.PROVISION_BOT, ActionType.FOLLOW_TELEPORT):
        assert internal not in capabilities.actions


async def test_capability_lookup_failure_becomes_a_failed_result():
    class Broken(_ReportingAdapter):
        @property
        def capabilities(self) -> Capabilities:
            raise RuntimeError("adapter not ready")

    world = MockMcpqClient()
    result = await execute_action(Broken(world, _mcpq_profile()), ActionType.CHAT, {"message": "x"})

    assert not result.success
    assert "adapter not ready" in result.message
    assert world.chat_messages_sent == []


async def test_a_bare_mcpq_client_still_dispatches():
    from minecraft_ai_bridge.minecraft.mc_api import McpqClient

    result = await execute_action(McpqClient(), ActionType.DONE, {})  # type: ignore[arg-type]

    assert result.success
