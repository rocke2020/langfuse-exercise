# OpenClaw Tracing Spec (OpenTelemetry + Langfuse Strategy)

## 1. Core Idea

Do NOT build another Langfuse.
Extend it with Agent Observability.

## 2. Trace Model

One task = one trace, with nested spans for each step the agent takes.

```
TRACE (task_id)
 ├── agent.run
 │    ├── agent.step          (reasoning, decision)
 │    ├── llm.call            (model invocation)
 │    ├── tool.call           (bash, file edit, web search)
 │    ├── guard.check         (safety, permission)
 │    └── retry / abort       (error recovery or bail)
```

## 3. Span Types

| Span Type | What It Captures |
|-----------|-----------------|
| `agent.run` | Full task execution lifecycle |
| `agent.step` | Single reasoning + action cycle |
| `agent.decision` | Branch point where agent chose between options |
| `llm.call` | Model invocation with input/output/tokens |
| `tool.call` | External tool execution (bash, API, file) |
| `tool.result` | Tool output and exit status |
| `memory.read` | Context retrieval from memory/vector store |
| `memory.write` | Persisting information for future use |
| `guard.check` | Safety or permission validation |
| `retry` | Retrying a failed operation |
| `abort` | Agent giving up on a task or subtask |

## 4. Global Attributes

Every span carries these for correlation:

```json
{
  "trace_id": "...",
  "task_id": "...",
  "agent_id": "...",
  "session_id": "...",
  "goal": "user's original request",
  "status": "success | failed | aborted"
}
```

## 5. Agent Step Schema

```json
{
  "span_type": "agent.step",
  "step_index": 7,
  "thought": "Need to check if the file exists before editing",
  "action": "tool.call | llm.call",
  "confidence": 0.72,
  "decision_latency_ms": 420
}
```

## 6. LLM Call Schema

```json
{
  "span_type": "llm.call",
  "model": "gpt-5-mini",
  "input_tokens": 1200,
  "output_tokens": 800,
  "cost_usd": 0.023,
  "latency_ms": 1800,
  "success": true
}
```

## 7. Tool Call Schema

```json
{
  "span_type": "tool.call",
  "tool_name": "bash",
  "command": "cargo test",
  "exit_code": 1,
  "duration_ms": 2300,
  "success": false
}
```

## 8. Failure Attribution

When something goes wrong, classify the root cause:

```json
{
  "failure_type": "tool_error | llm_error | reasoning_error",
  "root_cause": "bash command returned exit code 1, test assertion failed",
  "is_recoverable": true
}
```

## 9. Waste Metrics

Track inefficiency to measure agent quality:

```json
{
  "wasted_tokens": 3200,
  "loop_count": 4,
  "duplicate_calls": 3,
  "no_progress_steps": 2
}
```

These metrics power the "was this agent run efficient?" question. High `loop_count` with low progress signals the agent is stuck. High `duplicate_calls` means the agent forgot it already tried something.

## 10. Architecture

```
OpenClaw Gateway (localhost:18789)
    |
    | /v1/responses or /v1/chat/completions
    v
Client code with shared tracer (openclaw/tracer.py)
    |
    | StartGen/End pattern records Input/Output/tokens
    v
Langfuse Cloud (us.cloud.langfuse.com)
    |
    | LLM layer: traces, generations, token usage
    v
Agent Analyzer (future)
    |
    | Agent layer: loops, waste, failure attribution
    v
Grafana dashboards
```

**Current state:** The Langfuse layer works end-to-end. The Agent Analyzer layer is the next step.

## 11. What Langfuse Covers vs. What's Missing

| Capability | Langfuse | Agent Analyzer (future) |
|------------|----------|------------------------|
| LLM tracing (input/output) | Yes | - |
| Token usage and cost | Yes | - |
| Prompt logging | Yes | - |
| Session tracking | Yes | - |
| Agent loop detection | - | Needed |
| Waste detection (duplicate calls, no-progress) | - | Needed |
| State transition tracking | - | Needed |
| Failure root cause classification | - | Needed |

## 12. Implementation Status

**Done:**
- OpenClaw OTel plugin sends spans to Langfuse (token usage, session tracking)
- Shared tracer module (`openclaw/tracer.py`) fills the Input/Output gap
- Integration test (`test/openclaw/test_chat.py`) verifies end-to-end tracing
- `openclaw_observe.py` provides chat, watch, and analyze modes

**Gap:**
- OpenClaw's built-in OTel plugin doesn't emit `gen_ai.*` semantic attributes (see `langfuse-otel-investigation.md`)
- HTTP API calls don't generate OTel spans (only WebSocket chat does)
- Workaround: client-side tracer adds Langfuse instrumentation directly

**Next:**
- Build Agent Analyzer for loop detection, waste metrics, failure attribution
- Add custom Langfuse scores for agent quality metrics
- Dashboard in Grafana for operational monitoring

## 13. Key Takeaway

This is an **Agent Observability Platform (AOP)**, not logging.

Logging tells you what happened. Observability tells you why the agent wasted 3200 tokens going in circles, which tool call caused the failure chain, and whether the session was stuck because of a reasoning error or a network timeout.
