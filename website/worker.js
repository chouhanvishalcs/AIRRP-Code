// AIRRP product site — Cloudflare Worker. Static files are served from ./public (see wrangler.toml);
// the Worker only answers the two form endpoints and adds security headers to them.
import { enquiryConfig, handleEnquiry, json, methodNotAllowed } from './src/enquiry.js';

const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
};

function protect(response) {
  const headers = new Headers(response.headers);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) headers.set(k, v);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === '/api/enquiry-config') {
      return protect(request.method === 'GET' ? enquiryConfig(env) : methodNotAllowed('GET'));
    }
    if (pathname === '/api/enquiry') {
      return protect(request.method === 'POST' ? await handleEnquiry(request, env) : methodNotAllowed('POST'));
    }
    if (pathname.startsWith('/api/')) return protect(json(404, { ok: false, message: 'Not found.' }));
    return env.ASSETS.fetch(request);
  },
};
