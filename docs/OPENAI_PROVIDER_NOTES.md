# OpenAI Provider Notes (Phase 10)

## Why OpenAI

The Techno-AI-Router integration (Phase 7) could never be used live: its API key was not
obtainable. For the release candidate the reasoning provider is **direct OpenAI API access**.
The router adapter stays in the repository, deprecated, and is never called unless
`REASONING_PROVIDER=ai_router` is set explicitly.

```text
LeadAnalysisService → ReasoningProvider (interface) → OpenAIAdapter → official openai SDK
                                                                     → Responses API
```

The analysis domain, prompt, grounding/catalog validation and eligibility gate are unchanged.

## Contract

| | |
|---|---|
| Provider | OpenAI |
| Role | lead analysis / offer-angle generation |
| Credential | `OPENAI_API_KEY` (secret, `.env` or the deployment secret store) |
| Transport | HTTPS, backend only (the browser never sees the key) |
| SDK | official `openai` Python package (2.x), `AsyncOpenAI`, SDK retries disabled |
| API | Responses API — `client.responses.create(...)`, `store=False`, no tools, no web search |
| Structured output | `text.format` = `{type: "json_schema", name: "lead_analysis", strict: true, schema}` |
| Code | `app/providers/openai/{adapter,schema,mock}.py` |

### Structured output

The project's own Pydantic model `LeadAnalysisOutput` remains the authority. Its JSON Schema is
passed on `ChatRequest.response_schema`; the adapter adapts it to OpenAI strict mode
(`app/providers/openai/schema.py`): every object lists all properties as `required` and sets
`additionalProperties: false`; value constraints (lengths, ranges) are moved into the field
description. The answer is then validated again by the domain (`parse_output` +
`validate_output`): unknown evidence ids, offers outside the catalog, hypotheses restated as
facts etc. are rejected exactly as before.

### Model

- Configured with `OPENAI_MODEL` (default **`gpt-6-luna`**); no code change needed to switch.
- Verified on 2026-10-05: listed by `GET /v1/models` for this key (127 models visible),
  `models.retrieve` succeeds, strict JSON-schema output accepted, analysis valid on the first
  attempt. Chosen because it is a current low-cost model ($0.10 in / $0.50 out per 1M tokens on
  the official pricing page that day); a full lead analysis costs about $0.001.
- Before the first generation of every process the adapter calls `models.retrieve(model)`
  (free). An unknown model stops the run with `provider_model_unavailable` — nothing is spent.
- Reasoning models: leave `OPENAI_REASONING_EFFORT` empty (model default) or set
  `minimal|low|medium|high`. `temperature` is not sent.

### Pricing and budgets

There is no price API, so cost is **estimated**: tokens reported by OpenAI × a price.

| Setting | Default | Meaning |
|---|---|---|
| `OPENAI_PRICE_INPUT_PER_1M_USD` / `OPENAI_PRICE_OUTPUT_PER_1M_USD` | empty | empty = built-in table for known models (`KNOWN_PRICES` in the adapter, checked 2026-10-05). A model that is not in the table **and** has no price here is refused — spend must be budgetable |
| `OPENAI_MAX_TOKENS` | 2500 | `max_output_tokens` per call |
| `OPENAI_MAX_TOKENS_PER_RUN` | 60000 | tokens per analysis run |
| `OPENAI_MAX_ESTIMATED_COST_USD_PER_RUN` | 0.25 | per analysis run; also the ceiling for the worst case of a single call (checked **before** the call) |
| `OPENAI_MAX_ESTIMATED_COST_USD_PER_MONTH` | 5.00 | month-to-date, from stored analyses |
| `PIPELINE_MAX_ESTIMATED_COST_USD_PER_RUN` | 1.00 | all stages of one pipeline run |

Update `KNOWN_PRICES` (or set the two price variables) when OpenAI changes prices. The OpenAI
usage dashboard is the authority for what was actually billed.

### Safety

A generation happens only when **all** are true for that process: `PROVIDER_MODE=live`,
`ALLOW_PAID_PROVIDER_CALLS=true`, key present, price known, worst-case cost within budget,
model exists. Otherwise the call is refused before any request. The safe baseline in `.env`
stays `mock` / `false`.

### Errors (mapped to the project's typed errors)

| OpenAI | Project error | Retried |
|---|---|---|
| 401 / 403 | `provider_not_configured` (stops the whole analysis run) | no |
| 404 / `model_not_found` / 400 on `model` | `provider_model_unavailable` | no |
| 429 rate limit | `provider_rate_limited` | yes |
| 429 `insufficient_quota` | `provider_quota_exhausted` | no |
| 5xx | `provider_unavailable` | yes |
| timeout / connection | `provider_timeout` / `provider_unavailable` | yes |
| refusal content | `provider_refused` | no |
| `status: incomplete` | `provider_incomplete_response` | no |
| empty / non-JSON / unexpected | `provider_malformed_response` | no |
| over budget (pre-check) | `provider_budget_exceeded` | no |

Retries: at most `OPENAI_MAX_RETRIES` (2), backoff 2 s, 4 s (max 16 s). A billed but unusable
answer (refusal, incomplete) is stored as a FAILED analysis **with** its token usage and cost,
so budgets stay honest.

### What is stored and logged

Every analysis row: `provider = openai`, `gateway = openai`, the exact `model` returned by the
API, token usage, estimated cost, latency, attempts, policy/prompt/catalog versions.
Logs contain provider, operation, model, status, duration, token total and estimated cost —
never the key, the prompt or the model's text.

Historical records produced through the router (or mocks) are **not** rewritten; they keep
their original provider/gateway values.

## Setting it up (the only manual steps)

1. Create an API key in the OpenAI dashboard (API keys page). Prefer a **project key** with
   only model/Responses access; set a monthly budget limit on the project in the dashboard.
2. Put it in `.env`: `OPENAI_API_KEY=<key>`. Never paste it into chat, code or documents.
3. Check (free): `uv run python scripts/check_openai.py`
4. Smallest paid test (about $0.001):
   ```powershell
   $env:PROVIDER_MODE="live"; $env:ALLOW_PAID_PROVIDER_CALLS="true"
   uv run python scripts/check_openai.py --chat --analysis-sample
   Remove-Item Env:PROVIDER_MODE; Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS
   ```

**Rotation:** create a new key in the dashboard → replace the value in `.env` →
`docker compose up -d app scheduler` → run step 3 → delete the old key in the dashboard.
Rotate immediately if the key was ever pasted somewhere or a laptop is lost.

## Sources checked (2026-10-05)

- developers.openai.com/api/docs/guides/structured-outputs — Responses API `text.format`
  json_schema, strict-mode schema rules, refusal content, `status: incomplete`.
- developers.openai.com/api/docs/pricing — standard-tier token prices.
- Live `GET /v1/models` with the project key — model availability.
- A real `responses.create` call with the strict schema (see PHASE_10_COMPLETION §8).
