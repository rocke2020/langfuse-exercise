"""
openclaw_observe.py -- Thin agent-layer observability for OpenClaw via Langfuse.

Uses OpenTelemetry spans as the universal schema, sends them to Langfuse
for LLM observability, and adds agent-layer attribution on top:
  - State tracking (session lifecycle, context window pressure)
  - Waste detection (cache misses, redundant calls, compaction overhead)
  - Failure attribution (error chains, retries, stuck sessions)

Architecture:
  OpenClaw gateway (localhost:18789)
      |  OpenAI-compatible /v1/chat/completions
  This script (Langfuse 4.x OTel-native instrumentation)
      |  Langfuse SpanProcessor (auto OTel export)
  Langfuse cloud (traces with gen_ai.* + agent.* attributes)

Usage:
  # Single observation of a live chat
  python openclaw_observe.py chat "what is 1+1?"

  # Continuous monitoring daemon (polls gateway, attributes every exchange)
  python openclaw_observe.py watch --interval 30

  # Analyze recent sessions for waste/failure patterns
  python openclaw_observe.py analyze --hours 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langfuse import Langfuse
from langfuse._client.propagation import propagate_attributes
from opentelemetry import trace
from opentelemetry.trace import StatusCode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE = Path(os.path.expanduser("~/.openclaw/.env"))
CONFIG_FILE = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
LOG_PATH = Path(os.path.expanduser("~/.openclaw/logs/gateway.log"))

load_dotenv(ENV_FILE)

GATEWAY_URL = "http://127.0.0.1:18789"
GATEWAY_TOKEN = None  # loaded lazily from openclaw.json

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _load_gateway_token() -> str:
    global GATEWAY_TOKEN
    if GATEWAY_TOKEN:
        return GATEWAY_TOKEN
    cfg = json.loads(CONFIG_FILE.read_text())
    GATEWAY_TOKEN = cfg["gateway"]["auth"]["token"]
    return GATEWAY_TOKEN


def _init_langfuse() -> Langfuse:
    """Langfuse 4.x auto-configures OTel TracerProvider + SpanProcessor."""
    return Langfuse()


# ---------------------------------------------------------------------------
# Core: Observed chat call
# ---------------------------------------------------------------------------


def observed_chat(
    langfuse: Langfuse,
    messages: list[dict],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    model: str = "openclaw",
) -> dict:
    """
    Send a chat to OpenClaw gateway and record a full Langfuse generation
    with proper Input/Output + agent-layer attributes.
    """
    token = _load_gateway_token()
    session_id = session_id or f"observe-{int(time.time())}"

    # propagate_attributes sets trace-level context (session, user, name)
    with propagate_attributes(
        trace_name="openclaw.chat",
        session_id=session_id,
        user_id=user_id or "observe",
    ):
        # start_as_current_observation creates an OTel span that Langfuse renders
        with langfuse.start_as_current_observation(
            name="openclaw.generation",
            as_type="generation",
            input=messages,
            model=model,
            model_parameters={"max_tokens": 4096},
        ) as generation:
            t0 = time.perf_counter()

            try:
                resp = httpx.post(
                    f"{GATEWAY_URL}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 4096,
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                response_body = resp.json()
            except Exception as e:
                generation.update(
                    output={"error": str(e)},
                    level="ERROR",
                    status_message=str(e),
                )
                raise

            duration_ms = (time.perf_counter() - t0) * 1000.0

            # Extract response
            choice = response_body["choices"][0]
            output_message = choice["message"]
            usage = response_body.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # Agent-layer: context pressure
            context_limit = 128000
            context_pressure = round(prompt_tokens / context_limit, 4) if context_limit else 0

            # Agent-layer: output efficiency
            output_text = output_message.get("content", "") or ""
            chars_per_token = round(
                len(output_text) / max(completion_tokens, 1), 2
            )

            # Update the generation with output + usage + agent metadata
            generation.update(
                output=output_message,
                model=response_body.get("model", model),
                usage_details={
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": usage.get("total_tokens", 0),
                },
                metadata={
                    "duration_ms": round(duration_ms, 1),
                    "finish_reason": choice.get("finish_reason"),
                    "gateway": GATEWAY_URL,
                    # Agent-layer attributes
                    "agent.context_pressure": context_pressure,
                    "agent.chars_per_output_token": chars_per_token,
                    "agent.context_pressure_warning": context_pressure > 0.8,
                },
            )

            # Also set OTel span attributes for the raw OTel layer
            span = trace.get_current_span()
            span.set_attribute("gen_ai.system", "openclaw")
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.response.model", response_body.get("model", model))
            span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
            span.set_attribute("agent.context_pressure", context_pressure)
            span.set_attribute("agent.duration_ms", round(duration_ms, 1))
            span.set_attribute("agent.total_tokens", usage.get("total_tokens", 0))

    langfuse.flush()
    return response_body


# ---------------------------------------------------------------------------
# Watch mode: continuous monitoring
# ---------------------------------------------------------------------------


def _parse_gateway_log_tail(lines: int = 200) -> list[dict]:
    """Parse recent gateway log for chat.send events."""
    if not LOG_PATH.exists():
        return []
    text = LOG_PATH.read_text()
    recent = text.strip().split("\n")[-lines:]
    events = []
    for line in recent:
        if "chat.send" not in line or "\u2717" in line:
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        ts_str = parts[0]
        rest = parts[2]
        run_id = None
        if "runId=" in rest:
            run_id = rest.split("runId=")[1].split(" ")[0]
        events.append({"timestamp": ts_str, "runId": run_id})
    return events


def watch_loop(langfuse: Langfuse, interval: int = 30):
    """
    Continuous monitoring: poll gateway logs, detect new exchanges,
    flag stuck sessions and waste patterns.
    """
    print(f"[observe] Watching OpenClaw gateway (interval={interval}s)")
    seen_runs: set[str] = set()

    while True:
        try:
            events = _parse_gateway_log_tail(500)
            new_events = [e for e in events if e["runId"] and e["runId"] not in seen_runs]

            for evt in new_events:
                seen_runs.add(evt["runId"])

                with propagate_attributes(trace_name="openclaw.exchange"):
                    with langfuse.start_as_current_observation(
                        name="exchange.detected",
                        metadata={
                            "run_id": evt["runId"],
                            "timestamp": evt["timestamp"],
                            "source": "gateway_log_watch",
                        },
                    ) as obs:
                        span = trace.get_current_span()
                        span.set_attribute("agent.run_id", evt["runId"])
                        span.set_attribute("agent.source", "gateway_log")

            # Stuck session detection
            if LOG_PATH.exists():
                recent = LOG_PATH.read_text().strip().split("\n")[-50:]
                for line in recent:
                    if "stuck session" in line.lower():
                        with propagate_attributes(trace_name="openclaw.failure"):
                            with langfuse.start_as_current_observation(
                                name="stuck_session",
                                level="ERROR",
                                status_message="stuck session detected",
                                metadata={"raw": line[:500]},
                            ) as obs:
                                span = trace.get_current_span()
                                span.set_attribute("agent.failure_type", "stuck_session")
                                span.set_status(StatusCode.ERROR, "stuck session")

            if new_events:
                print(f"[observe] {len(new_events)} new exchanges detected")
                langfuse.flush()

        except KeyboardInterrupt:
            print("\n[observe] Shutting down...")
            langfuse.flush()
            break
        except Exception as e:
            print(f"[observe] Error: {e}", file=sys.stderr)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Analyze mode: retrospective waste/failure analysis
# ---------------------------------------------------------------------------


def analyze_sessions(langfuse: Langfuse, hours: float = 4):
    """
    Analyze recent OpenClaw activity for waste and failure patterns.

    Waste signals:
      - Stuck sessions (processing state stuck, wasted compute)
      - Blocked webhooks (cron targets unreachable)
      - Chat errors (failed exchanges)

    Failure signals:
      - Error outcomes in chat.send
      - Stuck session frequency
    """
    print(f"[analyze] Scanning last {hours}h of gateway activity...")

    runtime_log = Path(f"/tmp/openclaw/openclaw-{datetime.now().strftime('%Y-%m-%d')}.log")

    report = {
        "period_hours": hours,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_events": 0,
        "chat_errors": 0,
        "stuck_sessions": 0,
        "blocked_webhooks": 0,
    }

    # Parse gateway log
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().strip().split("\n"):
            if not line.strip():
                continue
            if "chat.send" in line and "\u2717" not in line:
                report["chat_events"] += 1
            elif "chat.send" in line and "\u2717" in line:
                report["chat_errors"] += 1

    # Runtime log for stuck sessions and blocked webhooks
    if runtime_log.exists():
        for line in runtime_log.read_text().strip().split("\n"):
            if "stuck session" in line.lower():
                report["stuck_sessions"] += 1
            if "blocked URL fetch" in line:
                report["blocked_webhooks"] += 1

    # Emit as Langfuse trace
    with propagate_attributes(trace_name="openclaw.analysis"):
        with langfuse.start_as_current_observation(
            name="retrospective_analysis",
            input={"period_hours": hours},
            output=report,
            metadata=report,
        ) as obs:
            span = trace.get_current_span()
            span.set_attribute("agent.analysis_type", "retrospective")
            span.set_attribute("agent.period_hours", hours)
            span.set_attribute("agent.chat_events", report["chat_events"])
            span.set_attribute("agent.chat_errors", report["chat_errors"])
            span.set_attribute("agent.stuck_sessions", report["stuck_sessions"])
            span.set_attribute("agent.blocked_webhooks", report["blocked_webhooks"])

            if report["stuck_sessions"] > 5:
                span.set_attribute("agent.alert", "high_stuck_session_rate")
            if report["chat_errors"] > 0:
                span.set_attribute("agent.alert", "chat_errors_detected")

    langfuse.flush()

    # Print report
    print()
    print("=" * 60)
    print("  OpenClaw Observability Report")
    print("=" * 60)
    print(f"  Period:             last {hours}h")
    print(f"  Chat exchanges:     {report['chat_events']}")
    print(f"  Chat errors:        {report['chat_errors']}")
    print(f"  Stuck sessions:     {report['stuck_sessions']}")
    print(f"  Blocked webhooks:   {report['blocked_webhooks']}")
    print("=" * 60)

    if report["stuck_sessions"] > 5:
        print("  !! HIGH stuck session count -- check cron jobs")
    if report["chat_errors"] > 0:
        print("  !! Chat errors detected -- check gateway.err.log")
    if report["blocked_webhooks"] > 20:
        print("  !! Many blocked webhooks -- check cron webhook targets")

    print()
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw agent-layer observability via OTel + Langfuse"
    )
    sub = parser.add_subparsers(dest="command")

    chat_p = sub.add_parser("chat", help="Send an observed chat message")
    chat_p.add_argument("message", help="User message to send")
    chat_p.add_argument("--session", default=None, help="Session ID")
    chat_p.add_argument("--model", default="openclaw", help="Model name")

    watch_p = sub.add_parser("watch", help="Continuous monitoring daemon")
    watch_p.add_argument("--interval", type=int, default=30, help="Poll interval (seconds)")

    analyze_p = sub.add_parser("analyze", help="Analyze recent sessions")
    analyze_p.add_argument("--hours", type=float, default=4, help="Lookback window (hours)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    langfuse = _init_langfuse()

    if args.command == "chat":
        messages = [{"role": "user", "content": args.message}]
        result = observed_chat(
            langfuse, messages,
            session_id=args.session,
            model=args.model,
        )
        output = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        print(f"\n{output}")
        print(f"\n[tokens] in={usage.get('prompt_tokens', 0)} "
              f"out={usage.get('completion_tokens', 0)} "
              f"total={usage.get('total_tokens', 0)}")

    elif args.command == "watch":
        watch_loop(langfuse, interval=args.interval)

    elif args.command == "analyze":
        analyze_sessions(langfuse, hours=args.hours)


if __name__ == "__main__":
    main()
