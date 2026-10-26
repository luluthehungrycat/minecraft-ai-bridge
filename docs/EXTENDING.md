# Extending the Bridge

This guide covers how to add new actions, LLM providers, goal decompositions, and memory strategies.

## Adding a New Action

Adding a new action requires changes in 5 files. Here's a step-by-step example adding an `inspect_entity` action.

### Step 1: Add to ActionType Enum

**File**: `minecraft_ai_bridge/minecraft/actions.py`

```python
class ActionType(str, Enum):
    # ... existing actions ...
    
    # ── Information ─────────────────────────────────────────────────
    SCAN = "scan"
    INSPECT_ENTITY = "inspect_entity"  # NEW
    CHECK_TIME = "check_time"
```

### Step 2: Write the Handler

**File**: `minecraft_ai_bridge/minecraft/actions.py`

```python
async def _inspect_entity(mc: WorldAdapter, params: dict) -> ActionResult:
    entity_type = params.get("entity_type", "nearest")
    # MCPQ doesn't directly support entity inspection,
    # so use a command
    if entity_type == "nearest":
        resp = await _cmd(mc, "data get entity @e[limit=1,sort=nearest]")
    else:
        resp = await _cmd(mc, f"data get entity @e[type={entity_type},limit=1]")
    
    return ActionResult(
        success=True,
        action=ActionType.INSPECT_ENTITY,
        message=f"Inspected {entity_type}",
        data={"raw": resp},
    )
```

### Step 3: Register in Handler Dict

**File**: `minecraft_ai_bridge/minecraft/actions.py`

```python
_HANDLERS: dict[ActionType, Handler] = {
    # ... existing handlers ...
    ActionType.INSPECT_ENTITY: _inspect_entity,  # NEW
}
```

### Step 4: Add to Action Tool Schema

**File**: `minecraft_ai_bridge/llm/client.py`

Find the `ACTION_TOOL` dict and add the new action to the `action` enum and to the `action_params` properties:

```python
ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "act",
        "parameters": {
            "properties": {
                "action": {
                    "enum": [
                        # ... existing actions ...
                        "inspect_entity",  # NEW
                    ],
                },
                "action_params": {
                    "properties": {
                        # ... existing params ...
                        "entity_type": {        # NEW
                            "type": "string",
                            "description": "Type of entity to inspect (default: nearest)",
                            "enum": ["nearest", "minecraft:zombie", "minecraft:cow", ...],
                        },
                    },
                },
            },
        },
    },
}
```

### Step 5: Add to System Prompt

**File**: `minecraft_ai_bridge/llm/prompts.py`

In the `SYSTEM_PROMPT` string, add the action to the appropriate category:

```
Information:
  inspect_entity - Inspect the nearest or specified entity's NBT data. Params: entity_type (string, optional)
```

### Step 6: Verify

```bash
# Check imports
python -c "from minecraft_ai_bridge.minecraft.actions import ActionType; print(ActionType.INSPECT_ENTITY)"

# Run bridge with verbose to see action in choices
minecraft-ai-bridge --verbose "Inspect nearby entities"
```

## Adding a New LLM Provider

LLM providers implement the `LLMClient` abstract base class.

### Step 1: Create the Client Class

**File**: `minecraft_ai_bridge/llm/client.py`

```python
class GroqClient(LLMClient):
    """LLM client for Groq API (OpenAI-compatible)."""
    
    def __init__(self, config: AppConfig) -> None:
        self._model = config.llm.model
        self._api_key = config.llm.groq_api_key or os.getenv("GROQ_API_KEY", "")
        if not self._api_key:
            raise ValueError("GROQ_API_KEY not set")
        self._client = httpx.AsyncClient(
            base_url="https://api.groq.com/openai/v1",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
    
    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
    ) -> LLMResponse:
        # Build OpenAI-compatible messages
        msgs = [
            {"role": "system", "content": system_prompt},
        ]
        for msg in messages:
            msgs.append({
                "role": msg.role.value,
                "content": msg.content,
            })
        
        # Call chat completions with tool
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": msgs,
                "tools": [ACTION_TOOL],
                "tool_choice": "required",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Parse tool call
        choice = data["choices"][0]
        tool_call = choice["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
        
        return LLMResponse(
            action=args["action"],
            action_params=args.get("action_params", {}),
            reasoning=args.get("reasoning", ""),
        )
    
    async def close(self) -> None:
        await self._client.aclose()
```

### Step 2: Add Config Fields

**File**: `minecraft_ai_bridge/config.py`

In `LLMConfig`:

```python
class LLMConfig(BaseSettings):
    provider: Literal["openai", "anthropic", "ollama", "openrouter", "opencode_server", "groq"] = "openai"
    # ... existing fields ...
    
    # Groq
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
```

### Step 3: Register in Factory

**File**: `minecraft_ai_bridge/llm/client.py`

```python
def create_llm_client(config: AppConfig) -> LLMClient:
    provider = config.llm.provider
    if provider == "openai":
        return OpenAIClient(config)
    elif provider == "anthropic":
        return AnthropicClient(config)
    elif provider == "ollama":
        return OllamaClient(config)
    elif provider == "openrouter":
        return OpenRouterClient(config)
    elif provider == "opencode_server":
        return OpenCodeServerClient(config)
    elif provider == "groq":            # NEW
        return GroqClient(config)       # NEW
    else:
        raise ValueError(f"Unknown provider: {provider}")
```

### Step 4: Update Enums and Docs

- Add to the `Literal` type in `config.py`
- Add to `main.py`'s `_list_providers()`
- Add to `.env.example`
- Add to `README.md` tables

## Customizing Goal Decomposition

### Extending Fallback Plans

**File**: `minecraft_ai_bridge/bridge/goal_manager.py`

Add new patterns to `_FALLBACK_PLANS`:

```python
_FALLBACK_PLANS: list[tuple[str, str, list[str]]] = [
    # ... existing patterns ...
    (r"fish|rod|ocean|sea|water", "Fishing expedition", [
        "Craft a fishing rod",
        "Find a body of water",
        "Fish until you catch something",
        "Cook any raw fish caught",
        "Store or use the fish",
    ]),
    (r"defend|fort|wall|castle|base", "Build a defensive structure", [
        "Scout the area for a good location",
        "Gather stone or cobblestone",
        "Build a perimeter wall",
        "Add defensive features (moat, wall, battlements)",
        "Build a secure entrance",
        "Light up the interior to prevent mob spawns",
    ]),
]
```

The regex is matched against the goal text (case-insensitive). The first match wins, so order matters — put more specific patterns first.

### Custom Decomposition Strategies

You can also override the LLM decomposition entirely by subclassing `GoalManager`:

```python
class CustomGoalManager(GoalManager):
    async def _decompose_with_llm(self, goal: str) -> list[str]:
        # Custom logic
        if "nether" in goal.lower():
            return [
                "Mine 10 obsidian",
                "Craft a flint and steel",
                "Build a nether portal (4x5 frame)",
                "Light the portal and enter the Nether",
                "Explore the Nether safely",
            ]
        return await super()._decompose_with_llm(goal)
```

## Custom Memory Strategies

The `AgentMemory` class can be extended or replaced. The interface used by `Orchestrator`:

```python
class AgentMemory:
    def record_action(self, action: str, result: dict) -> None: ...
    def record_observation(self, state: WorldState) -> None: ...
    def remember_fact(self, fact: str) -> None: ...
    def notable_facts(self) -> str: ...
    def recent_messages(self, n: int) -> list[Message]: ...
    @property
    def short_term_summary(self) -> str: ...
```

To implement a persistent memory backend (e.g., SQLite or Redis):

```python
class PersistentMemory(AgentMemory):
    def __init__(self, db_path: str = "memory.db"):
        import sqlite3
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT, success BOOL, message TEXT,
                timestamp REAL
            )
        """)
        # ... etc.
    
    def record_action(self, action: str, result: dict) -> None:
        self._conn.execute(
            "INSERT INTO actions (action, success, message, timestamp) VALUES (?, ?, ?, ?)",
            (action, result.get("success"), result.get("message", ""), time.time()),
        )
        self._conn.commit()
```

Then swap it in `Orchestrator.__init__()`:

```python
self._memory = PersistentMemory("/data/memory.db")
```

## Customizing the Observer

### Adding New Observation Sources

In `minecraft_ai_bridge/minecraft/observer.py`, modify the `observe()` method:

```python
async def observe(self) -> WorldState:
    pos_task = self._safe_get_player_pos()
    health_task = self._safe_run_command("data get entity @p Health")
    inv_task = self._safe_run_command("data get entity @p Inventory")
    time_task = self._mc.get_time()  # could also use /time query
    weather_task = self._safe_run_command("weather query")
    players_task = self._mc.get_players_online()
    xp_task = self._safe_run_command("data get entity @p Xp")  # NEW
    
    results = await asyncio.gather(
        pos_task, health_task, inv_task, time_task,
        weather_task, players_task, xp_task,  # NEW
        return_exceptions=True,
    )
    
    # ... parse results ...
    world_state.xp_level = self._parse_xp(results[6])  # NEW
    return world_state
```

## Testing Changes

```bash
# Syntax check
python -c "import minecraft_ai_bridge; print('OK')"

# Check CLI works
minecraft-ai-bridge --help
minecraft-ai-bridge --list-providers
minecraft-ai-bridge --version

# Run with a simple goal and verbose logging
minecraft-ai-bridge --verbose "Wait 3 seconds"

# Unit tests (if you add them)
pytest tests/
```
