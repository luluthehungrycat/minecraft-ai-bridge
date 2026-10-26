"""LLM client abstraction — supports OpenAI, Anthropic, Ollama,
OpenRouter, and OpenCode Server.

The ``LLMClient`` interface provides a unified way to send messages and
receive structured actions (via tool calls or parsed text output).
"""

from __future__ import annotations

import json
import logging
import os
import string
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, cast

from .models import LLMResponse, Message, Role, ToolCall

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletionMessageParam,
        ChatCompletionToolChoiceOptionParam,
        ChatCompletionToolParam,
    )

logger = logging.getLogger(__name__)


# ── Tool definition (OpenAI / Anthropic format) ─────────────────────────

ACTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "execute_action",
        "description": "Execute an action in Minecraft",
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Step-by-step reasoning for this action",
                },
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "move_to",
                        "move_forward",
                        "move_back",
                        "walk_to",
                        "turn_left",
                        "turn_right",
                        "jump",
                        "teleport",
                        "break_block",
                        "place_block",
                        "interact",
                        "check_inventory",
                        "equip_item",
                        "equip_best_weapon",
                        "craft_item",
                        "drop_item",
                        "attack",
                        "scan",
                        "check_time",
                        "check_weather",
                        "check_health",
                        "check_position",
                        "list_players",
                        "chat",
                        "wait",
                        "done",
                    ],
                },
                "action_params": {
                    "type": "object",
                    "description": "Parameters for the chosen action",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "steps": {"type": "number"},
                        "block_type": {"type": "string"},
                        "item_type": {"type": "string"},
                        "amount": {"type": "integer"},
                        "slot": {"type": "integer"},
                        "radius": {"type": "integer"},
                        "seconds": {"type": "number"},
                        "message": {"type": "string"},
                        "entity_type": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["reasoning", "action"],
        },
    },
}


# ── Abstract client ─────────────────────────────────────────────────────


class LLMClient(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """Send conversation context and get back an action decision."""
        ...

    @abstractmethod
    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        """Decompose a high-level goal into structured sub-goals."""
        ...


# ── Shared helpers ──────────────────────────────────────────────────────


def _parse_json_from_text(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from arbitrary text."""
    import re

    json_pattern = r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _make_wait_response(reason: str, seconds: int = 5) -> LLMResponse:
    return LLMResponse(
        action="wait",
        action_params={"seconds": seconds},
        reasoning=reason,
    )


# ── OpenAI implementation ──────────────────────────────────────────────


class OpenAIClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = default_headers

        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        openai_messages = [
            {"role": "system", "content": system_prompt},
            *[{"role": m.role.value, "content": m.content} for m in messages],
        ]

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=cast("list[ChatCompletionMessageParam]", openai_messages),
                tools=[cast("ChatCompletionToolParam", ACTION_TOOL)],
                tool_choice=cast("ChatCompletionToolChoiceOptionParam", tool_choice),
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            logger.error("OpenAI API call failed: %s", exc)
            return _make_wait_response(f"API error: {exc}. Waiting and retrying.")

        choice = response.choices[0]
        msg = choice.message

        # Tool call path
        # Only function tool calls carry ACTION_TOOL arguments.
        if msg.tool_calls and msg.tool_calls[0].type == "function":
            tc = msg.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_call = ToolCall(
                id=tc.id,
                name=args.pop("action", "wait"),
                arguments=args.pop("action_params", {}),
            )
            if "reasoning" in args:
                tool_call.arguments["reasoning"] = args["reasoning"]
            return LLMResponse.from_tool_call(tool_call)

        # Text response path (fallback)
        content = msg.content or ""
        return self._parse_text_response(content)

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        from .prompts import GOAL_DECOMPOSE_PROMPT

        # Format goal safely — str.format() crashes on "{" inside goal
        prompt = string.Template(GOAL_DECOMPOSE_PROMPT).safe_substitute(goal=goal)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a Minecraft task planner. Return only valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                subgoals = parsed.get("subgoals", parsed.get("steps", []))
                if isinstance(subgoals, list):
                    return subgoals
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as exc:
            logger.warning("Goal decomposition failed: %s", exc)
            return []

    def _parse_text_response(self, content: str) -> LLMResponse:
        obj = _parse_json_from_text(content)
        if obj and "action" in obj:
            return LLMResponse(
                thought=obj.get("reasoning", obj.get("thought", "")),
                action=obj["action"],
                action_params=obj.get("action_params", {}),
                reasoning=obj.get("reasoning", ""),
            )
        return _make_wait_response("Could not parse LLM output.", seconds=3)


# ── Anthropic implementation ────────────────────────────────────────────


class AnthropicClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise ImportError(
                "Install anthropic package: pip install minecraft-ai-bridge[anthropic]"
            ) from None
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        anthropic_messages = [
            {"role": m.role.value, "content": m.content} for m in messages if m.role != Role.SYSTEM
        ]

        tool_config = {
            "name": "execute_action",
            "description": ACTION_TOOL["function"]["description"],
            "input_schema": ACTION_TOOL["function"]["parameters"],
        }

        try:
            response = await self._client.messages.create(
                model=self._model,
                system=system_prompt,
                messages=anthropic_messages,
                tools=[tool_config],
                tool_choice={"type": "auto"} if tool_choice == "auto" else None,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            logger.error("Anthropic API call failed: %s", exc)
            return _make_wait_response(f"API error: {exc}. Waiting and retrying.")

        for block in response.content:
            if block.type == "tool_use":
                args = (
                    block.input if hasattr(block, "input") else block.model_dump().get("input", {})
                )
                return LLMResponse(
                    thought="",
                    action=args.get("action", "wait"),
                    action_params=args.get("action_params", {}),
                    reasoning=args.get("reasoning", ""),
                )
            if block.type == "text" and block.text:
                obj = _parse_json_from_text(block.text)
                if obj and "action" in obj:
                    return LLMResponse(
                        action=obj["action"],
                        action_params=obj.get("action_params", {}),
                        reasoning=obj.get("reasoning", ""),
                    )

        return _make_wait_response("No tool call or parseable response from Anthropic.", seconds=3)

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        from .prompts import GOAL_DECOMPOSE_PROMPT

        prompt = string.Template(GOAL_DECOMPOSE_PROMPT).safe_substitute(goal=goal)
        try:
            response = await self._client.messages.create(
                model=self._model,
                system="You are a Minecraft task planner. Return only valid JSON.",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            content = "".join(b.text for b in response.content if b.type == "text")
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                subgoals = parsed.get("subgoals", parsed.get("steps", []))
                return subgoals if isinstance(subgoals, list) else []
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as exc:
            logger.warning("Goal decomposition failed: %s", exc)
            return []


# ── Ollama (local) implementation ───────────────────────────────────────


class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        import httpx

        ollama_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            ollama_messages.append({"role": m.role.value, "content": m.content})

        payload = {
            "model": self._model,
            "messages": ollama_messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": False,
            "format": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "{}")
        except Exception as exc:
            logger.error("Ollama call failed: %s", exc)
            return _make_wait_response(f"Ollama error: {exc}")

        # Try strict JSON parse first
        try:
            parsed = json.loads(content)
            return LLMResponse(
                action=parsed.get("action", "wait"),
                action_params=parsed.get("action_params", {}),
                reasoning=parsed.get("reasoning", ""),
            )
        except json.JSONDecodeError:
            pass

        # Fallback: try to extract JSON from text (some models ignore format: json)
        obj = _parse_json_from_text(content)
        if obj and "action" in obj:
            return LLMResponse(
                action=obj["action"],
                action_params=obj.get("action_params", {}),
                reasoning=obj.get("reasoning", ""),
            )

        # Final fallback: retry WITHOUT forced JSON format
        logger.warning(
            "JSON mode failed for model %s — retrying without forced JSON format", self._model
        )
        try:
            import httpx as _httpx

            retry_payload = {
                "model": self._model,
                "messages": ollama_messages,
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "stream": False,
                # Deliberately omit format: json
            }
            async with _httpx.AsyncClient(timeout=60) as http:
                resp = await http.post(f"{self._base_url}/api/chat", json=retry_payload)
                resp.raise_for_status()
                retry_data = resp.json()
                retry_content = retry_data.get("message", {}).get("content", "{}")
            obj = _parse_json_from_text(retry_content)
            if obj and "action" in obj:
                return LLMResponse(
                    action=obj["action"],
                    action_params=obj.get("action_params", {}),
                    reasoning=obj.get("reasoning", ""),
                )
        except Exception as retry_err:
            logger.warning("Ollama fallback also failed: %s", retry_err)

        return _make_wait_response("JSON parse error from Ollama.", seconds=3)

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        import httpx

        from .prompts import GOAL_DECOMPOSE_PROMPT

        prompt = string.Template(GOAL_DECOMPOSE_PROMPT).safe_substitute(goal=goal)
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a Minecraft task planner. Return only valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "stream": False,
            "format": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post(f"{self._base_url}/api/chat", json=payload)
                resp.raise_for_status()
                content = resp.json().get("message", {}).get("content", "{}")
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                subgoals = parsed.get("subgoals", parsed.get("steps", []))
                return subgoals if isinstance(subgoals, list) else []
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as exc:
            logger.warning("Goal decomposition failed: %s", exc)
            return []


# ── OpenRouter implementation (OpenAI-compatible proxy) ─────────────────


class OpenRouterClient(LLMClient):
    """OpenRouter is an OpenAI-compatible API proxy.

    Uses the OpenAI SDK under the hood — just points at
    ``https://openrouter.ai/api/v1`` and adds the required headers.

    OpenRouter also supports any model available through its catalog.
    Model names use the ``provider/model`` format, e.g.:
    ``openai/gpt-4o``, ``anthropic/claude-sonnet-4``, ``meta-llama/llama-3``
    """

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        referer: str = "",
        title: str = "Minecraft AI Bridge",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        from openai import AsyncOpenAI

        headers = {
            "HTTP-Referer": referer or "https://github.com/minecraft-ai-bridge",
            "X-OpenRouter-Title": title,
        }

        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            default_headers=headers,
        )
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        openai_messages = [
            {"role": "system", "content": system_prompt},
            *[{"role": m.role.value, "content": m.content} for m in messages],
        ]

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=cast("list[ChatCompletionMessageParam]", openai_messages),
                tools=[cast("ChatCompletionToolParam", ACTION_TOOL)],
                tool_choice=cast("ChatCompletionToolChoiceOptionParam", tool_choice),
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            logger.error("OpenRouter API call failed: %s", exc)
            return _make_wait_response(f"OpenRouter error: {exc}")

        choice = response.choices[0]
        msg = choice.message

        # Only function tool calls carry ACTION_TOOL arguments.
        if msg.tool_calls and msg.tool_calls[0].type == "function":
            tc = msg.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_call = ToolCall(
                id=tc.id,
                name=args.pop("action", "wait"),
                arguments=args.pop("action_params", {}),
            )
            if "reasoning" in args:
                tool_call.arguments["reasoning"] = args["reasoning"]
            return LLMResponse.from_tool_call(tool_call)

        content = msg.content or ""
        obj = _parse_json_from_text(content)
        if obj and "action" in obj:
            return LLMResponse(
                thought=obj.get("reasoning", obj.get("thought", "")),
                action=obj["action"],
                action_params=obj.get("action_params", {}),
                reasoning=obj.get("reasoning", ""),
            )
        return _make_wait_response("Could not parse OpenRouter response.", seconds=3)

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        from .prompts import GOAL_DECOMPOSE_PROMPT

        prompt = string.Template(GOAL_DECOMPOSE_PROMPT).safe_substitute(goal=goal)
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a Minecraft task planner. Return only valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                subgoals = parsed.get("subgoals", parsed.get("steps", []))
                return subgoals if isinstance(subgoals, list) else []
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as exc:
            logger.warning("Goal decomposition failed: %s", exc)
            return []


# ── OpenCode Server implementation (session-based API) ──────────────────


class OpenCodeServerClient(LLMClient):
    """Connects to an attachable OpenCode server via its session API.

    The OpenCode server provides a session-based inference endpoint
    at ``POST /session/{id}/message``.  This client:
      1. Creates a session on the server on first use.
      2. Sends each ``decide()`` call as a message to that session.
      3. Parses tool-use parts from the response.

    Works with any OpenCode server including the ``opencode serve``
    headless mode.  Ideal for using local inference (e.g. Big Pickle)
    that OpenCode manages for you.

    Configuration
    -------------
    ``opencode_server_url`` — base URL of the OpenCode server
        (default ``http://localhost:4096``)
    ``opencode_server_api_key`` — optional API key
    ``opencode_server_model`` — model to use (default ``big-pickle``)
    """

    def __init__(
        self,
        server_url: str = "http://localhost:4096",
        api_key: str = "",
        model: str = "big-pickle",
    ) -> None:
        import httpx

        self._base_url = server_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._session_id: str | None = None
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=120,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
        )

    async def _ensure_session(self) -> str:
        """Create a session on the server if one doesn't exist yet."""
        if self._session_id is not None:
            return self._session_id

        logger.info("Creating session on OpenCode server at %s", self._base_url)
        try:
            resp = await self._http.post("/sessions", json={})
            resp.raise_for_status()
            data = resp.json()
            # The session ID may be under "id" or "_id" or "sessionID"
            self._session_id = (
                data.get("id") or data.get("_id") or data.get("sessionID") or data.get("session_id")
            )
            if not self._session_id:
                logger.warning("Could not find session ID in response: %s", data)
                # Fallback: use a generated ID
                self._session_id = f"session_{id(self)}"
            logger.info("Session created: %s", self._session_id)
        except Exception as exc:
            logger.warning("Session creation failed (%s). Using fallback session ID.", exc)
            self._session_id = f"session_{id(self)}"

        return self._session_id

    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        await self._ensure_session()

        # Build the parts array from the conversation
        parts: list[dict[str, Any]] = []
        for m in messages:
            parts.append({"type": "text", "text": f"{m.role.value}: {m.content}"})

        request_body: dict[str, Any] = {
            "parts": parts,
            "system": system_prompt,
            "tools": [ACTION_TOOL],
        }

        # Only send model if explicitly set (server picks default otherwise)
        if self._model:
            request_body["model"] = {
                "providerID": "opencode",
                "modelID": self._model,
            }

        # Map OpenCode server models to provider/model format if needed
        if self._model and "/" in self._model:
            provider_id, model_id = self._model.split("/", 1)
            request_body["model"] = {
                "providerID": provider_id,
                "modelID": model_id,
            }

        try:
            resp = await self._http.post(
                f"/session/{self._session_id}/message",
                json=request_body,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("OpenCode server call failed: %s", exc)
            return _make_wait_response(f"OpenCode server error: {exc}")

        return self._parse_response(data)

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse a server response into an LLMResponse.

        The response format from ``POST /session/{id}/message`` is::

            {
              "info": { "id": "...", "role": "assistant", ... },
              "parts": [
                { "type": "text", "text": "..." },
                { "type": "tool_use",
                  "tool_use": { "name": "execute_action", "input": {...} } }
              ]
            }

        Tool calls may also appear as OpenCode's internal format:
        ``{ "type": "tool_use", "id": "...", "name": "...", "input": {...} }``
        """
        parts = data.get("parts", [])

        for part in parts:
            ptype = part.get("type", "")

            # Standard OpenCode tool_use part
            if ptype == "tool_use":
                tool_block = part.get("tool_use", part)  # some servers flatten
                if isinstance(tool_block, dict):
                    name = tool_block.get("name", "")
                    inp = tool_block.get("input", {})
                    if name == "execute_action":
                        return LLMResponse(
                            thought="",
                            action=inp.get("action", "wait"),
                            action_params=inp.get("action_params", {}),
                            reasoning=inp.get("reasoning", ""),
                        )
                    return LLMResponse(
                        thought="",
                        action=name,
                        action_params=inp if isinstance(inp, dict) else {},
                        reasoning="",
                    )

            # Tool call in OpenAI-style part
            if ptype == "function" or "function" in part:
                fn = part.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                return LLMResponse(
                    thought="",
                    action=args.get("action", fn.get("name", "wait")),
                    action_params=args.get("action_params", {}),
                    reasoning=args.get("reasoning", ""),
                )

            # Text part — attempt JSON extraction
            if ptype == "text":
                text = part.get("text", "")
                obj = _parse_json_from_text(text)
                if obj and "action" in obj:
                    return LLMResponse(
                        thought=obj.get("reasoning", obj.get("thought", "")),
                        action=obj["action"],
                        action_params=obj.get("action_params", {}),
                        reasoning=obj.get("reasoning", ""),
                    )

        # Fallback: check the whole response for a text field
        full_text = data.get("text", "") or (parts[0].get("text", "") if parts else "")
        obj = _parse_json_from_text(full_text)
        if obj and "action" in obj:
            return LLMResponse(
                thought=obj.get("reasoning", ""),
                action=obj["action"],
                action_params=obj.get("action_params", {}),
                reasoning=obj.get("reasoning", ""),
            )

        return _make_wait_response("No parseable tool call from OpenCode server.", seconds=3)

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        from .prompts import GOAL_DECOMPOSE_PROMPT

        await self._ensure_session()

        prompt = string.Template(GOAL_DECOMPOSE_PROMPT).safe_substitute(goal=goal)
        request_body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "system": "You are a Minecraft task planner. Return only valid JSON.",
        }
        if self._model:
            if "/" in self._model:
                p, m = self._model.split("/", 1)
                request_body["model"] = {"providerID": p, "modelID": m}
            else:
                request_body["model"] = {
                    "providerID": "opencode",
                    "modelID": self._model,
                }

        try:
            resp = await self._http.post(
                f"/session/{self._session_id}/message",
                json=request_body,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Goal decomposition failed: %s", exc)
            return []

        # Find text parts and try to parse JSON
        content = ""
        for part in data.get("parts", []):
            if part.get("type") == "text":
                content += part.get("text", "")

        if not content:
            return []

        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                subgoals = parsed.get("subgoals", parsed.get("steps", []))
                return subgoals if isinstance(subgoals, list) else []
            if isinstance(parsed, list):
                return parsed
            return []
        except json.JSONDecodeError:
            return []

    async def close(self) -> None:
        """Close the HTTP session.  MUST be called on shutdown."""
        await self._http.aclose()
        self._session_id = None


# ── Factory ─────────────────────────────────────────────────────────────


def create_llm_client(config: Any) -> LLMClient:
    """Build the right LLM client based on config."""
    provider = config.llm.provider
    model = config.llm.model
    temp = config.llm.temperature
    max_tok = config.llm.max_tokens

    logger.info("Initializing LLM client: provider=%s model=%s", provider, model)

    if provider == "openai":
        key = config.llm.openai_api_key or os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise ValueError(
                "OpenAI API key not set. Set OPENAI_API_KEY env var or "
                "llm.openai_api_key in config."
            )
        return OpenAIClient(api_key=key, model=model, temperature=temp, max_tokens=max_tok)

    elif provider == "anthropic":
        key = config.llm.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("Anthropic API key not set. Set ANTHROPIC_API_KEY env var.")
        return AnthropicClient(api_key=key, model=model, temperature=temp, max_tokens=max_tok)

    elif provider == "ollama":
        base = config.llm.ollama_base_url
        return OllamaClient(base_url=base, model=model, temperature=temp, max_tokens=max_tok)

    elif provider == "openrouter":
        key = config.llm.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
        if not key:
            raise ValueError("OpenRouter API key not set. Set OPENROUTER_API_KEY env var.")
        return OpenRouterClient(
            api_key=key,
            model=model,
            temperature=temp,
            max_tokens=max_tok,
            referer=config.llm.openrouter_referer,
            title=config.llm.openrouter_title,
            base_url=config.llm.openrouter_base_url,
        )

    elif provider == "opencode_server":
        return OpenCodeServerClient(
            server_url=config.llm.opencode_server_url,
            api_key=config.llm.opencode_server_api_key,
            model=config.llm.opencode_server_model,
        )

    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider}. "
            f"Supported: openai, anthropic, ollama, openrouter, opencode_server"
        )
