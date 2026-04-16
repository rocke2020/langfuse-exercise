"""Test OpenClaw gateway via OpenResponses HTTP API with Langfuse tracing.

Sends chat requests to the local OpenClaw gateway (/v1/responses),
instruments each call with the shared tracer module (StartGen/End pattern).

Usage:
    conda run -n performance --no-capture-output python test/openclaw/test_chat.py
"""

from __future__ import annotations

import json
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, ".")
from openclaw.tracer import Tracer

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

tracer = Tracer.from_env()

# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _extract_response(data: dict) -> tuple[str, dict]:
    """Extract (text, usage) from a /v1/responses non-streaming response."""
    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
    usage = data.get("usage", {})
    return text or json.dumps(data, indent=2), usage


def _extract_stream_response(resp: requests.Response) -> tuple[str, dict]:
    """Extract (text, usage) from a /v1/responses streaming response."""
    full_text = ""
    usage = {}
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
            if event.get("usage"):
                usage = event["usage"]
            elif event.get("response", {}).get("usage"):
                usage = event["response"]["usage"]
        except json.JSONDecodeError:
            continue
    return full_text, usage


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def send_chat(message: str, stream: bool = False) -> str:
    """Send a chat message to the OpenClaw gateway and return the response."""
    session_id = f"test-{int(time.time())}"
    messages = [{"role": "user", "content": message}]

    span = tracer.start_gen("/v1/responses", session_id, "test-script", messages)

    payload = {
        "model": "openclaw",
        "input": message,
        "stream": stream,
    }

    try:
        if stream:
            resp = requests.post(GATEWAY_URL, headers=HEADERS, json=payload, stream=True)
            resp.raise_for_status()
            result, usage = _extract_stream_response(resp)
        else:
            resp = requests.post(GATEWAY_URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            result, usage = _extract_response(resp.json())

        span.end(result, usage)
    except Exception as e:
        span.end("", {}, err=e)
        raise

    tracer.flush()
    return result


if __name__ == "__main__":
    print("=== OpenClaw Gateway Chat Test (with Langfuse tracing) ===\n")

    print("[Non-streaming] Sending: 'What is 2 + 3?'")
    result = send_chat("What is 2 + 3?")
    print(f"[Non-streaming] Response: {result}\n")

    print("[Streaming] Sending: 'Tell me a one-sentence joke.'")
    result_stream = send_chat("Tell me a one-sentence joke.", stream=True)
    print(f"[Streaming] Response: {result_stream}\n")

    tracer.flush()
    print("=== Done. Traces sent to Langfuse. ===")
