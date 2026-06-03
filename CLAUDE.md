# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

TraceClaw is an enterprise-grade transparent AI agent framework built on LangGraph. It gives users full observability into what their AI agent is doing — every LLM call, tool invocation, and system action is audited via JSONL logs and a Rich-based monitor terminal.

## Commands

```bash
# Install in editable mode (also installs all dependencies)
pip install -e .

# Alternative: install deps only (no package install)
pip install -r requirements.txt

# Run the interactive agent
traceclaw run

# Run the configuration wizard (sets up .env)
traceclaw config

# Run the monitor terminal (in a separate window — tail logs)
traceclaw monitor

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python tests/test_builtins.py

# Run the two-phase skills experiment
python -c "from tests.test_two_phase_skills import run_tests; run_tests()"

# Run the lazy-loading benchmark
python examples/benchmark_lazy_loading.py
```

## Architecture

The system is a **LangGraph StateGraph** with two nodes — `agent` and `tools` — running in a loop (agent → tools → agent until the agent decides to reply directly).

### Entry points

Three CLI commands are registered via `setup.py` → `entry/cli.py` (Typer app):
- `traceclaw run` → `entry/main.py` — full interactive agent with Prompt Toolkit UI
- `traceclaw config` → `entry/cli.py:config_wizard` — interactive .env setup wizard
- `traceclaw monitor` → `entry/monitor.py` — live JSONL log viewer using Rich panels

### Core modules

| Module | Role |
|--------|------|
| `traceclaw/core/agent.py` | Builds the LangGraph StateGraph. Constructs the system prompt from user profile + context summary, binds tools, invokes the LLM, audits every step. The `agent_node` function is the central decision loop. |
| `traceclaw/core/config.py` | Resolves all directory paths (`WORKSPACE_DIR`, `OFFICE_DIR`, `SKILLS_DIR`, `DB_PATH`, etc.) from `TRACECLAW_WORKSPACE` env var or defaults to `<project>/workspace/`. Also creates dirs on import. |
| `traceclaw/core/provider.py` | LLM provider factory — maps provider name strings to LangChain chat models (ChatOpenAI for OpenAI/aliyun/z.ai/tencent, ChatAnthropic, ChatOllama). Includes fallback base URLs for popular Chinese providers. |
| `traceclaw/core/context.py` | Defines `AgentState` (messages + summary) and `trim_context_messages()` — when total turns exceed `trigger_turns` (40), older turns are discarded and summarized, keeping the most recent `keep_turns` (10). Groups messages by user turn (HumanMessage → next HumanMessage). |
| `traceclaw/core/skill_loader.py` | `LazySkillLoader` class — scans `workspace/office/skills/` for folders containing `SKILL.md`, extracts name/description from frontmatter, and creates LangChain `StructuredTool` wrappers. Uses LRU caching (max 50). Tools enforce a **two-phase call protocol**: `mode='help'` first (returns the SKILL.md content), then `mode='run'` with the assembled command. |
| `traceclaw/core/logger.py` | Singleton `JSONLEventLogger` — background thread writes structured JSONL events to `logs/<thread_id>.jsonl`. Five event types: `llm_input`, `tool_call`, `tool_result`, `ai_message`, `system_action`. |
| `traceclaw/core/bus.py` | Global `asyncio.Queue` singleton — the single input channel for the entire system. User keyboard input and heartbeat task triggers both `put()` onto `task_queue`; the `agent_worker` coroutine blocks on `get()` and processes messages serially. This design avoids multi-user concurrency issues at the cost of no parallel request handling (correct tradeoff for a personal agent). |
| `traceclaw/core/heartbeat.py` | `pacemaker_loop` — async coroutine that polls `workspace/tasks.json` every 10s. When a task's `target_time` ≤ now, it calls `emit_task()` (from `bus.py`) to push a message onto the shared `task_queue`, then computes the next occurrence for recurring tasks (hourly/daily/weekly/monthly). |

### Tools layer

- `traceclaw/core/tools/base.py` — `TraceClawBaseTool` ABC (class-based tool) and `traceclaw_tool` alias for LangChain's `@tool` decorator.
- `traceclaw/core/tools/builtins.py` — 12 built-in tools compiled into `BUILTIN_TOOLS` list. Includes time queries, calculator, user profile management, task scheduling (CRUD on `workspace/tasks.json`), and the four sandbox tools. Uses a `threading.Lock` for task file writes.
- `traceclaw/core/tools/sandbox_tools.py` — File I/O and shell execution confined to `OFFICE_DIR`. `_get_safe_path()` enforces that all resolved absolute paths start with the office directory. `execute_office_shell` has five regex-based dangerous-pattern checks preventing path traversal, absolute paths, and drive-letter escapes on Windows. Commands get a 60-second timeout.

### Memory system (dual-watermark)

- **Long-term**: `workspace/memory/user_profile.md` — manually or tool-edited Markdown file storing user preferences, injected into the system prompt.
- **Short-term**: SQLite checkpointing via LangGraph's `AsyncSqliteSaver` (`workspace/state.sqlite3`) — stores full conversation history. When turns exceed `trigger_turns=40`, `agent_node` invokes the same LLM to summarize discarded turns into a running `summary` string, then issues `RemoveMessage` commands to prune old messages from state.

### Conversation flow

1. User types input in the Prompt Toolkit session → placed on `bus.task_queue` (shared `asyncio.Queue`)
2. `agent_worker` coroutine consumes the queue, wraps input as `HumanMessage`, calls `app.astream()`
3. `agent_node` trims context, reads user profile, constructs system prompt, invokes LLM with tools bound
4. `tools_condition` routes to `tool_node` if LLM emitted tool calls, else to `END`
5. Tool results return to `agent_node` for the next thinking cycle
6. `heartbeat_worker` (`pacemaker_loop`) runs in parallel, pushing scheduled task messages into the same queue

## Configuration

`.env` at the project root (copied from `.env.example`). Key variables:
- `DEFAULT_PROVIDER` — `openai`, `anthropic`, `aliyun`, `tencent`, `z.ai`, `ollama`
- `DEFAULT_MODEL` — model name string (e.g. `gpt-4o-mini`, `glm-5`, `qwen-max`)
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — provider-specific keys
- `OPENAI_API_BASE` — base URL for OpenAI-compatible providers (aliyun dashscope, etc.)
- `OLLAMA_BASE_URL` — Ollama server URL (defaults to `http://localhost:11434`)
- `TRACECLAW_WORKSPACE` — optional override for the workspace directory

## Test patterns

Tests use `unittest.TestCase` and are runnable both via pytest and directly (`python tests/test_*.py`). Tests typically `sys.path.insert` the project root.

| Test file | Covers |
|-----------|--------|
| `test_agent.py` | Core agent loop, LLM invocation, tool binding |
| `test_builtins.py` | 12 built-in tools (time, calculator, profile, tasks, sandbox) |
| `test_config_and_skill_loader.py` | Path resolution, env loading, skill scanning |
| `test_context_advanced.py` | Context trimming algorithm, turn grouping, summarization |
| `test_heartbeat.py` | Pacemaker loop, recurring task scheduling |
| `test_lazy_loader.py` | LazySkillLoader — metadata caching, LRU eviction, hot reload |
| `test_sandbox_tools.py` | Path sandbox (`_get_safe_path`), dangerous command regex filtering |
| `test_two_phase_skills.py` | Full integration test — creates fake SKILL.md tools with traps (look-alike tool names) and measures whether the LLM correctly uses the help→run protocol vs. blindly calling `run`. Safety hit rate improved from 50% → 90% after two-phase enforcement. |

## Auxiliary directories

- `examples/basic_usage.py` — programmatic API usage (no TUI). Shows how to call `create_agent_app()` and stream events directly.
- `examples/benchmark_lazy_loading.py` — performance micro-benchmark for the lazy loader across 10/30/50/100 skills.
- `docs/` — contains lazy-loading guides (`LAZY_LOADING_GUIDE.md`, `LAZY_LOADING_QUICKSTART.md`, `LAZY_LOADING_SUMMARY.md`) and `two_phase_comparison.html` (benchmark visualization).
- `CHANGELOG.md` — documents the lazy-loading refactoring: startup time ↓99.98% (2000ms → 0.4ms for 100 skills), memory ↓80%.
- `extractedCode.txt` — stale code dump, not part of the project. Ignore it.

## Skills (pluggable tools)

Skills are folders under `workspace/office/skills/` containing a `SKILL.md` with YAML frontmatter (`name:`, `description:`) and shell-oriented instructions. Skills are lazily loaded — metadata is scanned at startup (cached 60s), content is read only on first `mode='help'` invocation. Compatible with both OpenClaw and Claude Code skill ecosystems.
