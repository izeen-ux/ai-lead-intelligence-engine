// Sidecar tests: real (pinned, built) enrichment-kit Waterfall + our routes, with fetch stubbed.
// No network: every outbound request goes to the stub; unexpected hosts fail the test.
import { afterEach, beforeEach, describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApp, loadKit, makeKeyRing, readConfig } from '../lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const kitInfo = loadKit(path.join(here, '..', 'vendor', 'enrichment-kit'));
const TOKEN = 'local-test-token';
const realFetch = globalThis.fetch;
let calls;
let routes;

function json(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  calls = [];
  routes = {};
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(String(url));
    const key = `${u.host}${u.pathname}`;
    calls.push({ key, url: u, init });
    const handler = routes[key];
    if (!handler) throw new Error(`unexpected request to ${key}`);
    return handler(u, init);
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

function app(over = {}) {
  const cfg = { hunterKey: 'hk', apolloKey: 'ak', token: TOKEN, port: 0, maxProviderCalls: 50, timeoutMs: 2000, ...over };
  return createApp({ cfg, kitInfo });
}
const auth = { authorization: `Bearer ${TOKEN}` };
const post = (a, route, body, headers = auth) => a.handle('POST', route, headers, JSON.stringify(body));
const INPUT = { input: { fullName: 'Manav Bansal', domain: 'drivn.co.in', company: 'Drivn' } };

const hunterFound = (score, status = 'valid') => () =>
  json(200, {
    data: {
      first_name: 'Manav', last_name: 'Bansal', email: 'manav@drivn.co.in', score, domain: 'drivn.co.in',
      accept_all: false, position: 'CEO', linkedin_url: null, phone_number: '+91 00000', company: 'Drivn',
      sources: [{ uri: 'https://drivn.co.in/team' }], verification: { date: '2026-09-20', status },
    },
  });
const apolloMatch = (conf = 'high') => () =>
  json(200, {
    match_confidence: conf,
    person: {
      first_name: 'Manav', last_name: 'Bansal', name: 'Manav Bansal', email: 'manav.b@drivn.co.in', email_status: 'verified',
      linkedin_url: 'https://www.linkedin.com/in/manav-bansal', title: 'CEO & Co-Founder', phone_numbers: [],
      organization: { name: 'Drivn', primary_domain: 'drivn.co.in' }, city: 'Gurgaon', state: null, country: 'India',
    },
  });

describe('config + health', () => {
  test('readConfig takes only the allowed keys from the env file', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'sidecar-'));
    const file = path.join(dir, '.env');
    writeFileSync(file, 'HUNTER_API_KEY=h1\nAPOLLO_API_KEY=a1\nDATABASE_URL=postgres://secret\nENRICHMENT_KIT_PORT=9999\n');
    const cfg = readConfig({ envFile: file, env: {} });
    assert.deepEqual(Object.keys(cfg).sort(), ['apolloBackups', 'apolloKey', 'hunterBackups', 'hunterKey', 'maxProviderCalls', 'port', 'timeoutMs', 'token']);
    assert.equal(cfg.port, 9999);
    assert.equal(JSON.stringify(cfg).includes('postgres'), false);
  });

  test('health is config-only: no provider call, no secret values', async () => {
    const [status, body] = await app().handle('GET', '/health', {}, '');
    assert.equal(status, 200);
    assert.equal(calls.length, 0);
    assert.equal(body.providers.hunter.configured, true);
    assert.equal(body.enrichmentKit.commit.startsWith('6998898c03'), true);
    const text = JSON.stringify(body);
    for (const secret of ['hk', 'ak', TOKEN]) assert.equal(text.includes(`"${secret}"`), false);
  });

  test('POST routes need the bearer token', async () => {
    const [status] = await post(app(), '/v1/enrich', INPUT, {});
    assert.equal(status, 401);
    assert.equal(calls.length, 0);
  });

  test('bad requests are rejected before any provider call', async () => {
    const a = app();
    assert.equal((await post(a, '/v1/enrich', { input: { domain: 'drivn.co.in' } }))[0], 422);
    assert.equal((await post(a, '/v1/verify', { email: 'nope' }))[0], 422);
    assert.equal((await a.handle('POST', '/v1/enrich', auth, 'x'.repeat(20000)))[0], 413);
    assert.equal((await a.handle('POST', '/v1/enrich', auth, '{bad'))[0], 400);
    assert.equal((await a.handle('GET', '/v1/enrich', auth, ''))[0], 404);
    assert.equal(calls.length, 0);
  });
});

describe('waterfall (upstream enrichment-kit)', () => {
  test('confident Hunter hit exits early - Apollo not called', async () => {
    routes['api.hunter.io/v2/email-finder'] = hunterFound(92);
    const [status, body] = await post(app(), '/v1/enrich', INPUT);
    assert.equal(status, 200);
    assert.equal(body.success, true);
    assert.deepEqual(body.order, ['hunter', 'apollo']);
    assert.equal(body.best.email, 'manav@drivn.co.in');
    assert.equal(body.best.evidence.verification.status, 'valid');
    assert.equal(body.outputs.length, 1);
    assert.equal(calls.length, 1);
    assert.equal(JSON.stringify(body).includes('+91'), false); // phone never returned
  });

  test('phone is passed through only when the request asks for it (includePhone)', async () => {
    routes['api.hunter.io/v2/email-finder'] = hunterFound(92);
    const [, off] = await post(app(), '/v1/enrich', { ...INPUT, includePhone: false });
    assert.equal('phone' in off.best, false);
    const [, on] = await post(app(), '/v1/enrich', { ...INPUT, includePhone: true });
    assert.match(on.best.phone, /^\+91/);
    assert.equal(on.outputs[0].output.phone, on.best.phone);
    assert.equal(calls.length, 2); // one finder call each: no extra phone lookup, no extra credit
  });

  test('phone requested but Apollo is the source: reveal stays OFF and no number is returned', async () => {
    routes['api.hunter.io/v2/email-finder'] = () => json(200, { data: { email: null } });
    routes['api.apollo.io/api/v1/people/match'] = apolloMatch();
    const [, body] = await post(app(), '/v1/enrich', { ...INPUT, includePhone: true });
    assert.equal(body.best.source, 'apollo');
    assert.equal(body.best.phone, null);
    assert.equal(JSON.parse(calls[1].init.body).reveal_phone_number, false);
  });

  test('Hunter miss falls back to Apollo with personal-email reveal OFF', async () => {
    routes['api.hunter.io/v2/email-finder'] = () => json(200, { data: { email: null } });
    routes['api.apollo.io/api/v1/people/match'] = apolloMatch();
    const [, body] = await post(app(), '/v1/enrich', INPUT);
    assert.equal(body.best.source, 'apollo');
    assert.equal(body.best.evidence.email_status, 'verified');
    assert.deepEqual(body.outputs.map((o) => [o.provider, o.output?.email ?? null]), [['hunter', null], ['apollo', 'manav.b@drivn.co.in']]);
    const sent = JSON.parse(calls[1].init.body);
    assert.equal(sent.reveal_personal_emails, false);
    assert.equal(sent.reveal_phone_number, false);
    assert.equal(calls[1].init.headers['X-Api-Key'], 'ak');
  });

  test('low-confidence Hunter result continues to Apollo; both outputs recorded', async () => {
    routes['api.hunter.io/v2/email-finder'] = hunterFound(60, 'accept_all');
    routes['api.apollo.io/api/v1/people/match'] = apolloMatch();
    const [, body] = await post(app(), '/v1/enrich', INPUT);
    assert.equal(body.outputs.length, 2);
    assert.equal(body.best.source, 'apollo'); // 0.9 > 0.6
  });

  test('Hunter usage limit (429) is typed; Apollo still tried', async () => {
    routes['api.hunter.io/v2/email-finder'] = () => json(429, { errors: [{ details: 'usage limit' }] });
    routes['api.apollo.io/api/v1/people/match'] = apolloMatch('none');
    const [, body] = await post(app(), '/v1/enrich', INPUT);
    assert.equal(body.success, false);
    assert.equal(body.outputs[0].errorKind, 'quota_exhausted');
    assert.equal(body.outputs[0].statusCode, 429);
    assert.equal(body.outputs[1].output, null); // match_confidence none = miss
  });

  test('providers filter + process call limit', async () => {
    routes['api.hunter.io/v2/email-finder'] = () => json(200, { data: { email: null } });
    const onlyHunter = await post(app(), '/v1/enrich', { ...INPUT, providers: ['hunter'] });
    assert.deepEqual(onlyHunter[1].order, ['hunter']);
    const limited = app({ maxProviderCalls: 1 });
    const [, body] = await post(limited, '/v1/enrich', INPUT);
    assert.equal(body.outputs[1].errorKind, 'budget_exhausted');
    assert.equal(body.outputs[1].called, false);
    assert.equal(limited.state.calls, 1);
  });

  test('unconfigured provider is not used; none configured -> 409', async () => {
    routes['api.hunter.io/v2/email-finder'] = () => json(200, { data: { email: null } });
    const [, body] = await post(app({ apolloKey: '' }), '/v1/enrich', INPUT);
    assert.deepEqual(body.order, ['hunter']);
    assert.equal((await post(app({ hunterKey: '', apolloKey: '' }), '/v1/enrich', INPUT))[0], 409);
  });
});

describe('verify + people', () => {
  test('verification statuses incl. 202 pending and typed errors', async () => {
    const a = app();
    routes['api.hunter.io/v2/email-verifier'] = () => json(200, { data: { status: 'valid', score: 97, accept_all: false } });
    let [, body] = await post(a, '/v1/verify', { email: 'Manav@Drivn.co.in' });
    assert.deepEqual([body.status, body.score, body.email], ['valid', 97, 'manav@drivn.co.in']);
    assert.equal(calls[0].init.headers['X-API-KEY'], 'hk');
    assert.equal(calls[0].url.searchParams.has('api_key'), false); // key in header, not URL
    routes['api.hunter.io/v2/email-verifier'] = () => json(202, { data: {} });
    [, body] = await post(a, '/v1/verify', { email: 'a@drivn.co.in' });
    assert.equal(body.status, 'pending');
    routes['api.hunter.io/v2/email-verifier'] = () => json(401, {});
    [, body] = await post(a, '/v1/verify', { email: 'a@drivn.co.in' });
    assert.deepEqual([body.status, body.errorKind], [null, 'auth']);
    routes['api.hunter.io/v2/email-verifier'] = () => json(451, {});
    [, body] = await post(a, '/v1/verify', { email: 'a@drivn.co.in' });
    assert.equal(body.errorKind, 'pii_blocked');
  });

  test('verification timeout is typed', async () => {
    routes['api.hunter.io/v2/email-verifier'] = (u, init) =>
      new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
    const [, body] = await post(app({ timeoutMs: 20 }), '/v1/verify', { email: 'a@drivn.co.in' });
    assert.equal(body.errorKind, 'timeout');
  });

  test('account usage is free, uncounted and drops personal account data', async () => {
    routes['api.hunter.io/v2/account'] = () =>
      json(200, { data: { first_name: 'Owner', email: 'owner@example.com', plan_name: 'Free', reset_date: '2026-10-15', requests: { searches: { used: 3, available: 25 }, verifications: { used: 1, available: 50 } } } });
    const a = app({ maxProviderCalls: 0 });
    const [, body] = await post(a, '/v1/account', {});
    assert.equal(body.planName, 'Free');
    assert.deepEqual(body.requests.searches, { used: 3, available: 25 });
    assert.equal(JSON.stringify(body).includes('owner@example.com'), false);
    assert.equal(a.state.calls, 0);
  });

  test('people search maps Hunter domain-search executives', async () => {
    routes['api.hunter.io/v2/domain-search'] = (u) => {
      assert.equal(u.searchParams.get('seniority'), 'executive');
      assert.equal(u.searchParams.get('limit'), '3');
      return json(200, { data: { emails: [{ value: 'manav@drivn.co.in', first_name: 'Manav', last_name: 'Bansal', position: 'CEO', seniority: 'executive', confidence: 94, verification: { date: '2026-09-01', status: 'valid' }, sources: [{}, {}] }] } });
    };
    const [, body] = await post(app(), '/v1/people', { domain: 'drivn.co.in', limit: 3 });
    assert.deepEqual(body.people[0], {
      firstName: 'Manav', lastName: 'Bansal', position: 'CEO', seniority: 'executive', email: 'manav@drivn.co.in',
      confidence: 94, verification: { date: '2026-09-01', status: 'valid' }, sourcesCount: 2,
    });
  });
});

describe('backup keys (2026-10-14)', () => {
  test('key ring: first usable key, benched keys come back after the cool-down', () => {
    let now = 0;
    const ring = makeKeyRing('k1', ['k2', 'k1', 'k3'], () => now);
    assert.deepEqual([ring.size, ring.current()], [3, 'k1']);
    assert.equal(ring.bench('k1', 'bad_request'), false); // not a key problem
    assert.equal(ring.current(), 'k1');
    assert.equal(ring.bench('k1', 'quota_exhausted'), true);
    assert.equal(ring.current(), 'k2');
    assert.equal(ring.bench('k2', 'auth'), true);
    assert.equal(ring.bench('k3', 'auth'), false); // nothing left to switch to
    assert.equal(ring.current(), 'k1'); // all benched: the provider's own error is shown
    now = 7 * 3600 * 1000; // credits may be back after 6 h; a rejected key stays off for 24 h
    assert.deepEqual(ring.usable(), ['k1']);
  });

  test('a rejected or used-up Hunter key is replaced by the next one, same request', async () => {
    const a = app({ hunterBackups: ['hk2', 'hk3'] });
    routes['api.hunter.io/v2/email-verifier'] = (u, init) => {
      const key = init.headers['X-API-KEY'];
      if (key === 'hk') return json(401, {}); // old key no longer accepted
      if (key === 'hk2') return json(429, {}); // Hunter: usage limit reached
      return json(200, { data: { status: 'valid', score: 90 } });
    };
    let [, body] = await post(a, '/v1/verify', { email: 'a@drivn.co.in' });
    assert.deepEqual([body.status, body.errorKind], ['valid', null]);
    assert.deepEqual(calls.map((c) => c.init.headers['X-API-KEY']), ['hk', 'hk2', 'hk3']);
    calls.length = 0;
    [, body] = await post(a, '/v1/verify', { email: 'b@drivn.co.in' });
    assert.deepEqual(calls.map((c) => c.init.headers['X-API-KEY']), ['hk3']); // remembered
  });

  test('account usage adds the keys together and says how many were refused', async () => {
    const a = app({ hunterBackups: ['hk2', 'hk3'] });
    routes['api.hunter.io/v2/account'] = (u, init) =>
      init.headers['X-API-KEY'] === 'hk'
        ? json(401, {})
        : json(200, { data: { plan_name: 'Free', requests: { searches: { used: 5, available: 50 } } } });
    const [, body] = await post(a, '/v1/account', {});
    assert.deepEqual(body.requests.searches, { used: 10, available: 100 });
    assert.deepEqual(body.keys, { total: 3, refused: 1 });
    assert.equal(JSON.stringify(body).includes('hk2'), false);
  });

  test('no backups configured: behaviour is unchanged (typed error, one call)', async () => {
    const a = app();
    routes['api.hunter.io/v2/email-verifier'] = () => json(401, {});
    const [, body] = await post(a, '/v1/verify', { email: 'a@drivn.co.in' });
    assert.equal(body.errorKind, 'auth');
    assert.equal(calls.length, 1);
  });
});
