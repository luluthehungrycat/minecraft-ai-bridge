"""Per-credential world delegation for ControllerService (#24)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    SCOPES,
    ActionRequest,
    ActionRequestSchema,
    ActionStatus,
    AuthzService,
    ControllerService,
)
from minecraft_ai_bridge.runtime.action_service import (
    WORLD_DELEGATION_SCOPES,
    ActionClass,
    classify_action,
)
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient

WORLD_SCOPES = {"steve:world:move", "steve:world:edit", "steve:world:items", "steve:world:combat"}

# One representative privileged action per delegation scope.
SAMPLES = {
    "steve:world:move": (ActionType.TELEPORT, {"x": 5, "y": 64, "z": 5}),
    "steve:world:edit": (ActionType.BREAK_BLOCK, {"x": 1, "y": 64, "z": 1}),
    "steve:world:items": (ActionType.CRAFT_ITEM, {"item_type": "stick", "amount": 1}),
    "steve:world:combat": (ActionType.HEAL, {}),
}


@dataclass
class World:
    auth: AuthzService
    mc: MockMcpqClient
    admin: str
    world_admin: str

    def credential(self, *scopes: str) -> str:
        return self.auth.create_credential(
            "AIBot", "owner", {"steve:control", *scopes}, admin_token=self.admin
        ).token

    def service(self, *, world_admin: bool = True) -> ControllerService:
        return ControllerService(
            McpqWorldAdapter(self.mc),
            self.auth,
            "AIBot",
            world_admin_token=self.world_admin if world_admin else None,
        )


@pytest.fixture
def world(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    mc = MockMcpqClient()
    mc.set_position(0, 64, 0)
    yield World(
        auth=auth,
        mc=mc,
        admin=admin.token,
        world_admin=auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token,
    )
    auth.close()


def _request(session, action: ActionType, params: dict, request_id: str = "req-1"):
    return ActionRequestSchema(
        steve_id=session.steve_id,
        controller_session_id=session.session_id,
        generation=session.generation,
        request_id=request_id,
        action=action,
        params=params,
    )


async def _submit(service: ControllerService, token: str, action: ActionType, params: dict):
    session = await service.open_session(token)
    try:
        # A fresh request ID per call, so idempotency never masks the policy.
        return await service.submit_action(
            token, _request(session, action, params, request_id=f"req-{uuid4()}")
        )
    finally:
        await service.close_session(token, session.session_id)


def test_every_privileged_action_has_exactly_one_delegation_scope():
    privileged = {a for a in ActionType if classify_action(a) is ActionClass.PRIVILEGED_SIMULATED}
    assert set(WORLD_DELEGATION_SCOPES) == privileged
    assert set(WORLD_DELEGATION_SCOPES.values()) == WORLD_SCOPES
    assert WORLD_SCOPES <= SCOPES
    assert set(SAMPLES) == WORLD_SCOPES
    assert all(WORLD_DELEGATION_SCOPES[action] == scope for scope, (action, _) in SAMPLES.items())


@pytest.mark.parametrize("scope", sorted(WORLD_SCOPES))
async def test_operator_token_alone_does_not_delegate(world, scope):
    action, params = SAMPLES[scope]
    result = await _submit(world.service(), world.credential(), action, params)

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "POLICY_REJECTED"
    assert scope in result.message
    assert world.mc.commands_ran == []


@pytest.mark.parametrize("scope", sorted(WORLD_SCOPES))
async def test_delegation_alone_does_not_grant_without_operator_token(world, scope):
    action, params = SAMPLES[scope]
    result = await _submit(
        world.service(world_admin=False), world.credential(*WORLD_SCOPES), action, params
    )

    assert result.status is ActionStatus.REJECTED
    assert world.mc.commands_ran == []


@pytest.mark.parametrize("scope", sorted(WORLD_SCOPES))
async def test_token_and_matching_delegation_allow_the_action(world, scope):
    action, params = SAMPLES[scope]
    result = await _submit(world.service(), world.credential(scope), action, params)

    # HEAL may fail in the mock for game reasons, but it must pass policy.
    assert result.reason_code != "POLICY_REJECTED", result.message


async def test_delegation_is_limited_to_its_own_group(world):
    service = world.service()
    mover = world.credential("steve:world:move")

    allowed = await _submit(service, mover, *SAMPLES["steve:world:move"])
    missing = ("steve:world:edit", "steve:world:items", "steve:world:combat")
    denied = [await _submit(service, mover, *SAMPLES[scope]) for scope in missing]

    assert allowed.status is ActionStatus.SUCCEEDED
    assert [r.reason_code for r in denied] == ["POLICY_REJECTED"] * 3
    assert all(scope in r.message for scope, r in zip(missing, denied, strict=True))


@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.CHAT, {"message": "hi"}),
        (ActionType.WAIT, {"seconds": 0}),
        (ActionType.CHECK_POSITION, {}),
    ],
)
async def test_observation_and_chat_need_no_delegation(world, action, params):
    result = await _submit(world.service(), world.credential(), action, params)
    assert result.reason_code != "POLICY_REJECTED", result.message


def test_privileged_actions_without_a_scope_are_denied_when_delegation_is_required(world):
    token = world.credential(*WORLD_SCOPES)
    credential = world.auth.authenticate(token)
    session = world.auth.acquire_session(
        token,
        "AIBot",
        owner_id=credential.owner_id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    policy = world.auth.action_policy(
        session, admin_token=world.world_admin, blocking=True, require_delegation=True
    )

    for action in (ActionType.GOTO_TELEPORT, ActionType.PROVISION_BOT):
        decision = policy(ActionRequest(action=action, params={}))
        assert not decision.allowed
        assert not decision.allow_privileged


def test_cli_policy_without_required_delegation_is_unchanged(world):
    token = world.credential()  # no world scopes
    credential = world.auth.authenticate(token)
    session = world.auth.acquire_session(
        token,
        "AIBot",
        owner_id=credential.owner_id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    policy = world.auth.action_policy(session, admin_token=world.world_admin)

    decision = policy(ActionRequest(action=ActionType.TELEPORT, params={}))
    assert decision.allowed and decision.allow_privileged
