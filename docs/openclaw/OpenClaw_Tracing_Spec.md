# OpenClaw Tracing Spec (OpenTelemetry + Langfuse Strategy)

## 1. Core Idea
Do NOT build another Langfuse.  
Extend it with Agent Observability.

---

## 2. Trace Model

One task = one trace

TRACE (task_id)
 ├── agent.run
 │    ├── agent.step
 │    ├── llm.call
 │    ├── tool.call
 │    ├── guard.check
 │    └── retry / abort

---

## 3. Span Types

- agent.run
- agent.step
- agent.decision
- llm.call
- tool.call
- tool.result
- memory.read
- memory.write
- guard.check
- retry
- abort

---

## 4. Global Attributes

{
  "trace_id": "...",
  "task_id": "...",
  "agent_id": "...",
  "session_id": "...",
  "goal": "...",
  "status": "success | failed | aborted"
}

---

## 5. Agent Step Schema

{
  "span_type": "agent.step",
  "step_index": 7,
  "thought": "...",
  "action": "tool.call | llm.call",
  "confidence": 0.72,
  "decision_latency_ms": 420
}

---

## 6. LLM Call Schema

{
  "span_type": "llm.call",
  "model": "gpt-4o",
  "input_tokens": 1200,
  "output_tokens": 800,
  "cost_usd": 0.023,
  "latency_ms": 1800,
  "success": true
}

---

## 7. Tool Call Schema

{
  "span_type": "tool.call",
  "tool_name": "bash",
  "command": "cargo test",
  "exit_code": 1,
  "duration_ms": 2300,
  "success": false
}

---

## 8. Failure Attribution

{
  "failure_type": "tool_error | llm_error | reasoning_error",
  "root_cause": "...",
  "is_recoverable": true
}

---

## 9. Waste Metrics

{
  "wasted_tokens": 3200,
  "loop_count": 4,
  "duplicate_calls": 3,
  "no_progress_steps": 2
}

---

## 10. Architecture

OpenClaw → OpenTelemetry → Langfuse → Agent Analyzer → Grafana

---

## 11. Langfuse Comparison

Langfuse:
- ✔ LLM tracing
- ✔ tokens
- ✔ prompt logging

Missing:
- ❌ agent loops
- ❌ waste detection
- ❌ state transitions

---

## 12. Final Strategy

Use Langfuse for LLM layer  
Build custom Agent Analyzer for:

- failure attribution
- loop detection
- token waste

---

## 13. Key Takeaway

You are building:

Agent Observability Platform (AOP)

NOT logging.
