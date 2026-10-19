# AIRRP product website

A single, capability-led page built to the *AIRRP Website Build Brief V2 (Differentiation)*. Navigation is **Capabilities | Pricing | Request Demo | Contact Us**, and the content follows the order V2 requires:

| Order | Block | What is on it |
| --- | --- | --- |
| 1 | Headline + immediate advantage | V2 hero copy and buttons, with **“Why AIRRP is different”** beside it: four differentiators (enterprise context first; govern once, reuse appropriately; explain why; connect outcomes), each with a workflow cue that links to its evidence. All four start above the fold on desktop and laptop. |
| 2 | Six capabilities | Six visible cards, each showing **what it does → how it works differently → why a customer would choose it → product proof**, plus the four-step workflow. The brief’s copy is used word for word. TPRM (*you assess your third parties*) and Questionnaire Assistance (*your customers assess you*) are kept visibly separate. |
| 3 | One connected example | The Employee HR Management System scenario: understand once → determine obligations → assess and decide → reuse governed knowledge, ending with the visitor takeaway. Horizontal on desktop, vertical on mobile. The TPRM step is built but hidden until TPRM is verified. |
| 4 | Why connected work matters | V2’s disconnected-vs-connected comparison table and commercial message, placed directly before Pricing. |
| 5 | Pricing | The approved pricing (see below): five charging units with price, unit and annual term; professional services quoted separately; what is included; the founding-customer discount; “Ask about pricing”. |
| 6 | Request Demo / Contact Us | One short form with a request-type switch; name, work email and organisation are required and the message is optional. |

Look and feel: the midnight-teal gradient, mint accent and mint buttons of airrp.dsrdt.com, with **saffron** (`#FF9933`) for anything that means *talk to us* and for the section labels. The approved AIRRP logo (gold mark) comes from the brand assets. Inter is self-hosted (SIL OFL), so there are no third-party fonts, analytics or trackers. V2 asks for no unnecessary animation, so there is none beyond hover feedback.

```
website/
  public/              static site (index.html, privacy.html, 404.html, assets/, _headers)
  public/assets/product/   genuine AIRRP development captures used as product proof
  src/enquiry.js       form API: validation, Turnstile, team email, acknowledgement, optional CRM webhook
  worker.js            Cloudflare Worker: /api/enquiry-config, /api/enquiry, everything else from public/
  test/                node:test tests for the form API and Worker routing
  scripts/check-site.mjs   V2 acceptance checks and release gates
  release-gates.json   owner sign-offs required before `npm run deploy` will publish
  QA-EVIDENCE.md, qa/  V2 §9 QA evidence sheet with desktop/mobile screenshots
```

## Run it

```bash
cd website
npm test                      # form API + Worker routing tests (Node 20+, no install needed)
npm run check                 # V2 acceptance checks; warns about pending pricing, proof and open gates
python3 -m http.server -d public 8080   # static preview: the form falls back to the visitor's email app
npm install && npx wrangler dev         # Worker preview (configure secrets first, see below)
npm run preview               # separate workers.dev URL for content approval (wrangler.preview.toml)
npm run deploy                # replaces the "airrp-website" Worker on airrp.dsrdt.com, only once every release check passes
```

## Product proof

V2 needs one genuine product example per capability. Two are in place, both cropped from development captures already published on the current site (synthetic test data):

| Capability | Proof | Status |
| --- | --- | --- |
| 03 Enterprise Risk Register | `assets/product/risk-register.webp`: a risk record showing its source finding and assessment | In use; a capture that also shows owner, decision and treatment state would match V2 more closely |
| 05 Governance Document Intelligence | `assets/product/governed-document.webp`: “What AIRRP knows”, the facts used, and facts to confirm | In use |
| 01, 02, 04, 06 | — | Pending. Each card says what its proof will show. |

To add one, put the full capture and a cropped thumbnail in `public/assets/product/`. Then replace that card’s `<figure class="proof proof-pending" data-pending-proof>` with the markup used on card 03: a link to the full capture with `data-viewer` and `data-caption`, a thumbnail with descriptive `alt`, and a one-line `figcaption`. Use synthetic or redacted data only. Clicking a thumbnail opens the full capture in a viewer.

## Request Demo / Contact Us routing

When the Worker is configured, a submission is:
1. checked: same origin, JSON only, size limit, field rules, a hidden honeypot field, and Cloudflare Turnstile verified on the server;
2. emailed to the monitored inbox `ALERT_TO_EMAIL`. The subject starts `[AIRRP Request Demo]` or `[AIRRP Contact Us]`, the email carries a `request_type` tag, and Reply-To is the visitor;
3. optionally posted to `CRM_WEBHOOK_URL`;
4. acknowledged to the visitor on screen and by a fixed-text email that never repeats visitor-supplied text.

If team delivery fails, the visitor sees an error and the email address, never a false success. Submissions are not stored.

| Setting | Required | Purpose |
| --- | --- | --- |
| `PUBLIC_ENQUIRY_ENABLED` | yes | `true` switches the form on |
| `RESEND_API_KEY`, `FROM_EMAIL` | yes | transactional email and a verified sender |
| `ALERT_TO_EMAIL` | yes | monitored team inbox |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET` | yes | Turnstile widget for the site and preview hostnames |
| `SEND_ACKNOWLEDGEMENT` | no | `false` skips the visitor email |
| `CRM_WEBHOOK_URL`, `CRM_WEBHOOK_SECRET` | no | also send each request to an approved CRM |

These are the same setting names the existing site uses. Until all the required ones are set, the button reads **Continue in email** and opens a pre-filled email to airrp@dsrdt.com, so it never does nothing.

## Pricing

`pricing.approved.json` holds the pricing approved by the product owner (19 October 2026), and the table in `public/index.html` (between `PRICING:START` and `PRICING:END`) is written from it:

| Charging unit | Price | Per | Term |
| --- | --- | --- | --- |
| Common compliance programs | ₹8,00,000 | Program | Annually |
| Privacy, regulatory and specialised programs | ₹10,00,000 | Program | Annually |
| NIST SP 800-53 | ₹25,00,000 | Program | Annually |
| Governed applications | ₹10,000 | Application | Annually |
| Governed third parties | ₹20,000 | Vendor | Annually |
| Professional services | Quoted separately | | |

Also shown, word for word: *Unlimited users and reassessments*; *No separate legal-entity fee, hidden platform fee or per-control, evidence, finding, risk or document charge*; *Founding-customer discount: 20% on eligible compliance programs only*.

`npm run check` fails if:
- any row differs from the JSON;
- any other price appears anywhere on the page (so discounted figures are never worked out and shown);
- a minimum, tier, volume discount or other percentage is added;
- a required inclusion line is missing.

To change pricing, update the JSON and the table together. No capability entitlements are attached to any charging unit. The page only says the included capabilities are confirmed in the licence scope.

## Release gates

`npm run deploy` runs `check-site.mjs --release` and refuses to publish while any of these is open:

| Gate | Owner confirms |
| --- | --- |
| `pricingApproved` | the rendered pricing section matches the approved pricing (the table is generated from it and checked automatically; this is the owner’s final look) |
| `tprmEndToEndValidated` | TPRM end-to-end test passed. Only then add the TPRM capture and show connected-example step 5 (remove its `hidden`). |
| `productProofApproved` | all six proof captures are genuine, use synthetic/redacted data and are approved for publication |
| `connectedExampleVerified` | the HR system example is an actually tested AIRRP scenario and the steps match what the product does |
| `privacyNoticeApproved` | `public/privacy.html` is the legally approved notice |
| `formTestedEndToEnd` | a real submission of each request type reached the inbox/CRM correctly labelled and the visitor got the acknowledgement |
| `buyerComprehensionTested` | the V2 10–15 s / 30–45 s comprehension test was run and unclear points fixed (script in `QA-EVIDENCE.md`) |

## What is checked automatically

- `npm run check`:
  - structure: one page with three areas, the four nav items, the four hero differentiators with cues, six named cards each with the four V2 parts and a four-step workflow;
  - content order;
  - the connected example has four or more visible steps and the TPRM step stays hidden while its gate is open;
  - the value table sits directly before Pricing;
  - TPRM is kept separate from questionnaires;
  - form fields and privacy link;
  - no superlatives, competitor names, “How AIRRP works”, “AI-powered”, blog/testimonials or unapproved prices;
  - anchors and files resolve, captures have descriptive alt text, and the CSP allows every inline script.
- `npm test`: 17 form-API and routing tests.
- Browser (see `QA-EVIDENCE.md`): 1440 to 320 px with no horizontal scroll; 40 interaction checks under the production CSP; axe-core WCAG 2.1 AA with no violations.
