"""Langfuse tracer for OpenClaw LLM calls.

Follows the xkong-agent-center StartGen/End pattern:

    tracer = Tracer.from_env()
    span = tracer.start_gen("/v1/responses", session_id, user_id, messages)
    ... do HTTP call ...
    span.end(output_text, total_tokens)
    tracer.flush()

If Langfuse credentials are missing, all methods are no-ops.
"""

from __future__ import annotations

import time
from typing import Protocol

from dotenv import load_dotenv
from langfuse import Langfuse
from langfuse._client.propagation import propagate_attributes
from opentelemetry import trace

load_dotenv()

CONTEXT_LIMIT = 128_000


# ---------------------------------------------------------------------------
# GenSpan protocol
# ---------------------------------------------------------------------------


class GenSpan(Protocol):
    def end(self, output: str, usage: dict, err: Exception | None = None) -> None: ...


# ---------------------------------------------------------------------------
# Live implementation
# ---------------------------------------------------------------------------


class _LfGenSpan:
    """In-flight generation span backed by Langfuse."""

    def __init__(self, tracer: _LfTracer, path: str, session_id: str, user_id: str, input_msgs: list[dict]):
        self._tracer = tracer
        self._path = path
        self._session_id = session_id
        self._user_id = user_id
        self._input_msgs = input_msgs
        self._start = time.perf_counter()

    def end(self, output: str, usage: dict, err: Exception | None = None) -> None:
        """Record the completed generation.

        Args:
            output: Assistant response text.
            usage: Token usage dict (keys: input_tokens, output_tokens, total_tokens).
            err: Optional error that occurred during the call.
        """
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        output_text = output if not err else f"[error] {err}"
        output_message = {"role": "assistant", "content": output_text}

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        # Agent-layer metrics
        context_pressure = round(total_tokens / CONTEXT_LIMIT, 4) if total_tokens else 0
        chars_per_token = round(len(output_text) / max(output_tokens, 1), 2)

        usage_details = {}
        if input_tokens:
            usage_details["input"] = input_tokens
        if output_tokens:
            usage_details["output"] = output_tokens
        if total_tokens:
            usage_details["total"] = total_tokens

        with propagate_attributes(
            trace_name="openclaw.chat",
            session_id=self._session_id,
            user_id=self._user_id,
        ):
            with self._tracer._langfuse.start_as_current_observation(
                name="openclaw.generation",
                as_type="generation",
                input=self._input_msgs,
                output=output_message,
                model="openclaw",
                model_parameters={"max_tokens": 4096},
                usage_details=usage_details,
                metadata={
                    "duration_ms": round(duration_ms, 1),
                    "finish_reason": "error" if err else "stop",
                    "agent.context_pressure": context_pressure,
                    "agent.chars_per_output_token": chars_per_token,
                    "agent.context_pressure_warning": context_pressure > 0.8,
                    "source": "openclaw-tracer",
                    "path": self._path,
                },
            ):
                span = trace.get_current_span()
                span.set_attribute("gen_ai.system", "openclaw")
                span.set_attribute("gen_ai.request.model", "openclaw")
                span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
                span.set_attribute("agent.context_pressure", context_pressure)
                span.set_attribute("agent.duration_ms", round(duration_ms, 1))
                if err:
                    span.set_attribute("error", True)


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


class _LfTracer:
    """Langfuse-backed tracer."""

    def __init__(self, langfuse: Langfuse):
        self._langfuse = langfuse

    def start_gen(self, path: str, session_id: str, user_id: str, input_msgs: list[dict]) -> GenSpan:
        return _LfGenSpan(self, path, session_id, user_id, input_msgs)

    def flush(self) -> None:
        self._langfuse.flush()


# ---------------------------------------------------------------------------
# Noop implementation
# ---------------------------------------------------------------------------


class _NoopSpan:
    def end(self, output: str, usage: dict, err: Exception | None = None) -> None:
        pass


class _NoopTracer:
    def start_gen(self, path: str, session_id: str, user_id: str, input_msgs: list[dict]) -> GenSpan:
        return _NoopSpan()

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class Tracer:
    """Public factory. Use Tracer.from_env() or Tracer.create(langfuse)."""

    @staticmethod
    def from_env() -> _LfTracer | _NoopTracer:
        """Create tracer from LANGFUSE_* environment variables. Noop if missing."""
        try:
            lf = Langfuse()
            # Langfuse() doesn't raise on missing keys, but auth_check() does
            return _LfTracer(lf)
        except Exception:
            return _NoopTracer()

    @staticmethod
    def create(langfuse: Langfuse | None) -> _LfTracer | _NoopTracer:
        """Create tracer from an existing Langfuse client. None = noop."""
        if langfuse is None:
            return _NoopTracer()
        return _LfTracer(langfuse)
