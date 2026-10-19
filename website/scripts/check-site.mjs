// Acceptance checks for the AIRRP product site (Website Build Brief V2, §8 mandatory checklist).
//   node scripts/check-site.mjs            structure, links, form, CSP — exits 1 on any failure
//   node scripts/check-site.mjs --release  also refuses while pricing is pending or a release gate is open
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const pub = join(root, 'public');
const release = process.argv.includes('--release');
const read = (p) => readFileSync(join(pub, p), 'utf8');
const html = read('index.html');
const gates = JSON.parse(readFileSync(join(root, 'release-gates.json'), 'utf8'));
const failures = [];
const warnings = [];
const check = (ok, msg) => { if (!ok) failures.push(msg); };
const text = (s) => s.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();

// 1. One page, three content areas (plus the introductory headline).
const sections = [...html.matchAll(/<section\b[^>]*>/g)].map((m) => m[0]);
check(sections.length === 4, `expected hero + 3 content sections, found ${sections.length} <section> elements`);
for (const id of ['capabilities', 'pricing', 'contact']) check(html.includes(`id="${id}"`), `missing section #${id}`);

// Navigation: exactly the four agreed items.
const nav = html.match(/<nav class="nav-menu"[\s\S]*?<\/nav>/)?.[0] ?? '';
const navLabels = [...nav.matchAll(/<a\b[^>]*>([\s\S]*?)<\/a>/g)].map((m) => text(m[1]));
check(JSON.stringify([...new Set(navLabels)].sort()) === JSON.stringify(['Capabilities', 'Contact Us', 'Pricing', 'Request Demo']),
  `navigation should be Capabilities | Pricing | Request Demo | Contact Us, found: ${navLabels.join(' | ')}`);

// 2. Immediate differentiation: four statements beside the headline, before the six cards.
const hero = html.match(/<section class="hero"[\s\S]*?<\/section>/)?.[0] ?? '';
const why = [...hero.matchAll(/<li class="why-item">[\s\S]*?<\/li>/g)].map((m) => m[0]);
const whyTitles = why.map((w) => text(w.match(/<h3>([\s\S]*?)<\/h3>/)?.[1] ?? ''));
check(JSON.stringify(whyTitles) === JSON.stringify(['Enterprise context first', 'Govern once, reuse appropriately', 'Explain why', 'Connect outcomes']),
  `hero must show the four differentiators, found: ${whyTitles.join(' | ') || 'none'}`);
why.forEach((w, i) => check(/class="why-cue"/.test(w), `differentiator ${i + 1} needs a workflow cue or proof point`));

// 3. Six named capabilities, each with what / how / why choose / product proof, plus a four-step workflow.
const expected = [
  'Compliance Management', 'Risk Assessments', 'Centralised Enterprise Risk Register', 'Third-Party Risk Management',
  'Governance Document Intelligence', 'Customer Security and Compliance Questionnaire Assistance',
];
const caps = [...html.matchAll(/<article class="cap"[\s\S]*?<\/article>/g)].map((m) => m[0]);
check(caps.length === 6, `expected 6 capability cards, found ${caps.length}`);
caps.forEach((cap, i) => {
  const title = text(cap.match(/<h3[^>]*>([\s\S]*?)<\/h3>/)?.[1] ?? '');
  check(title === expected[i], `capability ${i + 1} should be "${expected[i]}", found "${title}"`);
  for (const label of ['What it does', 'How it works differently', 'Why a customer would choose it']) {
    check(cap.includes(`<dt>${label}</dt>`), `${title}: missing "${label}"`);
  }
  const steps = (cap.match(/<ol class="flow-chain"[\s\S]*?<\/ol>/)?.[0].match(/<li>/g) ?? []).length;
  check(steps === 4, `${title}: workflow should have 4 steps, found ${steps}`);
  check(/<figure class="proof[^"]*"/.test(cap) && cap.includes('Product proof'), `${title}: missing the product proof block`);
  if (/<a class="proof-shot"/.test(cap)) check(/<img [^>]*alt="[^"]{40,}"/.test(cap), `${title}: proof capture needs descriptive alt text`);
});
const tprm = caps.find((c) => c.includes('id="cap-tprm"')) ?? '';
const quest = caps.find((c) => c.includes('id="cap-questionnaires"')) ?? '';
check(tprm.includes('You assess your third parties'), 'TPRM card must say it assesses your third parties');
check(quest.includes('Your customers assess you'), 'Questionnaire card must say your customers assess you');
check(html.includes('id="directions-title"'), 'TPRM vs questionnaire distinction panel missing');

// Required content order: hero + advantage → six capabilities → connected example → value → pricing → contact.
const order = ['id="why-different"', 'class="cap-grid"', 'id="connected-example"', 'id="why-connected"', 'id="pricing"', 'id="contact"'].map((k) => html.indexOf(k));
check(order.every((v, i) => v > 0 && (i === 0 || v > order[i - 1])), 'content order must be advantage → capabilities → connected example → value → pricing → contact');

// 5. One connected example: at least four visible steps; the TPRM step stays hidden until TPRM is verified.
const connected = html.match(/<div class="connected"[\s\S]*?<\/ol>/)?.[0] ?? '';
const visibleSteps = [...connected.matchAll(/<li class="cstep[^"]*"([^>]*)>/g)].filter((m) => !/\bhidden\b/.test(m[1])).length;
check(visibleSteps >= 4, `connected example needs at least 4 visible steps, found ${visibleSteps}`);
const tprmStep = connected.match(/<li class="cstep[^"]*" data-gate="tprm"([^>]*)>/);
if (tprmStep && gates.tprmEndToEndValidated !== true) check(/\bhidden\b/.test(tprmStep[1]), 'TPRM step in the connected example must stay hidden until tprmEndToEndValidated');
check(/class="connected-takeaway"/.test(html), 'connected example needs the visitor takeaway');

// 6. Value before price: the comparison sits directly before the pricing section.
const valueBlock = html.match(/<div class="value[^"]*" id="why-connected"[\s\S]*?<\/table>/)?.[0] ?? '';
check(text(valueBlock).includes('Why connected work matters'), 'value section must be headed "Why connected work matters"');
check((valueBlock.match(/<tr>/g) ?? []).length === 5, 'value comparison should have a header row and 4 rows');
const between = html.slice(html.indexOf('</table>', html.indexOf('id="why-connected"')), html.indexOf('id="pricing"'));
check(!/<(section|div class="(cap|connected|directions))/.test(between.replace(/<section class="section section-white" $/, '')), 'nothing may sit between the value comparison and Pricing');

// 9. No unsupported claims, competitors or launch-excluded content.
const visible = text(html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<!--[\s\S]*?-->/g, ''));
for (const [re, why] of [
  [/\b(best|leading|world[- ]class|unrivalled|unmatched|only platform|#1|guarantee[sd]? compliance)\b/i, 'superlative or exclusivity claim'],
  [/\b(vanta|drata|onetrust|servicenow|archer|logicgate)\b/i, 'competitor name'],
  [/\b(testimonial|blog|case study|lorem|todo|tbd)\b/i, 'excluded or placeholder content'],
  [/how airrp works|ai[- ]powered/i, 'phrase the brief rules out'],
  [gates.pricingApproved === true ? null : /[₹$€£]\s?\d/, 'numeric price before pricingApproved is set'],
]) {
  if (!re) continue;
  const m = visible.match(re);
  check(!m, `${why}: "${m?.[0]}"`);
}

// 6. Lead form: request type, required + optional fields, privacy statement, status region.
const form = html.match(/<form[\s\S]*?<\/form>/)?.[0] ?? '';
check(/value="Request Demo"/.test(form) && /value="Contact Us"/.test(form), 'form needs both request types');
for (const name of ['name', 'email', 'organisation']) {
  check(new RegExp(`name="${name}"[^>]*required`).test(form), `form field "${name}" must be required`);
}
check(/<textarea[^>]*name="message"(?![^>]*required)/.test(form), 'message must be optional');
check(/href="privacy\.html"/.test(form), 'form needs a privacy statement link');
check(/role="status"/.test(form) && /aria-live="polite"/.test(form), 'form needs a live status region');
check(/action="mailto:/.test(form), 'form needs a no-JavaScript mailto fallback');

// Internal anchors and local files.
for (const page of ['index.html', 'privacy.html', '404.html']) {
  const src = read(page);
  for (const [, id] of src.matchAll(/href="#([^"]+)"/g)) check(src.includes(`id="${id}"`), `${page}: broken anchor #${id}`);
  for (const [, ref] of src.matchAll(/(?:src|href)="([^"#:]+?)(?:[?#][^"]*)?"/g)) {
    if (ref === '/' || ref.startsWith('//')) continue;
    const file = ref.startsWith('/') ? join(pub, ref) : join(pub, ref);
    check(existsSync(file), `${page}: missing local file ${ref}`);
  }
  // the capture viewer's <img> has no src until opened; its alt is copied from the thumbnail then
  for (const [img] of src.matchAll(/<img\b[^>]*>/g)) if (/\bsrc=/.test(img)) check(/\balt="[^"]+"/.test(img), `${page}: image without alt text`);
  check(!/\sstyle="/.test(src), `${page}: inline style attribute would be blocked by the CSP`);
}

// CSP must allow exactly the inline bootstrap script(s) present in the page.
const headers = read('_headers');
for (const [, body] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
  const hash = createHash('sha256').update(body).digest('base64');
  check(headers.includes(`'sha256-${hash}'`), `_headers CSP is missing 'sha256-${hash}' for an inline script`);
}

// 5 & 7. Pricing source and release gates.
const pending = (html.match(/data-pending\b(?!-)/g) ?? []).length;
const pendingProof = (html.match(/data-pending-proof/g) ?? []).length;
const notice = html.includes('data-preview-notice');
const open = Object.entries(gates).filter(([k, v]) => !k.startsWith('_') && v !== true).map(([k]) => k);
if (pending) warnings.push(`pricing: ${pending} cell(s) still awaiting the approved pricing source`);
if (pendingProof) warnings.push(`product proof: ${pendingProof} capability capture(s) still pending`);
if (notice) warnings.push('pricing: content-approval preview notice is still shown');
if (open.length) warnings.push(`release gates open: ${open.join(', ')}`);
if (release) {
  check(!pending, 'release blocked: pricing cells are still pending');
  check(!pendingProof, 'release blocked: product proof captures are still pending');
  check(!notice, 'release blocked: remove the pricing preview notice');
  check(!open.length, `release blocked: open gates ${open.join(', ')}`);
}

for (const w of warnings) console.log(`warn  ${w}`);
for (const f of failures) console.log(`FAIL  ${f}`);
console.log(failures.length ? `\n${failures.length} check(s) failed.` : `\nAll ${release ? 'release ' : ''}checks passed${warnings.length && !release ? ' (preview — see warnings)' : ''}.`);
process.exit(failures.length ? 1 : 0);
