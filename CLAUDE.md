# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Agent-layer observability for OpenClaw using Langfuse and OpenTelemetry. The project instruments OpenClaw gateway interactions with proper trace context (Input/Output, token usage, agent metrics) since OpenClaw's built-in OTel plugin doesn't emit `gen_ai.*` semantic convention attributes — leaving Input/Output null in Langfuse without workarounds.

## Environment Setup

- **Python env**: `conda run -n performance --no-capture-output python <script>`
- **Config**: `.env` at project root (Langfuse keys, DashScope keys)
- **OpenClaw config**: `~/.openclaw/openclaw.json` (gateway token, OTel settings)

## Running Scripts

```bash
# Quick start: Langfuse + Qwen via DashScope
conda run -n performance --no-capture-output python quick_start/a0.py

# OpenClaw gateway test (sends to /v1/responses, records to Langfuse via shared tracer)
conda run -n performance --no-capture-output python test/openclaw/test_chat.py

# OpenClaw observe: chat / watch daemon / analyze
conda run -n performance --no-capture-output python openclaw/explore_and_test/openclaw_observe.py chat "message"
conda run -n performance --no-capture-output python openclaw/explore_and_test/openclaw_observe.py watch --interval 30
conda run -n performance --no-capture-output python openclaw/explore_and_test/openclaw_observe.py analyze --hours 4
```

## Architecture

```
Client (test script / curl / web UI)
    |
    v
OpenClaw Gateway (localhost:18789)
    - /v1/responses (OpenResponses API)
    - /v1/chat/completions (OpenAI-compatible)
    |
    v
Shared tracer module (openclaw/tracer.py)
    - StartGen/End pattern (like xkong-agent-center's tracer.go)
    - Records generation with Input/Output, tokens, agent metrics
    - Noop mode when Langfuse credentials missing
    |
    v
Langfuse Cloud (us.cloud.langfuse.com)
```

### Instrumentation Pattern (StartGen/End, like xkong-agent-center)

```python
from openclaw.tracer import Tracer

tracer = Tracer.from_env()
span = tracer.start_gen("/v1/responses", session_id, user_id, messages)
# ... do HTTP call, collect response ...
span.end(output_text, usage_dict)
tracer.flush()
```

### Key Agent Metrics

- **context_pressure**: `prompt_tokens / context_limit` (how full the context window is)
- **chars_per_output_token**: output text length / completion tokens (efficiency)
- **duration_ms**: end-to-end latency

## Ports

| Service | Port |
|---------|------|
| OpenClaw Gateway | 18789 |

## Known Gaps

OpenClaw's `diagnostics-otel` plugin sends OTEL spans to Langfuse but doesn't include `gen_ai.*` semantic attributes. This means:
- Token usage works (via `openclaw.*` namespace)
- Input/Output fields are null in Langfuse UI
- Workaround: use the shared tracer module (`openclaw/tracer.py`) which wraps the Langfuse SDK

See `docs/openclaw/langfuse-otel-investigation.md` for full investigation.
