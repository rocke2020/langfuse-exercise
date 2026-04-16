"""
langfuse_proxy.py -- Langfuse-instrumented proxy for OpenClaw.

Sits between clients and OpenClaw gateway, transparently forwarding
/v1/chat/completions requests while recording proper Langfuse generations
with Input/Output, token usage, and agent-layer attributes.

         Client (web UI, curl, SDK)
              |
         This proxy (:18790)
              |  records Langfuse generation
         OpenClaw gateway (:18789)

Setup:
  1. Start:  python langfuse_proxy.py
  2. Point clients to http://127.0.0.1:18790 instead of :18789
     (or configure OpenClaw webchat to use the proxy port)

The proxy is fully transparent -- same auth, same API, same streaming.
It just adds Langfuse observability on top.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from langfuse import Langfuse
from langfuse._client.propagation import propagate_attributes
from opentelemetry import trace

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE = Path(os.path.expanduser("~/.openclaw/.env"))
CONFIG_FILE = Path(os.path.expanduser("~/.openclaw/openclaw.json"))

load_dotenv(ENV_FILE)

UPSTREAM = os.environ.get("OPENCLAW_UPSTREAM", "http://127.0.0.1:18789")
PROXY_PORT = int(os.environ.get("LANGFUSE_PROXY_PORT", "18790"))

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

app = FastAPI(title="OpenClaw Langfuse Proxy")
langfuse: Langfuse | None = None
upstream_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup():
    global langfuse, upstream_client
    langfuse = Langfuse()
    upstream_client = httpx.AsyncClient(base_url=UPSTREAM, timeout=120.0)
    print(f"[proxy] Upstream: {UPSTREAM}")
    print(f"[proxy] Langfuse: {os.environ.get('LANGFUSE_BASE_URL', 'default')}")


@app.on_event("shutdown")
async def shutdown():
    if langfuse:
        langfuse.flush()
        langfuse.shutdown()
    if upstream_client:
        await upstream_client.aclose()


# ---------------------------------------------------------------------------
# /v1/chat/completions -- the instrumented path
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    auth = request.headers.get("Authorization", "")

    messages = body.get("messages", [])
    model = body.get("model", "openclaw")
    stream = body.get("stream", False)

    # Build session/user from headers or defaults
    session_id = request.headers.get("X-Session-Id", f"proxy-{int(time.time())}")
    user_id = request.headers.get("X-User-Id", "proxy")

    if stream:
        # Streaming: forward SSE events, collect chunks, record after done
        return await _handle_streaming(
            body, auth, messages, model, session_id, user_id
        )

    # Non-streaming: simple request-response
    t0 = time.perf_counter()

    resp = await upstream_client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )

    duration_ms = (time.perf_counter() - t0) * 1000.0
    response_body = resp.json()

    if resp.status_code == 200:
        _record_generation(
            messages, model, response_body, duration_ms, session_id, user_id
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": "application/json"},
    )


async def _handle_streaming(
    body: dict,
    auth: str,
    messages: list[dict],
    model: str,
    session_id: str,
    user_id: str,
):
    """Forward SSE stream, collect chunks, record generation after stream ends."""
    t0 = time.perf_counter()
    collected_content = []
    finish_reason = None
    response_model = model
    usage = {}

    async def stream_and_collect():
        nonlocal finish_reason, response_model, usage

        async with upstream_client.stream(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": auth, "Content-Type": "application/json"},
        ) as resp:
            async for line in resp.aiter_lines():
                yield line + "\n"

                # Parse SSE data lines
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        collected_content.append(delta["content"])
                    fr = chunk.get("choices", [{}])[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                    if chunk.get("model"):
                        response_model = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

        # Stream done, record to Langfuse
        duration_ms = (time.perf_counter() - t0) * 1000.0
        full_content = "".join(collected_content)
        _record_generation(
            messages,
            model,
            _build_response(response_model, full_content, finish_reason, usage),
            duration_ms,
            session_id,
            user_id,
        )

    return StreamingResponse(
        stream_and_collect(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _build_response(
    model: str, content: str, finish_reason: str | None, usage: dict
) -> dict:
    """Build a synthetic response dict for Langfuse recording."""
    return {
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": usage,
    }


def _record_generation(
    messages: list[dict],
    model: str,
    response_body: dict,
    duration_ms: float,
    session_id: str,
    user_id: str,
):
    """Record a complete generation to Langfuse with proper Input/Output."""
    choice = response_body["choices"][0]
    output_message = choice["message"]
    usage = response_body.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    # Agent-layer metrics
    context_limit = 128000
    context_pressure = round(prompt_tokens / context_limit, 4) if prompt_tokens else 0
    output_text = output_message.get("content", "") or ""
    chars_per_token = round(
        len(output_text) / max(completion_tokens, 1), 2
    )

    with propagate_attributes(
        trace_name="openclaw.chat",
        session_id=session_id,
        user_id=user_id,
    ):
        with langfuse.start_as_current_observation(
            name="openclaw.generation",
            as_type="generation",
            input=messages,
            output=output_message,
            model=response_body.get("model", model),
            model_parameters={"max_tokens": 4096},
            usage_details={
                "input": prompt_tokens,
                "output": completion_tokens,
                "total": total_tokens,
            },
            metadata={
                "duration_ms": round(duration_ms, 1),
                "finish_reason": choice.get("finish_reason"),
                "agent.context_pressure": context_pressure,
                "agent.chars_per_output_token": chars_per_token,
                "agent.context_pressure_warning": context_pressure > 0.8,
                "agent.total_tokens": total_tokens,
                "source": "langfuse_proxy",
            },
        ):
            # Set OTel span attributes for the universal schema layer
            span = trace.get_current_span()
            span.set_attribute("gen_ai.system", "openclaw")
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.response.model", response_body.get("model", model))
            span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
            span.set_attribute("agent.context_pressure", context_pressure)
            span.set_attribute("agent.duration_ms", round(duration_ms, 1))

    langfuse.flush()


# ---------------------------------------------------------------------------
# Passthrough for all other endpoints
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def passthrough(request: Request, path: str):
    """Forward everything else to upstream unchanged."""
    body = await request.body()
    resp = await upstream_client.request(
        method=request.method,
        url=f"/{path}",
        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        content=body,
        params=dict(request.query_params),
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[proxy] Starting on port {PROXY_PORT}")
    print(f"[proxy] Forwarding to {UPSTREAM}")
    print(f"[proxy] /v1/chat/completions -> instrumented with Langfuse")
    print(f"[proxy] All other routes -> passthrough")
    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")
