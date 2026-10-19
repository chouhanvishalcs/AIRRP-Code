import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { enquiryConfig, handleEnquiry, parseSubmission } from '../src/enquiry.js';
import worker from '../worker.js';

const ENV = {
  PUBLIC_ENQUIRY_ENABLED: 'true',
  RESEND_API_KEY: 're_test',
  FROM_EMAIL: 'AIRRP <no-reply@dsrdt.com>',
  ALERT_TO_EMAIL: 'airrp@dsrdt.com',
  TURNSTILE_SITE_KEY: 'site-key',
  TURNSTILE_SECRET: 'turnstile-secret',
};
const VALID = {
  requestType: 'Request Demo',
  name: 'Demo Reviewer',
  email: 'reviewer@example.org',
  organisation: 'AIRRP Demo Organization - Retail',
  message: 'Compliance and risk register for two programs.',
  turnstileToken: 'token-0123456789',
  website: '',
};

let calls;
let responders;
const realFetch = globalThis.fetch;
const realLog = console.log;

beforeEach(() => {
  calls = [];
  responders = {
    turnstile: () => Response.json({ success: true, hostname: 'airrp.dsrdt.com', action: 'enquiry' }),
    resend: () => Response.json({ id: 'email_1' }),
    crm: () => new Response('ok'),
  };
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const call = { url: u, init, body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body };
    calls.push(call);
    if (u.includes('turnstile')) return responders.turnstile(call);
    if (u.includes('resend.com')) return responders.resend(call);
    return responders.crm(call);
  };
  console.log = () => {};
});
afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
});

function post(body, { origin = 'https://airrp.dsrdt.com', type = 'application/json', url = 'https://airrp.dsrdt.com/api/enquiry' } = {}) {
  const headers = { 'Content-Type': type };
  if (origin) headers.Origin = origin;
  return new Request(url, { method: 'POST', headers, body: typeof body === 'string' ? body : JSON.stringify(body) });
}
const emails = () => calls.filter((c) => c.url.includes('resend.com')).map((c) => c.body);

test('config reports not ready and hides the site key until every binding exists', async () => {
  const off = await enquiryConfig({ ...ENV, PUBLIC_ENQUIRY_ENABLED: 'false' }).json();
  assert.deepEqual(off, { ready: false, siteKey: null });
  const missing = await enquiryConfig({ ...ENV, RESEND_API_KEY: '' }).json();
  assert.equal(missing.ready, false);
  const on = await enquiryConfig(ENV).json();
  assert.deepEqual(on, { ready: true, siteKey: 'site-key' });
});

test('unconfigured deployment refuses with 503 and sends nothing', async () => {
  const res = await handleEnquiry(post(VALID), {});
  assert.equal(res.status, 503);
  assert.equal(calls.length, 0);
});

test('rejects cross-origin, missing origin and plain-http origins', async () => {
  for (const origin of [null, 'https://evil.example', 'http://airrp.dsrdt.com']) {
    const res = await handleEnquiry(post(VALID, { origin }), ENV);
    assert.equal(res.status, 403, String(origin));
  }
  assert.equal(calls.length, 0);
});

test('allows the local dev origin over http', async () => {
  const res = await handleEnquiry(post(VALID, { origin: 'http://localhost:8787', url: 'http://localhost:8787/api/enquiry' }), {
    ...ENV,
  });
  // Turnstile hostname is airrp.dsrdt.com in the stub, so the check fails — but only after origin passed.
  assert.equal(res.status, 403);
  assert.ok(calls.some((c) => c.url.includes('turnstile')));
});

test('rejects non-JSON and oversized bodies', async () => {
  assert.equal((await handleEnquiry(post('name=x', { type: 'application/x-www-form-urlencoded' }), ENV)).status, 415);
  assert.equal((await handleEnquiry(post({ ...VALID, message: 'x'.repeat(9000) }), ENV)).status, 413);
  assert.equal((await handleEnquiry(post('{not json'), ENV)).status, 400);
});

test('validates required fields and the request type', () => {
  assert.ok(parseSubmission(VALID));
  assert.equal(parseSubmission({ ...VALID, message: '' }).message, '', 'message is optional');
  for (const bad of [
    { requestType: 'Newsletter' },
    { name: 'A' },
    { email: 'not-an-email' },
    { email: 'a@b' },
    { organisation: '' },
    { message: 'x'.repeat(2001) },
    { turnstileToken: '' },
  ]) {
    assert.equal(parseSubmission({ ...VALID, ...bad }), null, JSON.stringify(bad));
  }
});

test('invalid submission returns 400 before contacting Turnstile', async () => {
  const res = await handleEnquiry(post({ ...VALID, email: 'nope' }), ENV);
  assert.equal(res.status, 400);
  assert.equal(calls.length, 0);
});

test('honeypot looks successful but delivers nothing', async () => {
  const res = await handleEnquiry(post({ ...VALID, website: 'https://spam.example' }), ENV);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).ok, true);
  assert.equal(calls.length, 0);
});

test('failed or mismatched Turnstile verification is rejected', async () => {
  responders.turnstile = () => Response.json({ success: false });
  assert.equal((await handleEnquiry(post(VALID), ENV)).status, 403);
  responders.turnstile = () => Response.json({ success: true, hostname: 'other.example', action: 'enquiry' });
  assert.equal((await handleEnquiry(post(VALID), ENV)).status, 403);
  responders.turnstile = () => Response.json({ success: true, hostname: 'airrp.dsrdt.com', action: 'login' });
  assert.equal((await handleEnquiry(post(VALID), ENV)).status, 403);
  assert.equal(emails().length, 0);
});

test('demo request reaches the team inbox with its type and is acknowledged to the visitor', async () => {
  const res = await handleEnquiry(post(VALID), ENV);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, requestType: 'Request Demo', acknowledged: true, message: 'Request received.' });

  const [team, ack] = emails();
  assert.deepEqual(team.to, ['airrp@dsrdt.com']);
  assert.equal(team.reply_to, VALID.email);
  assert.match(team.subject, /^\[AIRRP Request Demo\] AIRRP Demo Organization - Retail — Demo Reviewer$/);
  assert.match(team.text, /Request type: Request Demo/);
  assert.match(team.text, /Compliance and risk register/);
  assert.deepEqual(team.tags, [{ name: 'request_type', value: 'request_demo' }]);

  assert.deepEqual(ack.to, [VALID.email]);
  assert.equal(ack.reply_to, 'airrp@dsrdt.com');
  assert.match(ack.subject, /demo request/);
  assert.ok(!ack.text.includes(VALID.name) && !ack.text.includes(VALID.organisation) && !ack.text.includes('Compliance and risk'),
    'acknowledgement must not echo visitor-supplied text');
});

test('contact request is routed with the contact_us type', async () => {
  const res = await handleEnquiry(post({ ...VALID, requestType: 'Contact Us', message: '' }), ENV);
  assert.equal(res.status, 200);
  const [team, ack] = emails();
  assert.match(team.subject, /^\[AIRRP Contact Us\]/);
  assert.deepEqual(team.tags, [{ name: 'request_type', value: 'contact_us' }]);
  assert.match(team.text, /\(not provided\)/);
  assert.match(ack.subject, /message/);
});

test('team delivery failure returns 502 and no acknowledgement is sent', async () => {
  responders.resend = () => new Response('down', { status: 500 });
  const res = await handleEnquiry(post(VALID), ENV);
  assert.equal(res.status, 502);
  assert.equal((await res.json()).ok, false);
  assert.equal(emails().length, 1);
});

test('acknowledgement failure still confirms receipt but reports acknowledged:false', async () => {
  let n = 0;
  responders.resend = () => (++n === 1 ? Response.json({ id: 'team' }) : new Response('fail', { status: 500 }));
  const out = await (await handleEnquiry(post(VALID), ENV)).json();
  assert.equal(out.ok, true);
  assert.equal(out.acknowledged, false);
});

test('SEND_ACKNOWLEDGEMENT=false sends only the team email', async () => {
  const out = await (await handleEnquiry(post(VALID), { ...ENV, SEND_ACKNOWLEDGEMENT: 'false' })).json();
  assert.equal(out.acknowledged, false);
  assert.equal(emails().length, 1);
});

test('optional CRM webhook receives the request with its type', async () => {
  await handleEnquiry(post(VALID), { ...ENV, CRM_WEBHOOK_URL: 'https://crm.example/hook', CRM_WEBHOOK_SECRET: 's3cret' });
  const crm = calls.find((c) => c.url === 'https://crm.example/hook');
  assert.ok(crm);
  assert.equal(crm.init.headers.Authorization, 'Bearer s3cret');
  assert.equal(crm.body.requestType, 'Request Demo');
  assert.equal(crm.body.email, VALID.email);
});

test('line breaks cannot be smuggled into single-line fields', async () => {
  await handleEnquiry(post({ ...VALID, name: 'Demo\r\nBcc: victim@example.com', organisation: 'Org\nX' }), ENV);
  const [team] = emails();
  assert.ok(!/[\r\n]/.test(team.subject));
  assert.match(team.subject, /Demo Bcc: victim@example.com/);
});

test('worker routes API methods, unknown API paths and static assets', async () => {
  const env = { ...ENV, ASSETS: { fetch: async () => new Response('<html>', { headers: { 'Content-Type': 'text/html' } }) } };
  const getEnquiry = await worker.fetch(new Request('https://airrp.dsrdt.com/api/enquiry'), env);
  assert.equal(getEnquiry.status, 405);
  assert.equal(getEnquiry.headers.get('Allow'), 'POST');
  assert.equal(getEnquiry.headers.get('X-Frame-Options'), 'DENY');

  const unknown = await worker.fetch(new Request('https://airrp.dsrdt.com/api/other'), env);
  assert.equal(unknown.status, 404);

  const cfg = await worker.fetch(new Request('https://airrp.dsrdt.com/api/enquiry-config'), env);
  assert.equal((await cfg.json()).ready, true);

  const page = await worker.fetch(new Request('https://airrp.dsrdt.com/'), env);
  assert.equal(await page.text(), '<html>');
});
