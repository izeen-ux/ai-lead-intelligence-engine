// Core of the enrichment sidecar (no I/O at import time; fully testable with a stubbed fetch).
//
// Routes (all JSON; POST routes need `Authorization: Bearer <ENRICHMENT_KIT_API_TOKEN>`):
//   GET  /health      config-only status - never calls Hunter/Apollo, never spends credits
//   POST /v1/enrich   enrichment-kit Waterfall (Hunter email-finder -> Apollo people/match)
//   POST /v1/verify   Hunter email-verifier (keeps HTTP 202/222 semantics upstream drops)
//   POST /v1/people   Hunter domain-search (decision-maker discovery on the verified domain)
//   POST /v1/account  Hunter account usage counters (free endpoint; for cost records only)
//
// Upstream enrichment-kit is used unmodified for the waterfall; two deliberate deviations live
// here: Apollo is called with reveal_personal_emails=false (work emails only, no extra credit),
// and the upstream cache is disabled (the Python app owns the cache in PostgreSQL).
// Secrets never leave this process and are never logged; request bodies are never logged.
import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { timingSafeEqual } from 'node:crypto';
import { parseEnv } from 'node:util';

export const ALLOWED_ENV = [
  'HUNTER_API_KEY',
  'HUNTER_API_KEY_BACKUPS',
  'APOLLO_API_KEY',
  'APOLLO_API_KEY_BACKUPS',
  'ENRICHMENT_KIT_API_TOKEN',
  'ENRICHMENT_KIT_PORT',
  'ENRICHMENT_KIT_MAX_PROVIDER_CALLS',
  'ENRICHMENT_KIT_TIMEOUT_MS',
];
const MAX_BODY_BYTES = 16 * 1024;
const HUNTER = 'https://api.hunter.io/v2';
const APOLLO_MATCH = 'https://api.apollo.io/api/v1/people/match';

// ------------------------------------------------------------------ backup keys
function splitKeys(value) {
  return String(value ?? '').split(',').map((k) => k.trim()).filter(Boolean);
}

// Error kinds that mean "this KEY cannot be used now" (not: this request is wrong).
const KEY_REJECTED = new Set(['auth', 'forbidden']);
const KEY_EXHAUSTED = new Set(['quota_exhausted']);
const REJECTED_MS = 24 * 3600 * 1000;
const EXHAUSTED_MS = 6 * 3600 * 1000;

// Ordered keys of one provider. current() = first key not switched off; a key the provider
// rejects or reports as used up is switched off for a while and the next one is used.
export function makeKeyRing(primary, backups = [], now = () => Date.now()) {
  const keys = [...new Set([primary, ...backups].filter(Boolean))];
  const benched = new Map();
  const usable = () => keys.filter((k) => (benched.get(k) ?? 0) <= now());
  return {
    keys,
    size: keys.length,
    usable,
    current: () => usable()[0] ?? keys[0] ?? '',
    position: (key) => keys.indexOf(key) + 1,
    // true = the kind is a key problem AND another key can be tried now
    bench(key, kind) {
      const rejected = KEY_REJECTED.has(kind);
      if (!rejected && !KEY_EXHAUSTED.has(kind)) return false;
      if (!keys.includes(key)) return false;
      benched.set(key, now() + (rejected ? REJECTED_MS : EXHAUSTED_MS));
      return usable().length > 0;
    },
  };
}

// ------------------------------------------------------------------ config
export function readConfig({ envFile, env = process.env } = {}) {
  // Only the keys this sidecar needs are taken from the project .env - nothing else.
  let fromFile = {};
  if (envFile && existsSync(envFile)) fromFile = parseEnv(readFileSync(envFile, 'utf8'));
  const pick = (k) => (env[k] ?? fromFile[k] ?? '').trim();
  return {
    hunterKey: pick('HUNTER_API_KEY'),
    apolloKey: pick('APOLLO_API_KEY'),
    // Backup keys (comma-separated): used when a key is rejected or out of credits.
    hunterBackups: splitKeys(pick('HUNTER_API_KEY_BACKUPS')),
    apolloBackups: splitKeys(pick('APOLLO_API_KEY_BACKUPS')),
    token: pick('ENRICHMENT_KIT_API_TOKEN'),
    port: Number(pick('ENRICHMENT_KIT_PORT') || 8787),
    maxProviderCalls: Number(pick('ENRICHMENT_KIT_MAX_PROVIDER_CALLS') || 50),
    timeoutMs: Number(pick('ENRICHMENT_KIT_TIMEOUT_MS') || 15000),
  };
}

export function loadKit(vendorDir) {
  const require = createRequire(import.meta.url);
  const kit = require(path.join(vendorDir, 'dist', 'index.js'));
  let installed = {};
  const meta = path.join(vendorDir, '..', 'INSTALLED.json');
  if (existsSync(meta)) installed = JSON.parse(readFileSync(meta, 'utf8'));
  return { kit, version: installed.version ?? 'unknown', commit: installed.commit ?? 'unknown' };
}

// ------------------------------------------------------------------ errors
export function classifyHttp(provider, status) {
  if (status === 401) return 'auth';
  if (status === 451) return 'pii_blocked';
  if (status === 422 || status === 400) return 'bad_request';
  if (provider === 'hunter' && status === 403) return 'rate_limited'; // Hunter: rate limit
  if (provider === 'hunter' && status === 429) return 'quota_exhausted'; // Hunter: usage limit
  if (status === 403) return 'forbidden';
  if (status === 429) return 'rate_limited';
  if (status >= 500) return 'provider_error';
  return 'provider_error';
}

function errorInfo(provider, err) {
  if (err?.name === 'BudgetExceeded') return { kind: 'budget_exhausted', statusCode: null };
  if (err?.name === 'AbortError' || /aborted|timeout/i.test(String(err?.message))) {
    return { kind: 'timeout', statusCode: null };
  }
  if (typeof err?.statusCode === 'number') {
    return { kind: classifyHttp(provider, err.statusCode), statusCode: err.statusCode };
  }
  return { kind: 'network', statusCode: null };
}

class BudgetExceeded extends Error {
  constructor() {
    super('sidecar provider-call limit reached');
    this.name = 'BudgetExceeded';
  }
}

// ------------------------------------------------------------------ providers
export function makeSafeApollo(kit, apiKey) {
  // Same mapping as upstream ApolloProvider, but work emails only (no personal-email reveal),
  // no phone reveal, and `match_confidence: none` counts as a miss.
  class SafeApolloProvider extends kit.ApolloProvider {
    async enrich(input) {
      const body = { reveal_personal_emails: false, reveal_phone_number: false };
      if (input.email) body.email = input.email;
      if (input.linkedinUrl) body.linkedin_url = input.linkedinUrl;
      if (input.firstName) body.first_name = input.firstName;
      if (input.lastName) body.last_name = input.lastName;
      if (input.company) body.organization_name = input.company;
      if (input.domain) body.domain = input.domain;
      const res = await this.httpRequest(APOLLO_MATCH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache', 'X-Api-Key': apiKey },
        body: JSON.stringify(body),
        timeoutMs: 12_000,
      });
      const p = res?.person;
      if (!p || res?.match_confidence === 'none') return null;
      const confidence =
        p.email && p.email_status === 'verified' ? 0.9 : p.email ? 0.75 : p.linkedin_url ? 0.65 : 0.5;
      return this.makeOutput({
        email: p.email ?? null,
        emailVerified: p.email_status === 'verified',
        firstName: p.first_name ?? null,
        lastName: p.last_name ?? null,
        fullName: p.name ?? null,
        title: p.title ?? null,
        company: p.organization?.name ?? null,
        companyDomain: p.organization?.primary_domain ?? null,
        linkedinUrl: p.linkedin_url ?? null,
        confidence,
        rawProviderData: { email_status: p.email_status ?? null, match_confidence: res?.match_confidence ?? null },
      });
    }
  }
  return new SafeApolloProvider(apiKey);
}

function evidenceOf(provider, raw) {
  if (!raw) return {};
  if (provider === 'hunter') {
    return {
      score: raw.score ?? null,
      accept_all: raw.accept_all ?? null,
      verification: raw.verification ? { date: raw.verification.date ?? null, status: raw.verification.status ?? null } : null,
      sources_count: Array.isArray(raw.sources) ? raw.sources.length : 0,
    };
  }
  return { email_status: raw.email_status ?? null, match_confidence: raw.match_confidence ?? null };
}

// Phone is an OPTIONAL capability: stripped unless the caller asked for it on this request
// (`includePhone: true`). Only a number already present in the provider's normal response is
// passed through (Hunter's email-finder `phone_number`, no extra credit); Apollo's paid phone
// reveal stays pinned off in makeSafeApollo, so this never spends phone credits.
export function sanitizeOutput(provider, out, includePhone = false) {
  if (!out) return null;
  const phone = includePhone && typeof out.phone === 'string' && out.phone.trim() ? { phone: out.phone.trim().slice(0, 40) } : includePhone ? { phone: null } : {};
  return {
    ...phone,
    email: out.email ?? null,
    emailVerified: out.emailVerified ?? null,
    firstName: out.firstName ?? null,
    lastName: out.lastName ?? null,
    fullName: out.fullName ?? null,
    title: out.title ?? null,
    companyDomain: out.companyDomain ?? null,
    linkedinUrl: out.linkedinUrl ?? null,
    confidence: out.confidence,
    source: out.source ?? provider,
    cost: out.cost ?? 0,
    evidence: evidenceOf(provider, out.rawProviderData),
  };
}

class RecordingProvider {
  // Wraps an upstream provider: enforces the process call limit and records every output or
  // error (status code + kind) so Python can keep per-provider provenance and typed errors.
  constructor(make, ring, state, log, includePhone = false) {
    this.includePhone = includePhone;
    this.make = make; // (apiKey) => upstream provider
    this.ring = ring;
    const inner = make(ring.current());
    this.inner = inner;
    this.name = inner.name;
    this.costPerCall = inner.costPerCall;
    this.typicalHitRate = inner.typicalHitRate;
    this.capabilities = inner.capabilities;
    this.state = state;
    this.log = log;
  }

  async enrich(input) {
    const started = Date.now();
    if (this.state.calls >= this.state.maxCalls) {
      const err = new BudgetExceeded();
      this.log.push({ provider: this.name, output: null, error: err.message, errorKind: 'budget_exhausted', statusCode: null, latencyMs: 0, called: false });
      throw err;
    }
    for (;;) {
      const key = this.ring.current();
      this.state.calls += 1;
      try {
        const out = await this.inner.enrich(input);
        this.log.push({ provider: this.name, output: sanitizeOutput(this.name, out, this.includePhone), error: null, errorKind: null, statusCode: null, latencyMs: Date.now() - started, called: true });
        return out;
      } catch (err) {
        const info = errorInfo(this.name, err);
        // rejected / used-up key and a backup exists: same lookup with the next key
        if (this.ring.bench(key, info.kind) && this.state.calls < this.state.maxCalls) {
          this.inner = this.make(this.ring.current());
          continue;
        }
        this.log.push({ provider: this.name, output: null, error: String(err?.message ?? err).slice(0, 200), errorKind: info.kind, statusCode: info.statusCode, latencyMs: Date.now() - started, called: true });
        throw err;
      }
    }
  }
}

const NO_CACHE = { async get() { return null; }, async set() {}, async has() { return false; } };

// ------------------------------------------------------------------ direct Hunter calls
async function hunterGet(cfg, state, ring, route, params) {
  for (;;) {
    if (state.calls >= state.maxCalls) return { kind: 'budget_exhausted', status: null, body: null };
    state.calls += 1;
    const key = ring.current();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
    try {
      const res = await fetch(`${HUNTER}/${route}?${new URLSearchParams(params)}`, {
        headers: { 'X-API-KEY': key },
        signal: controller.signal,
      });
      let body = null;
      try { body = await res.json(); } catch { body = null; }
      if (res.status >= 400) {
        const kind = classifyHttp('hunter', res.status);
        if (ring.bench(key, kind)) continue; // next backup key, same request
        return { kind, status: res.status, body: null };
      }
      return { kind: null, status: res.status, body };
    } catch (err) {
      return { kind: errorInfo('hunter', err).kind, status: null, body: null };
    } finally {
      clearTimeout(timer);
    }
  }
}

// ------------------------------------------------------------------ handler
export function createApp({ cfg, kitInfo }) {
  const state = { calls: 0, maxCalls: cfg.maxProviderCalls, startedAt: Date.now() };
  const { kit } = kitInfo;
  const hunter = makeKeyRing(cfg.hunterKey, cfg.hunterBackups ?? []);
  const apollo = makeKeyRing(cfg.apolloKey, cfg.apolloBackups ?? []);

  function authorized(req) {
    if (!cfg.token) return true; // allowed only because the server binds to 127.0.0.1
    const got = Buffer.from(String(req.headers.authorization ?? ''));
    const want = Buffer.from(`Bearer ${cfg.token}`);
    return got.length === want.length && timingSafeEqual(got, want);
  }

  function buildProviders(names, log, includePhone = false) {
    const out = [];
    for (const name of names) {
      if (name === 'hunter' && hunter.size) out.push(new RecordingProvider((key) => new kit.HunterProvider(key), hunter, state, log, includePhone));
      if (name === 'apollo' && apollo.size) out.push(new RecordingProvider((key) => makeSafeApollo(kit, key), apollo, state, log, includePhone));
    }
    return out;
  }

  async function enrichRoute(body) {
    const input = body?.input ?? {};
    const wanted = Array.isArray(body?.providers) ? body.providers.filter((p) => p === 'hunter' || p === 'apollo') : ['hunter', 'apollo'];
    const log = [];
    const includePhone = body?.includePhone === true;
    const providers = buildProviders(wanted, log, includePhone);
    if (providers.length === 0) return [409, { error: 'no_providers', message: 'no requested provider is configured in the sidecar' }];
    const hasName = input.fullName || input.firstName || input.lastName;
    if (!input.domain || !hasName) return [422, { error: 'bad_request', message: 'input needs domain and a person name' }];
    const clean = {
      fullName: input.fullName || undefined,
      firstName: input.firstName || undefined,
      lastName: input.lastName || undefined,
      company: input.company || undefined,
      domain: String(input.domain).toLowerCase(),
      linkedinUrl: input.linkedinUrl || undefined,
    };
    const waterfall = new kit.Waterfall({
      providers,
      cache: NO_CACHE,
      minConfidence: Number(body?.minConfidence ?? 0.5),
      earlyExitOnConfidence: Number(body?.earlyExitOnConfidence ?? 0.85),
    });
    const result = await waterfall.enrich(clean);
    const best = sanitizeOutput(result.data?.source ?? 'unknown', result.data, includePhone);
    return [200, {
      success: result.success,
      best,
      outputs: log,
      totalCost: result.totalCost,
      totalLatencyMs: result.totalLatencyMs,
      order: providers.map((p) => p.name),
    }];
  }

  async function verifyRoute(body) {
    const email = String(body?.email ?? '').trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return [422, { error: 'bad_request', message: 'invalid email' }];
    if (!hunter.size) return [409, { error: 'no_providers', message: 'Hunter not configured' }];
    const r = await hunterGet(cfg, state, hunter, 'email-verifier', { email });
    if (r.kind) return [200, { provider: 'hunter', email, status: null, errorKind: r.kind, httpStatus: r.status }];
    const d = r.body?.data ?? {};
    const status = r.status === 202 ? 'pending' : r.status === 222 ? 'unknown' : d.status ?? 'unknown';
    return [200, { provider: 'hunter', email, status, score: d.score ?? null, acceptAll: d.accept_all ?? null, httpStatus: r.status, errorKind: null }];
  }

  async function peopleRoute(body) {
    const domain = String(body?.domain ?? '').trim().toLowerCase();
    if (!domain.includes('.')) return [422, { error: 'bad_request', message: 'domain required' }];
    if (!hunter.size) return [409, { error: 'no_providers', message: 'Hunter not configured' }];
    const limit = Math.min(Math.max(Number(body?.limit ?? 5), 1), 10);
    const r = await hunterGet(cfg, state, hunter, 'domain-search', { domain, type: 'personal', seniority: 'executive', limit: String(limit) });
    if (r.kind) return [200, { provider: 'hunter', people: [], errorKind: r.kind, httpStatus: r.status }];
    const emails = r.body?.data?.emails ?? [];
    const people = emails.map((e) => ({
      firstName: e.first_name ?? null,
      lastName: e.last_name ?? null,
      position: e.position ?? null,
      seniority: e.seniority ?? null,
      email: e.value ?? null,
      confidence: e.confidence ?? null,
      verification: e.verification ? { date: e.verification.date ?? null, status: e.verification.status ?? null } : null,
      sourcesCount: Array.isArray(e.sources) ? e.sources.length : 0,
    }));
    return [200, { provider: 'hunter', people, errorKind: null, httpStatus: r.status }];
  }

  async function accountRoute() {
    // Hunter GET /v2/account is free ("This call is free"); only usage counters are returned -
    // the account holder's name/email are dropped. Does NOT count against the call limit.
    // Backup keys are separate Hunter accounts: their counters are added together.
    if (!hunter.size) return [409, { error: 'no_providers', message: 'Hunter not configured' }];
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
    try {
      const answers = await Promise.all(hunter.keys.map(async (key) => {
        const res = await fetch(`${HUNTER}/account`, { headers: { 'X-API-KEY': key }, signal: controller.signal });
        if (res.status >= 400) return { kind: classifyHttp('hunter', res.status), status: res.status };
        return { data: (await res.json())?.data ?? {} };
      }));
      const good = answers.filter((a) => a.data);
      const keys = { total: hunter.size, refused: answers.length - good.length };
      // one row per key (no key text): accepted?, its own counters and its reset date
      const keysDetail = answers.map((a, i) => ({
        position: i + 1,
        ok: Boolean(a.data),
        kind: a.kind ?? null,
        resetDate: a.data?.reset_date ?? null,
        requests: a.data?.requests ?? null,
      }));
      if (good.length === 0) return [200, { provider: 'hunter', errorKind: answers[0].kind, httpStatus: answers[0].status, keys, keysDetail }];
      const requests = {};
      for (const { data } of good) {
        for (const [name, row] of Object.entries(data.requests ?? {})) {
          requests[name] ??= { used: 0, available: 0 };
          requests[name].used += Number(row?.used ?? 0);
          requests[name].available += Number(row?.available ?? 0);
        }
      }
      const first = good[0].data;
      return [200, { provider: 'hunter', planName: first.plan_name ?? null, resetDate: first.reset_date ?? null, requests, keys, keysDetail, errorKind: null }];
    } catch (err) {
      return [200, { provider: 'hunter', errorKind: errorInfo('hunter', err).kind }];
    } finally {
      clearTimeout(timer);
    }
  }

  function health() {
    return [200, {
      status: 'ok',
      service: 'lead-intel-enrichment-sidecar',
      enrichmentKit: { version: kitInfo.version, commit: kitInfo.commit },
      providers: {
        hunter: { configured: hunter.size > 0, keys: hunter.size, usable: hunter.usable().length },
        apollo: { configured: apollo.size > 0, keys: apollo.size, usable: apollo.usable().length },
      },
      auth: Boolean(cfg.token),
      providerCalls: { used: state.calls, limit: state.maxCalls },
      uptimeSeconds: Math.round((Date.now() - state.startedAt) / 1000),
    }];
  }

  async function handle(method, url, headers, rawBody) {
    const route = url.split('?')[0];
    if (method === 'GET' && route === '/health') return health();
    const routes = { '/v1/enrich': enrichRoute, '/v1/verify': verifyRoute, '/v1/people': peopleRoute, '/v1/account': accountRoute };
    if (method !== 'POST' || !routes[route]) return [404, { error: 'not_found' }];
    if (!authorized({ headers })) return [401, { error: 'unauthorized' }];
    if (rawBody.length > MAX_BODY_BYTES) return [413, { error: 'too_large' }];
    let body;
    try { body = rawBody.length ? JSON.parse(rawBody) : {}; } catch { return [400, { error: 'bad_json' }]; }
    try {
      return await routes[route](body);
    } catch (err) {
      return [500, { error: 'internal', message: String(err?.name ?? 'Error') }];
    }
  }

  return { handle, state };
}
