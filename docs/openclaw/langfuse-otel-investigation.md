# Langfuse + OpenClaw OTel Integration: Investigation Summary

**Date:** 2026-04-16
**OpenClaw version:** v2026.4.14
**Langfuse:** US Cloud (us.cloud.langfuse.com), Hobby plan, project "quick-start"
**Plugin:** `diagnostics-otel` (bundled with OpenClaw)

## Setup

OpenClaw sends OTel spans to Langfuse via the `diagnostics-otel` plugin, configured in `~/.openclaw/openclaw.json`:

```json
"diagnostics": {
  "enabled": true,
  "otel": {
    "enabled": true,
    "endpoint": "https://us.cloud.langfuse.com/api/public/otel",
    "protocol": "http/protobuf",
    "serviceName": "openclaw-rocke-local",
    "headers": { "Authorization": "Basic <base64(publicKey:secretKey)>" },
    "traces": true,
    "metrics": true,
    "sampleRate": 1,
    "flushIntervalMs": 15000
  }
}
```

## Finding 1: Tracing works, but with a harmless warning

OTel data reaches Langfuse successfully. However, every flush cycle logs:

```
OTLPExportDelegate Export succeeded but could not deserialize response
RangeError: index out of range: 3 + 109 > 16
```

**Cause:** OpenClaw sends protobuf requests (`http/protobuf`), but Langfuse responds with JSON (`{"message":"OK"}`). The OTel SDK tries to decode this JSON as protobuf and fails. The key phrase is "Export **succeeded**" ... data is delivered, only the response parsing fails.

**Impact:** None. Data arrives in Langfuse correctly. The warning is cosmetic.

## Finding 2: Input/Output fields are null (the main gap)

Every trace in Langfuse shows Input = `null`, Output = `undefined`.

**Root cause:** The `diagnostics-otel` plugin only emits `openclaw.*` namespace attributes:

For `model.usage` spans:
- `openclaw.channel`, `openclaw.provider`, `openclaw.model`
- `openclaw.tokens.input`, `openclaw.tokens.output`, `openclaw.tokens.total`
- `openclaw.tokens.cache_read`, `openclaw.tokens.cache_write`
- `openclaw.sessionKey`, `openclaw.sessionId`

For `message.processed` spans:
- `openclaw.channel`, `openclaw.outcome`
- `openclaw.sessionKey`, `openclaw.messageId`

Langfuse expects [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) to populate Input/Output:
- `gen_ai.request.messages` or `gen_ai.prompt` -> Input
- `gen_ai.response.text` or `gen_ai.completion` -> Output

The plugin has **zero** references to `gen_ai.*` attributes. User messages and model responses are never included in any span.

**Plugin config schema is empty** ... no attribute mapping, no semantic convention toggle, no way to configure this from `openclaw.json`.

## Finding 3: HTTP API calls don't generate OTel spans

Calls to OpenClaw's HTTP API endpoints (`/v1/responses`, `/v1/chat/completions`) via external clients (curl, Python requests) produce correct LLM responses but do NOT generate the same OTel spans as the WebSocket chat UI. The `diagnostics-otel` plugin instruments the internal agent pipeline, not the HTTP gateway layer.

**Verified:** After sending requests via `test_chat.py` at 14:31, no new traces appeared in Langfuse for over 5 minutes. The gateway processed the requests and returned correct answers, but no spans were emitted. Only interactions through the OpenClaw chat UI (port 18789 WebSocket) generate `openclaw.model.usage` and `openclaw.message.processed` spans.

## Finding 4: Traces can appear to stop (but haven't)

During investigation, it looked like traces stopped arriving after 09:12 (2+ hour gap). This was misleading ... no `chat.send` events occurred in that window. The `session.stuck` spans continued arriving. Traces resume immediately when a new chat message is sent via the UI.

After a gateway restart (`openclaw gateway restart`), traces from the pre-restart session appeared because `sdk.shutdown()` flushes the batch buffer.

## What works via built-in OTel

- Token usage metrics (input, output, cache read/write, total)
- Model identification (provider, model name)
- Session tracking (sessionKey, sessionId)
- Message outcomes (completed, error)
- Duration measurements
- All span types: `model.usage`, `message.processed`, `session.stuck`, `webhook.*`, `queue.*`

## Workaround: Shared tracer module

Since the built-in OTel plugin doesn't emit Input/Output, we use a client-side tracer (`openclaw/tracer.py`) that wraps HTTP calls with Langfuse SDK instrumentation. This follows the same StartGen/End pattern as xkong-agent-center's `tracer.go`:

```python
from openclaw.tracer import Tracer

tracer = Tracer.from_env()
span = tracer.start_gen("/v1/responses", session_id, user_id, messages)
# ... HTTP call to OpenClaw gateway ...
span.end(output_text, usage_dict)
tracer.flush()
```

This produces traces in Langfuse with:
- Full Input/Output (user message and assistant response)
- Token usage (input_tokens, output_tokens, total_tokens)
- Agent metrics (context_pressure, chars_per_output_token)
- OTel semantic convention attributes (`gen_ai.*`)

**Verified:** Traces from the shared tracer appear in Langfuse within ~10 seconds with correct Input/Output and token counts.

## What's missing (feature request for OpenClaw)

1. **GenAI semantic convention attributes** ... Add `gen_ai.request.messages`, `gen_ai.response.text`, `gen_ai.request.model`, `gen_ai.system` to spans so Langfuse can populate Input/Output fields
2. **Span type mapping** ... Use `gen_ai.operation.name` so Langfuse categorizes spans as GENERATION type instead of generic SPAN
3. **HTTP API instrumentation** ... Emit OTel spans for `/v1/responses` and `/v1/chat/completions` HTTP endpoints, not just WebSocket interactions
4. **Optional attribute mapping config** ... Allow users to configure which attributes to emit (some may not want conversation content sent to external services)
