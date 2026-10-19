// Request Demo / Contact Us delivery for the AIRRP product site.
// One endpoint, one minimal form: the request is validated, checked with Cloudflare Turnstile,
// emailed to the monitored team inbox (with its request type), optionally posted to a CRM webhook,
// and acknowledged to the visitor. Nothing is stored by this code.
//
// Required configuration (same names as the existing AIRRP site):
//   PUBLIC_ENQUIRY_ENABLED=true, RESEND_API_KEY, FROM_EMAIL, ALERT_TO_EMAIL, TURNSTILE_SITE_KEY, TURNSTILE_SECRET
// Optional:
//   SEND_ACKNOWLEDGEMENT=false   do not email the visitor (the page still confirms on screen)
//   CRM_WEBHOOK_URL / CRM_WEBHOOK_SECRET   also POST the request as JSON to an approved CRM

export const CONTACT_EMAIL = 'airrp@dsrdt.com';
export const REQUEST_TYPES = Object.freeze({
  'Request Demo': { tag: 'request_demo', ackSubject: 'We’ve received your AIRRP demo request', noun: 'demo request' },
  'Contact Us': { tag: 'contact_us', ackSubject: 'We’ve received your message to AIRRP', noun: 'message' },
});
const MAX_BODY = 8000;
const LIMITS = { name: 120, email: 180, organisation: 180, message: 2000, token: 2048 };

const MESSAGES = {
  400: 'Please check the form fields and try again.',
  403: 'The security check failed. Please refresh the page and try again.',
  405: 'Please use the form on the AIRRP website.',
  413: 'Your message is too long.',
  415: 'Unsupported submission.',
  502: `Your request could not be delivered. Please email ${CONTACT_EMAIL} directly.`,
  503: `Online submission is not available right now. Please email ${CONTACT_EMAIL}.`,
};

export function json(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...extraHeaders },
  });
}
const fail = (status, message = MESSAGES[status]) => json(status, { ok: false, message });

export function isConfigured(env) {
  return Boolean(
    env.PUBLIC_ENQUIRY_ENABLED === 'true' && env.RESEND_API_KEY && env.FROM_EMAIL && env.ALERT_TO_EMAIL &&
    env.TURNSTILE_SITE_KEY && env.TURNSTILE_SECRET,
  );
}

// GET /api/enquiry-config — never returns secrets; the page falls back to email until this says ready.
export function enquiryConfig(env) {
  const ready = isConfigured(env);
  return json(200, { ready, siteKey: ready ? env.TURNSTILE_SITE_KEY : null });
}

export function methodNotAllowed(allow) {
  return json(405, { ok: false, message: MESSAGES[405] }, { Allow: allow });
}

// Single-line fields: collapse whitespace and drop control characters so nothing can break a header or subject line.
const oneLine = (v) => (typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim() : '');
const multiLine = (v) => (typeof v === 'string' ? v.replace(/\r\n?/g, '\n').replace(/[\u0000-\u0009\u000b-\u001f\u007f]/g, '').trim() : '');
export const isEmail = (v) => v.length <= LIMITS.email && /^[^\s@<>()",;:\\]+@[^\s@<>()",;:\\]+\.[^\s@<>()",;:\\]{2,}$/.test(v);

export function parseSubmission(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const sub = {
    requestType: oneLine(data.requestType),
    name: oneLine(data.name),
    email: oneLine(data.email),
    organisation: oneLine(data.organisation),
    message: multiLine(data.message),
    token: oneLine(data.turnstileToken),
  };
  const valid =
    Object.hasOwn(REQUEST_TYPES, sub.requestType) &&
    sub.name.length >= 2 && sub.name.length <= LIMITS.name &&
    isEmail(sub.email) &&
    sub.organisation.length >= 2 && sub.organisation.length <= LIMITS.organisation &&
    sub.message.length <= LIMITS.message &&
    sub.token.length >= 10 && sub.token.length <= LIMITS.token;
  return valid ? sub : null;
}

function sameOrigin(request) {
  const origin = request.headers.get('Origin');
  if (!origin) return false;
  try {
    const o = new URL(origin);
    const self = new URL(request.url);
    const local = o.hostname === 'localhost' || o.hostname === '127.0.0.1';
    return o.host === self.host && (o.protocol === 'https:' || local);
  } catch {
    return false;
  }
}

async function verifyTurnstile(token, request, env) {
  const form = new FormData();
  form.append('secret', env.TURNSTILE_SECRET);
  form.append('response', token);
  const ip = request.headers.get('CF-Connecting-IP');
  if (ip) form.append('remoteip', ip);
  try {
    const res = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
      method: 'POST', body: form, signal: AbortSignal.timeout(7000),
    });
    if (!res.ok) return false;
    const out = await res.json();
    if (!out.success) return false;
    if (out.hostname && out.hostname !== new URL(request.url).hostname) return false;
    if (out.action && out.action !== 'enquiry') return false;
    return true;
  } catch {
    return false;
  }
}

async function sendEmail(env, email) {
  try {
    const res = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(email),
      signal: AbortSignal.timeout(10000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function teamEmail(sub, env, receivedAt) {
  const type = REQUEST_TYPES[sub.requestType];
  return {
    from: env.FROM_EMAIL,
    to: [env.ALERT_TO_EMAIL],
    reply_to: sub.email,
    subject: `[AIRRP ${sub.requestType}] ${sub.organisation} — ${sub.name}`,
    text: [
      `AIRRP WEBSITE — ${sub.requestType.toUpperCase()}`,
      '',
      `Request type: ${sub.requestType}`,
      `Name: ${sub.name}`,
      `Work email: ${sub.email}`,
      `Organisation: ${sub.organisation}`,
      `Received: ${receivedAt}`,
      '',
      'What they would like to discuss:',
      sub.message || '(not provided)',
      '',
      'Reply to this email to respond to the visitor directly.',
    ].join('\n'),
    tags: [{ name: 'request_type', value: type.tag }],
  };
}

// The acknowledgement deliberately contains no visitor-supplied text, so the form cannot be used
// to send arbitrary content to a third party's inbox from the AIRRP domain.
export function acknowledgementEmail(sub, env) {
  const type = REQUEST_TYPES[sub.requestType];
  return {
    from: env.FROM_EMAIL,
    to: [sub.email],
    reply_to: env.ALERT_TO_EMAIL,
    subject: type.ackSubject,
    text: [
      'Hello,',
      '',
      `Thank you for contacting the AIRRP team. This email confirms that we have received the ${type.noun} you sent through the AIRRP website.`,
      '',
      'A member of the team will reply to this address. You do not need to do anything else.',
      '',
      'If you did not make this request, you can ignore this email.',
      '',
      'AIRRP · DSR Digital Technology',
      CONTACT_EMAIL,
    ].join('\n'),
    tags: [{ name: 'request_type', value: `${type.tag}_ack` }],
  };
}

async function postToCrm(sub, env, receivedAt) {
  if (!env.CRM_WEBHOOK_URL) return null;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (env.CRM_WEBHOOK_SECRET) headers.Authorization = `Bearer ${env.CRM_WEBHOOK_SECRET}`;
    const res = await fetch(env.CRM_WEBHOOK_URL, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        source: 'airrp-website', requestType: sub.requestType, name: sub.name, email: sub.email,
        organisation: sub.organisation, message: sub.message, receivedAt,
      }),
      signal: AbortSignal.timeout(8000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// POST /api/enquiry
export async function handleEnquiry(request, env) {
  if (!isConfigured(env)) return fail(503);
  if (!sameOrigin(request)) return fail(403);
  if (!(request.headers.get('Content-Type') || '').toLowerCase().startsWith('application/json')) return fail(415);
  if (Number(request.headers.get('Content-Length') || 0) > MAX_BODY) return fail(413);

  let raw;
  try {
    raw = await request.text();
  } catch {
    return fail(400);
  }
  if (raw.length > MAX_BODY) return fail(413);
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return fail(400);
  }

  // Honeypot: look successful to the bot, deliver nothing.
  if (data && typeof data.website === 'string' && data.website.trim()) {
    return json(200, { ok: true, acknowledged: false, message: 'Request received.' });
  }

  const sub = parseSubmission(data);
  if (!sub) return fail(400);
  if (!(await verifyTurnstile(sub.token, request, env))) return fail(403);

  const receivedAt = new Date().toISOString();
  if (!(await sendEmail(env, teamEmail(sub, env, receivedAt)))) return fail(502);

  const [crm, ack] = await Promise.all([
    postToCrm(sub, env, receivedAt),
    env.SEND_ACKNOWLEDGEMENT === 'false' ? Promise.resolve(false) : sendEmail(env, acknowledgementEmail(sub, env)),
  ]);
  console.log(JSON.stringify({ event: 'enquiry_delivered', requestType: sub.requestType, acknowledged: ack, crm }));

  return json(200, { ok: true, requestType: sub.requestType, acknowledged: ack, message: 'Request received.' });
}
