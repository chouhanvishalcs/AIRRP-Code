# QA evidence sheet: Website Build Brief V2

Preview build on branch `claude/gallant-keller-bhjma2`. Screenshots are in [`qa/`](qa/) (desktop 1440 px, mobile 390 px). Status key:

- **Met:** done in the build and checked automatically by `npm run check`.
- **Owner:** needs a decision or sign-off from the product owner.
- **Pending:** waiting on a genuine product capture or verification.

| # | V2 mandatory requirement | Where it appears | Desktop / mobile evidence | Product example supporting the claim | Status |
|---|---|---|---|---|---|
| 1 | Scope preserved: one page; only Capabilities, Pricing, Demo/Contact | Fixed header; `#capabilities`, `#pricing`, `#contact` | [desktop-nav](qa/desktop-nav.webp) · [mobile-nav](qa/mobile-nav.webp) | — | Met |
| 2 | Immediate differentiation: four truthful differentiators near the hero, nothing to expand | “Why AIRRP is different” panel beside the headline (`#why-different`); all four start above the fold at 1440×900 and 1366×768 | [desktop-hero](qa/desktop-hero.webp) · [mobile-hero](qa/mobile-hero.webp) | Each statement has a workflow cue linking to its evidence: context → connected example; govern once → Governance Documents capture; explain why → Compliance card; connect outcomes → Risk Register capture | Met (wording is V2’s suggested text, “subject to product-proof verification”) |
| 3 | Six capabilities complete: what / how differently / why a customer would choose it / one product proof | Six cards (`#cap-*`), all visible, no accordions; index chips above them | [desktop-capabilities](qa/desktop-capabilities.webp) · [desktop-card-proof](qa/desktop-card-proof.webp) · [desktop-card-pending](qa/desktop-card-pending.webp) · [mobile-card-proof](qa/mobile-card-proof.webp) | See row 4 | Met for structure; proof pending on 4 of 6 |
| 4 | Proof is authentic: real behaviour, subject and outcome identified, synthetic/redacted data | “Product proof” block in each card; full capture opens in a viewer | [desktop-card-proof](qa/desktop-card-proof.webp) · [mobile-card-proof](qa/mobile-card-proof.webp) | **03 Risk Register:** development capture showing a risk record and its source (“1 finding from 1 assessment”) with inherent/current rating. It does not show the owner, recorded decision or treatment state that V2 asks for, so a fuller capture is preferred.<br>**05 Governance Documents:** development capture showing “What AIRRP knows” and the governed facts used before drafting a privacy notice, plus “Facts to confirm” (human input).<br>**01, 02, 04, 06:** capture pending; each card states what the proof will show. | Pending (4 captures + approval of the 2 in use) |
| 5 | Connected workflow: one concrete scenario across understanding, obligations, assessment/findings, risk decisions and reuse; TPRM only if verified | “One connected example” (`#connected-example`): horizontal on desktop, vertical on mobile, four steps plus takeaway. The TPRM step exists but is hidden. | [desktop-connected](qa/desktop-connected.webp) · [mobile-connected](qa/mobile-connected.webp) | V2’s suggested Employee HR Management System, with the synthetic organisation name from the brand guidelines. Not yet confirmed as an actually tested scenario, and no step screenshots yet. | Owner (confirm scenario tested; supply step captures) |
| 6 | Value before price: buyer-oriented comparison directly before the pricing table | “Why connected work matters” (`#why-connected`) is the last block of Capabilities, directly followed by Pricing; the V2 table word for word plus the commercial message | [desktop-value](qa/desktop-value.webp) · [mobile-value](qa/mobile-value.webp) | — (no ROI figures or competitor claims) | Met |
| 7 | Correct capability boundaries: TPRM vs customer questionnaires | Direction pills on cards 04 and 06, plus the “Two directions of assurance” panel | [desktop-boundaries](qa/desktop-boundaries.webp) · [mobile-boundaries](qa/mobile-boundaries.webp) | — | Met |
| 8 | Pricing integrity: matches the approved source exactly | `#pricing` table between `PRICING:START/END` | [desktop-pricing](qa/desktop-pricing.webp) · [mobile-pricing](qa/mobile-pricing.webp) | Table matches the owner-approved `pricing.approved.json` (19 Oct 2026). `npm run check` rejects any other price, computed discount figure, minimum, tier, volume discount or extra percentage. No capability entitlements are attached to charging units. | Met in build; owner’s final look pending (`pricingApproved`) |
| 9 | Working actions: end-to-end delivery, success/failure, acknowledgement | `#contact` form → Worker `/api/enquiry` | [desktop-form](qa/desktop-form.webp) · [mobile-form](qa/mobile-form.webp) | 17 API tests and a 40-check browser suite (success, failure, request-type routing, email fallback). A real submission needs the Cloudflare/Resend/Turnstile settings. | Owner (configure + live test) |
| 10 | Responsive and accessible | Whole page | Checked at 1440, 1366, 900, 390, 360 and 320 px with no horizontal scroll | axe-core WCAG 2.1 AA: 0 violations on all pages; keyboard menu, focus handling, viewer Escape/focus return, alt text on every capture | Met |
| 11 | Truthful release: TPRM check complete before TPRM proof or promises | Card 04 uses the brief’s copy only; its proof is pending; the connected-example TPRM step is hidden; `npm run check` fails if that step is shown while the gate is open | — | **TPRM proof still awaiting verification:** (a) end-to-end TPRM test, (b) TPRM card capture, (c) TPRM step in the connected example | Pending |
| 12 | Buyer comprehension test (10–15 s / 30–45 s) | — | — | Not run yet; needs a person unfamiliar with AIRRP. Use the script below and record the results here. | Owner |

## TPRM items still awaiting verification (not passed)

1. TPRM end-to-end test of relationship → business context → assess and review → track risk.
2. Genuine TPRM capture for card 04 (currently “Capture pending”).
3. The hidden TPRM step 5 in the connected example. Show it only after 1 passes, by removing `hidden` and setting `tprmEndToEndValidated`.

## Buyer comprehension test script

Show the desktop page and, separately, the mobile page to someone unfamiliar with AIRRP. Do not explain anything.

| Time | Ask | Pass if they can say… | Result / unclear points |
|---|---|---|---|
| 10–15 s | “What does AIRRP do differently?” | At least one of: starts from enterprise context; govern once and reuse; explains why; connects findings to risk | |
| 30–45 s | “Describe one connected workflow.” | The HR system example: context → obligations → assessment/risk decision → reuse in documents/questionnaires | |
| 30–45 s | “Why could that justify the price?” | Less repeat collection, consistent coverage, traceable decisions, clearer accountability | |

Record the outcome, fix anything unclear, then set `buyerComprehensionTested` in `release-gates.json`.

## How this sheet was produced

```bash
cd website
npm test              # 17 form-API tests
npm run check         # V2 structure, order, proof blocks, TPRM gating, banned phrases, links, CSP
```

Browser checks were run with Playwright (Chromium) under the production CSP from `public/_headers`, plus axe-core 4.10.
