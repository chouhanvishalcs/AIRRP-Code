# AIRRP product website

A single, capability-led page built from the *AIRRP Website Build Brief (Focused)*. It has three content areas, reached from a fixed navigation bar: **Capabilities | Pricing | Request Demo | Contact Us**.

| Area | What is on it |
| --- | --- |
| Headline (not a section) | “AIRRP: Connected Compliance, Risk and Governance”, the two calls to action, and a map of the six capabilities around one governed foundation (each node links to its card) |
| Capabilities | The three themes (context-aware work, governed reuse, traceable decisions), then six cards. Each card shows *what it does → how it works differently → why it’s useful* plus a four-step workflow, using the brief’s copy word for word. A separate panel keeps TPRM (*you assess your third parties*) apart from Questionnaire Assistance (*your customers assess you*). |
| Pricing | A table of charging units with columns for what’s included, term, volume conditions and price, plus a note on why connected capabilities add value and an “Ask about pricing” link to the form. **Figures are pending (see below).** |
| Request Demo / Contact Us | One short form with a request-type switch. Name, work email and organisation are required; the message is optional. Includes a privacy statement and success/failure feedback. |

Look and feel: the midnight-teal gradient, mint headline accent and mint buttons of airrp.dsrdt.com, plus **saffron** (`#FF9933`) as the accent for anything that means *talk to us* (Request Demo, form submit, Ask about pricing) and for the overlines. The approved AIRRP logo (gold mark) comes from the brand assets. Inter is self-hosted (SIL OFL, `public/assets/fonts/OFL-Inter.txt`), so the page loads no third-party fonts, analytics or trackers.

```
website/
  public/            static site served as-is (index.html, privacy.html, 404.html, assets/, _headers)
  src/enquiry.js     form API: validation, Turnstile, team email, acknowledgement, optional CRM webhook
  worker.js          Cloudflare Worker: /api/enquiry-config, /api/enquiry, everything else from public/
  test/              node:test tests for the form API and Worker routing (no dependencies)
  scripts/check-site.mjs   acceptance checks for the brief (§8) and the release gates
  release-gates.json       owner sign-offs required before `npm run deploy` will publish
```

## Run it

```bash
cd website
npm test                      # form API + Worker routing tests (Node 20+, no install needed)
npm run check                 # brief acceptance checks; warns about pending pricing and open gates
python3 -m http.server -d public 8080   # static preview: the form falls back to the visitor's email app
npm install && npx wrangler dev         # Worker preview (needs .dev.vars, see below)
```

Deployment uses the same Cloudflare setup as the current site:

```bash
npm run preview   # separate workers.dev URL for content approval (wrangler.preview.toml)
npm run deploy    # replaces the "airrp-website" Worker on airrp.dsrdt.com, but only once every release check passes
```

## Request Demo / Contact Us routing

When the Worker is configured, a submission is:

1. checked: same origin, JSON only, size limit, field rules, a hidden honeypot field, and Cloudflare Turnstile verified on the server (hostname and the `enquiry` action);
2. emailed to the monitored inbox `ALERT_TO_EMAIL`. The subject is `[AIRRP Request Demo] …` or `[AIRRP Contact Us] …`, the email carries a `request_type` tag, and Reply-To is the visitor, so the team can sort and reply directly;
3. optionally posted as JSON to `CRM_WEBHOOK_URL`;
4. acknowledged to the visitor on screen, and by a fixed-text email (no visitor-supplied text is echoed, so the form can’t be used to send spam from your domain).

If team delivery fails, the visitor sees an error and the email address. They never see a false success. The code does not store submissions.

Configuration (Cloudflare secrets/vars; the names match the existing site, so current values carry over):

| Name | Required | Purpose |
| --- | --- | --- |
| `PUBLIC_ENQUIRY_ENABLED` | yes | must be `true` to switch the form on |
| `RESEND_API_KEY` | yes | transactional email (Resend) |
| `FROM_EMAIL` | yes | verified sender, e.g. `AIRRP <no-reply@dsrdt.com>` |
| `ALERT_TO_EMAIL` | yes | monitored team inbox |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET` | yes | Turnstile widget for this hostname (add the preview hostname too) |
| `SEND_ACKNOWLEDGEMENT` | no | set `false` to skip the visitor email |
| `CRM_WEBHOOK_URL`, `CRM_WEBHOOK_SECRET` | no | also send each request to an approved CRM (Bearer auth) |

Until every required value is set, `/api/enquiry-config` reports `ready:false`. The button then reads **Continue in email** and opens a pre-filled email to airrp@dsrdt.com, so the button always does something (the brief’s “no non-working buttons”). Without JavaScript the form also falls back to `mailto:`.

## Populating pricing (required before release)

The brief says pricing must come from the approved source supplied by the product owner, and no figures may be invented. Until that source is supplied, the table shows the cost drivers already published on the current licensing page, with **Approved term / conditions / price** placeholders and a visible preview notice.

To publish:
1. Edit the rows between `<!-- PRICING:START -->` and `<!-- PRICING:END -->` in `public/index.html` to match the approved model exactly: charging units, annual term where applicable, what’s included, volume conditions, what needs a separate quotation. Add or remove rows as the model requires.
2. Remove every `data-pending` placeholder and the `data-preview-notice` paragraph.
3. Set `"pricingApproved": true` in `release-gates.json`.

## Release gates

`npm run deploy` runs `check-site.mjs --release` first and refuses to publish while any of these is open:

| Gate | Owner confirms |
| --- | --- |
| `pricingApproved` | pricing table matches the approved model exactly (brief §5, checklist 5) |
| `tprmEndToEndValidated` | TPRM end-to-end testing is complete and the TPRM card’s claims match the demonstrable workflow (brief §3.4 release note, checklist 7) |
| `privacyNoticeApproved` | `public/privacy.html` is replaced with the legally approved notice (the current text is the existing working draft, updated to describe this form) |
| `formTestedEndToEnd` | a real submission of each request type reached the inbox/CRM with the right type and the visitor got the acknowledgement (checklist 6) |

## What was checked

- Brief §8 checks run automatically by `npm run check`: one page with three areas, the four nav items, six named capabilities each with what/how/why and a four-step workflow, TPRM kept distinct from Questionnaire Assistance, required and optional form fields, privacy link, no superlatives/competitor names/blog/testimonials/unapproved prices, anchors and local files resolve, images have alt text, and the CSP hash matches the inline script.
- Form API: 17 tests (`npm test`) covering configuration gating, origin/content-type/size checks, validation, honeypot, Turnstile failure modes, routing by request type, acknowledgement content, delivery failure, the optional CRM webhook and header-injection attempts.
- In the browser: Chromium at 1440 / 900 / 390 px with no horizontal scrolling; the menu, scroll-spy, request-type pre-selection, validation and focus handling, email fallback, and secure-mode success/failure, all under the production CSP; an axe-core WCAG 2.1 AA scan with no violations on all pages.
