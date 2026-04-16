"""Test OpenClaw gateway via OpenResponses HTTP API with Langfuse tracing.

Sends chat requests to the local OpenClaw gateway (/v1/responses),
instruments each call with the Langfuse SDK so traces appear in the
Langfuse UI with proper Input/Output and metadata.

Usage:
    conda run -n performance --no-capture-output python test/openclaw/test_chat.py
"""

from __future__ import annotations

import json
import time

import requests
from dotenv import load_dotenv
from langfuse import Langfuse
from langfuse._client.propagation import propagate_attributes

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_URL = "http://127.0.0.1:18789/v1/responses"
GATEWAY_TOKEN = "226a8aff8ff2dd9cdebee4f29ad1239e4adc488a1eac3990"

HEADERS = {
    "Authorization": f"Bearer {GATEWAY_TOKEN}",
    "Content-Type": "application/json",
    "x-openclaw-agent-id": "main",
}

langfuse = Langfuse()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_output_text(data: dict) -> str:
    """Extract assistant text from a /v1/responses non-streaming response."""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
    return json.dumps(data, indent=2)


def _extract_stream_text(resp: requests.Response) -> str:
    """Extract full text from a /v1/responses streaming response."""
    full_text = ""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() == "[DONE]":
            break
        try:
            event = json.loads(data_str)
            delta = event.get("delta")
            if isinstance(delta, dict):
                full_text += delta.get("text", "")
            elif isinstance(delta, str):
                full_text += delta
        except json.JSONDecodeError:
            continue
    return full_text


def _record_generation(
    input_msg: str,
    output_text: str,
    model: str,
    duration_ms: float,
    session_id: str,
):
    """Record a generation to Langfuse following the proxy pattern."""
    messages = [{"role": "user", "content": input_msg}]
    output_message = {"role": "assistant", "content": output_text}

    with propagate_attributes(
        trace_name="openclaw.chat",
        session_id=session_id,
        user_id="test-script",
    ):
        with langfuse.start_as_current_observation(
            name="openclaw.generation",
            as_type="generation",
            input=messages,
            output=output_message,
            model=model,
            model_parameters={"max_tokens": 4096},
            metadata={
                "duration_ms": round(duration_ms, 1),
                "finish_reason": "stop",
                "source": "test_chat.py",
                "gateway": GATEWAY_URL,
            },
        ):
            pass  # observation recorded on context exit

    langfuse.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def send_chat(message: str, stream: bool = False) -> str:
    """Send a chat message to the OpenClaw gateway and return the response."""
    session_id = f"test-{int(time.time())}"
    payload = {
        "model": "openclaw",
        "input": message,
        "stream": stream,
    }

    t0 = time.perf_counter()

    if stream:
        resp = requests.post(GATEWAY_URL, headers=HEADERS, json=payload, stream=True)
        resp.raise_for_status()
        result = _extract_stream_text(resp)
    else:
        resp = requests.post(GATEWAY_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
        result = _extract_output_text(data)

    duration_ms = (time.perf_counter() - t0) * 1000.0

    _record_generation(
        input_msg=message,
        output_text=result,
        model="openclaw",
        duration_ms=duration_ms,
        session_id=session_id,
    )

    return result


if __name__ == "__main__":
    print("=== OpenClaw Gateway Chat Test (with Langfuse tracing) ===\n")

    # Non-streaming request
    print("[Non-streaming] Sending: 'What is 2 + 3?'")
    result = send_chat("What is 2 + 3?")
    print(f"[Non-streaming] Response: {result}\n")

    # Streaming request
    print("[Streaming] Sending: 'Tell me a one-sentence joke.'")
    result_stream = send_chat("Tell me a one-sentence joke.", stream=True)
    print(f"[Streaming] Response: {result_stream}\n")

    langfuse.flush()
    print("=== Done. Traces sent to Langfuse. ===")
