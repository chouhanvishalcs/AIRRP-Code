// AIRRP product site: navigation, scroll-spy, product capture viewer, and the Request Demo / Contact Us form.
(() => {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const TEAM_EMAIL = 'airrp@dsrdt.com';
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- header ---------- */
  const header = $('.site-header');
  const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 8);
  onScroll();
  addEventListener('scroll', onScroll, { passive: true });

  /* ---------- mobile menu ---------- */
  const toggle = $('.menu-toggle');
  const menu = $('#site-menu');
  const setMenu = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    menu.classList.toggle('is-open', open);
  };
  toggle.addEventListener('click', () => setMenu(toggle.getAttribute('aria-expanded') !== 'true'));
  menu.addEventListener('click', (e) => { if (e.target.closest('a')) setMenu(false); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu.classList.contains('is-open')) { setMenu(false); toggle.focus(); }
  });
  matchMedia('(min-width: 961px)').addEventListener('change', (e) => { if (e.matches) setMenu(false); });

  /* ---------- scroll-spy ---------- */
  const spyLinks = $$('[data-spy]');
  if ('IntersectionObserver' in window) {
    const watched = [$('.hero'), ...spyLinks.map((a) => document.getElementById(a.dataset.spy))].filter(Boolean);
    const spy = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        for (const a of spyLinks) a.classList.toggle('is-active', a.dataset.spy === entry.target.id);
      }
    }, { rootMargin: '-45% 0px -50% 0px' });
    watched.forEach((el) => spy.observe(el));
  }

  /* ---------- product capture viewer ---------- */
  // Each proof thumbnail links to the full capture; with JavaScript it opens in a dialog instead of a new page.
  const viewer = $('#proof-viewer');
  if (viewer && typeof viewer.showModal === 'function') {
    const viewerImg = $('#viewer-img');
    const viewerCaption = $('#viewer-caption');
    let opener = null;
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a[data-viewer]');
      if (!link) return;
      e.preventDefault();
      opener = link;
      const thumb = link.querySelector('img');
      viewerImg.src = link.getAttribute('href');
      viewerImg.alt = thumb ? thumb.alt : '';
      viewerCaption.textContent = link.dataset.caption || '';
      viewer.showModal();
    });
    viewer.addEventListener('click', (e) => { if (e.target === viewer) viewer.close(); });
    viewer.addEventListener('close', () => { viewerImg.removeAttribute('src'); opener?.focus(); });
  }

  /* ---------- lead form ---------- */
  const form = $('#lead-form');
  if (!form) return;
  const statusBox = $('#form-status');
  const submit = $('#lead-submit');
  const submitLabel = $('[data-submit-label]');
  const modeNote = $('#form-mode');
  const success = $('#form-success');
  const successDetail = $('#success-detail');
  const message = $('#f-message');
  const fields = { name: $('#f-name'), email: $('#f-email'), organisation: $('#f-org') };
  const fieldMessages = {
    name: 'Please enter your name.',
    email: 'Please enter a valid work email address.',
    organisation: 'Please enter your organisation.',
  };
  let mode = 'checking'; // 'secure' (API + Turnstile) or 'email' (opens the visitor's mail app)
  let widgetId = null;

  const requestType = () => (form.elements.requestType.value === 'Contact Us' ? 'Contact Us' : 'Request Demo');
  const syncLabel = () => {
    if (mode === 'email') submitLabel.textContent = 'Continue in email';
    else submitLabel.textContent = requestType() === 'Request Demo' ? 'Request demo' : 'Send message';
  };
  form.addEventListener('change', (e) => { if (e.target.name === 'requestType') syncLabel(); });

  // Any link marked data-request pre-selects the request type (and optional topic) before scrolling to the form.
  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-request]');
    if (!link) return;
    const value = link.dataset.request === 'contact' ? 'Contact Us' : 'Request Demo';
    const radio = form.querySelector(`input[name="requestType"][value="${value}"]`);
    if (radio) { radio.checked = true; syncLabel(); }
    if (link.dataset.topic && !message.value.trim()) message.value = link.dataset.topic;
    if (matchMedia('(hover: hover)').matches && !form.hidden) {
      setTimeout(() => fields.name.focus({ preventScroll: true }), reducedMotion ? 0 : 500);
    }
  });

  const collect = () => ({
    requestType: requestType(),
    name: fields.name.value.trim(),
    email: fields.email.value.trim(),
    organisation: fields.organisation.value.trim(),
    message: message.value.trim(),
  });

  const emailLooksValid = (v) => /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]{2,}$/.test(v) && v.length <= 180;
  const checkField = (key) => {
    const input = fields[key];
    const value = input.value.trim();
    const ok = key === 'email' ? emailLooksValid(value) : value.length >= 2;
    input.setAttribute('aria-invalid', ok ? 'false' : 'true');
    document.getElementById(input.getAttribute('aria-describedby')).textContent = ok ? '' : fieldMessages[key];
    return ok;
  };
  const validate = () => {
    let firstInvalid = null;
    for (const key of Object.keys(fields)) {
      if (!checkField(key) && !firstInvalid) firstInvalid = fields[key];
    }
    if (firstInvalid) firstInvalid.focus();
    return !firstInvalid;
  };
  for (const key of Object.keys(fields)) {
    fields[key].addEventListener('input', () => { if (fields[key].getAttribute('aria-invalid') === 'true') checkField(key); });
    fields[key].addEventListener('blur', () => { if (fields[key].value.trim()) checkField(key); });
  }

  const mailtoHref = () => {
    const d = collect();
    const subject = `AIRRP ${d.requestType}${d.organisation ? ` — ${d.organisation}` : ''}`;
    const body = [
      `Request type: ${d.requestType}`,
      `Name: ${d.name}`,
      `Work email: ${d.email}`,
      `Organisation: ${d.organisation}`,
      '',
      d.message,
    ].join('\n');
    return `mailto:${TEAM_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  };

  const setStatus = (kind, ...parts) => {
    statusBox.className = `form-status full${kind ? ` is-${kind}` : ''}`;
    statusBox.replaceChildren(...parts);
  };
  const mailLink = (label) => {
    const a = document.createElement('a');
    a.href = mailtoHref();
    a.textContent = label;
    return a;
  };

  const useEmailMode = () => {
    mode = 'email';
    modeNote.textContent = 'Online submission isn’t switched on here yet. Your email app will open with your request filled in.';
  };

  const loadTurnstile = (siteKey) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Turnstile timeout')), 10000);
    window.airrpTurnstileReady = () => {
      try {
        widgetId = window.turnstile.render('#turnstile-slot', {
          sitekey: siteKey, action: 'enquiry', appearance: 'interaction-only', theme: 'light',
        });
        clearTimeout(timer);
        resolve();
      } catch (err) { clearTimeout(timer); reject(err); }
    };
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=airrpTurnstileReady';
    script.async = true;
    script.onerror = () => { clearTimeout(timer); reject(new Error('Turnstile failed to load')); };
    document.head.append(script);
  });

  const init = async () => {
    syncLabel();
    submit.disabled = true;
    modeNote.textContent = 'Preparing the secure form…';
    try {
      const res = await fetch('/api/enquiry-config', { cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error('config unavailable');
      const cfg = await res.json();
      if (!cfg.ready || !cfg.siteKey) throw new Error('form not enabled');
      await loadTurnstile(cfg.siteKey);
      mode = 'secure';
      modeNote.textContent = '';
    } catch {
      useEmailMode();
    }
    syncLabel();
    submit.disabled = false;
  };

  const showSuccess = (d, out) => {
    const what = d.requestType === 'Request Demo' ? 'demo request' : 'message';
    successDetail.textContent = out.acknowledged
      ? `Your ${what} has reached the AIRRP team. We’ve sent an acknowledgement to ${d.email}.`
      : `Your ${what} has reached the AIRRP team. We’ll reply to ${d.email}.`;
    form.reset();
    if (widgetId !== null) window.turnstile?.reset(widgetId);
    setStatus('');
    form.hidden = true;
    success.hidden = false;
    $('#success-title').focus();
  };

  $('#success-reset').addEventListener('click', () => {
    success.hidden = true;
    form.hidden = false;
    syncLabel();
    fields.name.focus();
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    setStatus('');
    if (!validate()) { setStatus('error', 'Please complete the highlighted fields.'); return; }

    if (mode === 'email') {
      window.location.href = mailtoHref();
      setStatus('info', 'Your email app should open with the request filled in — please press send there. If nothing opens, write to ', mailLink(TEAM_EMAIL), '.');
      return;
    }
    if (mode !== 'secure') return;

    const token = window.turnstile?.getResponse(widgetId);
    if (!token) { setStatus('info', 'Just a moment — the security check is still running. Please try again in a few seconds.'); return; }

    const payload = { ...collect(), website: form.elements.website.value, turnstileToken: token };
    submit.disabled = true;
    submitLabel.textContent = 'Sending…';
    try {
      const res = await fetch('/api/enquiry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload),
      });
      let out = {};
      try { out = await res.json(); } catch { /* non-JSON error page */ }
      if (!res.ok || !out.ok) {
        setStatus('error', `${out.message || 'Your request was not sent.'} You can also email `, mailLink(TEAM_EMAIL), ' with the same details.');
        window.turnstile?.reset(widgetId);
        return;
      }
      showSuccess(payload, out);
    } catch {
      setStatus('error', 'We couldn’t confirm that your request was sent. Please check your connection and try again, or email ', mailLink(TEAM_EMAIL), '.');
      window.turnstile?.reset(widgetId);
    } finally {
      submit.disabled = false;
      syncLabel();
    }
  });

  init();
})();
