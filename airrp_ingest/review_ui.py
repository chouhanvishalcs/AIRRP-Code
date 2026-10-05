"""Local reviewer console: approve/reject suggested mappings, map what the suggester missed, curate master controls.

Binds to 127.0.0.1 only, requires a random per-run token, and rejects foreign Host headers (DNS rebinding).
Every decision goes through the same Repository methods (and schema constraints) as the CLI.
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .masters import DuplicateSuspect, create_master
from .store import open_repository

MAX_BODY = 1 << 20


def _rows(rows):
    return [dict(r) for r in rows]


class _Handler(BaseHTTPRequestHandler):
    server_version = "airrp-review"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    # -- plumbing ---------------------------------------------------------------------------------
    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, query):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "bad host"})
            return False
        supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if not secrets.compare_digest(supplied, self.server.token):
            self._send(403, {"error": "missing or wrong token"})
            return False
        return True

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("expected application/json")
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes -----------------------------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._guard(q):
            return
        repo, fw = self.server.repo, self.server.framework
        arg = lambda k, d="": (q.get(k) or [d])[0]
        page = dict(limit=min(int(arg("limit", "25")), 100), offset=int(arg("offset", "0")))
        if url.path == "/":
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html")
        if url.path == "/api/summary":
            return self._send(200, {
                "framework": f"{fw[0]}@{fw[1]}", **repo.coverage(*fw),
                "open_suggestions": len(repo.open_suggestions()), "waiting_on_master_approval": repo.blocked_suggestions(),
                "masters": {r[0]: int(r[1]) for r in repo.fetchall("SELECT status, COUNT(*) FROM master_control GROUP BY 1")},
                "open_review_items": int(repo.fetchone("SELECT COUNT(*) FROM review_item WHERE status='open'")[0])})
        if url.path == "/api/queue":
            return self._send(200, _rows(repo.queue(arg("q"), **page)))
        if url.path == "/api/unmapped":
            return self._send(200, _rows(repo.unmapped(*fw, arg("q"), **page)))
        if url.path == "/api/masters":
            return self._send(200, _rows(repo.master_controls()))
        if url.path == "/api/review-items":
            return self._send(200, _rows(repo.fetchall(
                "SELECT control_id, code, severity, status, message FROM review_item WHERE status<>'resolved' ORDER BY id")))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._guard(parse_qs(url.query)):
            return
        repo, fw = self.server.repo, self.server.framework
        try:
            b = self._json_body()
            who = (b.get("reviewer") or "").strip()
            if not who:
                raise ValueError("enter your name as reviewer first")
            if url.path == "/api/decide":
                repo.decide_mapping(int(b["mapping_id"]), b["decision"], who, b.get("relationship"),
                                    b.get("rationale", ""), bool(b.get("primary")))
            elif url.path == "/api/map":
                repo.map_requirement(*fw, b["control_id"], b["master_id"], who, b.get("relationship"),
                                     b.get("rationale", ""), bool(b.get("primary")))
            elif url.path == "/api/master":
                mid = create_master(repo, who, **{k: b.get(k, "") for k in (
                    "name", "objective", "description", "domain", "frequency", "control_type", "evidence",
                    "test_procedure")}, distinct_from_reason=b.get("distinct_reason", ""))
                return self._send(200, {"id": mid})
            elif url.path == "/api/master/approve":
                repo.approve_master_control(b["id"], who)
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except DuplicateSuspect as exc:
            self._send(409, {"error": "looks like an existing master control; add a reason if it is really distinct",
                             "candidates": [{"id": i, "score": s} for i, s, _ in exc.candidates]})
        except Exception as exc:  # constraint violations, validation errors, bad input: all reviewer-facing
            self._send(400, {"error": str(exc)})


def make_server(db, framework_code: str, version: str, port: int = 8765, token: str = None,
                four_eyes: bool = True) -> HTTPServer:
    """``db`` is a repository instance, a SQLite path or a postgresql:// URL."""
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    httpd.repo = open_repository(db, four_eyes=four_eyes, check_same_thread=False) if isinstance(db, str) else db
    httpd.framework = (framework_code, version)
    httpd.token = token or secrets.token_urlsafe(24)
    return httpd


def serve(db_path, framework_code, version, port=8765, four_eyes=True):
    httpd = make_server(db_path, framework_code, version, port, four_eyes=four_eyes)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={httpd.token}"
    print(f"reviewer console: {url}\n(Ctrl+C to stop; the token changes on every start)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIRRP Review Console</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#dcdcd6;--acc:#1f5fbf;--ok:#1b7f4d;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--card:#222220;--ink:#ecece8;--mute:#9a9a94;--line:#3a3a36;--acc:#7aa7ee;--ok:#5fcf93;--bad:#ff8a80;--warn:#e0b050}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:2}
h1{font-size:17px;margin:0 0 8px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
nav button{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 12px;border-radius:6px;cursor:pointer}
nav button[aria-pressed=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
main{max-width:1100px;margin:0 auto;padding:16px}
input,select,textarea,button{font:inherit;color:inherit}
input,select,textarea{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
textarea{width:100%;min-height:56px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:0 0 14px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.cols{grid-template-columns:1fr}}
.lbl{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.txt{white-space:pre-wrap;margin:4px 0 0;max-height:340px;overflow:auto}.mute{color:var(--mute);font-size:13px}.tag{font-size:12px;border:1px solid var(--line);border-radius:10px;padding:1px 8px;color:var(--mute)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
button.go{background:var(--ok);color:#fff;border:0;padding:7px 14px;border-radius:6px;cursor:pointer}
button.no{background:transparent;color:var(--bad);border:1px solid var(--bad);padding:6px 12px;border-radius:6px;cursor:pointer}
#msg{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);padding:8px 14px;border-radius:8px;background:var(--ink);color:var(--bg);display:none;max-width:90vw}
.warn{color:var(--warn)}
</style></head><body>
<header><h1>AIRRP Review Console <span class="mute" id="fw"></span></h1>
<div class="row"><nav class="row" id="tabs"></nav>
<label class="row mute">Reviewer <input id="who" placeholder="your name" size="16"></label>
<input id="q" placeholder="search…" size="18"></div></header>
<main id="main"></main><div id="msg" role="status"></div>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
const $=(s,r=document)=>r.querySelector(s);
const el=(tag,props={},...kids)=>{const e=document.createElement(tag);Object.assign(e,props);kids.flat().forEach(k=>e.append(k));return e};
let tab='queue',summary={};
try{$('#who').value=localStorage.getItem('reviewer')||''}catch(e){}
$('#who').onchange=()=>{try{localStorage.setItem('reviewer',$('#who').value)}catch(e){}};
const who=()=>$('#who').value.trim();
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Token':T,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  const j=await r.json();if(!r.ok){const e=new Error(j.error||r.statusText);e.data=j;throw e}return j}
function say(t,bad){const m=$('#msg');m.textContent=t;m.style.background=bad?'var(--bad)':'var(--ink)';m.style.display='block';clearTimeout(say.t);say.t=setTimeout(()=>m.style.display='none',4000)}
const REL=['equivalent','subset','superset','intersects'];
function decisionBox(onSubmit,{reject}={}){
  const rel=el('select',{},el('option',{value:''},'relationship…'),REL.map(r=>el('option',{value:r},r)));
  const why=el('textarea',{placeholder:'Rationale (required): why does this requirement map to this control, and how strongly?'});
  const prim=el('input',{type:'checkbox'});
  const go=el('button',{className:'go',textContent:'Approve mapping',onclick:()=>onSubmit({decision:'approved',relationship:rel.value,rationale:why.value,primary:prim.checked})});
  const kids=[why,el('div',{className:'actions'},rel,el('label',{className:'mute'},prim,' primary mapping'),go)];
  if(reject)kids[1].append(el('button',{className:'no',textContent:'Reject',onclick:()=>onSubmit({decision:'rejected',rationale:why.value})}));
  return el('div',{},kids)}
function req(r){return el('div',{},el('div',{className:'lbl'},'Requirement'),el('strong',{},r.control_id+' — '+r.title),el('div',{className:'txt'},r.statement||''))}
async function guarded(fn){if(!who()){say('Enter your name as reviewer first',true);$('#who').focus();return}
  try{await fn();await refresh()}catch(e){if(e.data&&e.data.candidates)say(e.message+': '+e.data.candidates.map(c=>c.id+' ('+c.score+')').join(', '),true);else say(e.message,true)}}
async function viewQueue(){
  const rows=await api('/api/queue?limit=25&q='+encodeURIComponent($('#q').value));
  if(!rows.length)return el('p',{className:'mute'},'No suggestions ready. Run "suggest" after approving master controls, or use the Unmapped tab.');
  return rows.map(r=>el('div',{className:'card'},el('div',{className:'cols'},req(r),
    el('div',{},el('div',{className:'lbl'},'Suggested master control (candidate only)'),el('strong',{},r.master),
      el('div',{className:'mute'},[r.domain,r.frequency,r.control_type].join(' · ')),
      el('div',{className:'txt'},r.objective),el('div',{className:'mute txt'},r.description),
      el('div',{className:'mute'},'score '+(r.score==null?'—':r.score)+(r.evidence?' · shared terms: '+r.evidence:'')))),
    decisionBox(d=>guarded(async()=>{await api('/api/decide',{mapping_id:r.id,reviewer:who(),...d});say(d.decision+' '+r.control_id)}),{reject:true})))}
async function viewUnmapped(){
  const [rows,masters]=await Promise.all([api('/api/unmapped?limit=25&q='+encodeURIComponent($('#q').value)),api('/api/masters')]);
  const ok=masters.filter(m=>m.status==='approved');
  if(!rows.length)return el('p',{className:'mute'},'Every active requirement has an approved mapping.');
  return rows.map(r=>{const pick=el('select',{},el('option',{value:''},'master control…'),ok.map(m=>el('option',{value:m.id},m.name)));
    const box=decisionBox(d=>guarded(async()=>{if(!pick.value)throw new Error('pick a master control');
      await api('/api/map',{control_id:r.control_id,master_id:pick.value,reviewer:who(),...d});say('mapped '+r.control_id)}));
    return el('div',{className:'card'},req(r),el('div',{className:'actions'},pick),box)})}
async function viewMasters(){
  const ms=await api('/api/masters');const f={};
  const mk=(k,ph,area)=>f[k]=el(area?'textarea':'input',{placeholder:ph});
  const form=el('div',{className:'card'},el('strong',{},'New master control (draft)'),
    mk('name','Name'),mk('objective','Objective',1),mk('description','Description',1),mk('domain','Domain (e.g. IdentityAndAccess)'),
    mk('frequency','Frequency (e.g. Continuous)'),mk('control_type','Type (Preventive/Detective/Corrective)'),
    mk('evidence','Evidence expected',1),mk('test_procedure','Test procedure',1),mk('distinct_reason','Only if it is near an existing control: why is it distinct?'),
    el('div',{className:'actions'},el('button',{className:'go',textContent:'Create draft',onclick:()=>guarded(async()=>{
      const b={reviewer:who()};for(const k in f)b[k]=f[k].value;await api('/api/master',b);say('draft created')})})));
  return [form,...ms.map(m=>el('div',{className:'card'},el('strong',{},m.name),' ',el('span',{className:'tag'},m.status),' ',el('span',{className:'mute'},m.id+' · '+[m.domain,m.frequency,m.control_type].join(' · ')),
    el('div',{className:'txt'},m.objective),
    JSON.parse(m.quality_flags||'[]').length?el('div',{className:'warn'},'Needs work before approval: '+JSON.parse(m.quality_flags).join(', ')+' — fix with "master todo" / "master apply-csv"'):'',
    m.status==='draft'&&!JSON.parse(m.quality_flags||'[]').length?el('div',{className:'actions'},el('button',{className:'go',textContent:'Approve (needs a second person)',onclick:()=>guarded(async()=>{await api('/api/master/approve',{id:m.id,reviewer:who()});say('approved')})})):''))]}
async function viewSummary(){
  const [s,items]=await Promise.all([api('/api/summary'),api('/api/review-items')]);
  return [el('div',{className:'card'},el('div',{className:'lbl'},s.framework),
    el('p',{},`${s.with_approved_mapping} of ${s.active_requirements} active requirements have an approved mapping.`),
    el('p',{},`${s.open_suggestions} suggestion(s) ready to review · ${s.waiting_on_master_approval} waiting for their master control to be approved · masters: ${JSON.stringify(s.masters)} · ${s.open_review_items} open source issue(s)`)),
    el('div',{className:'card'},el('strong',{},'Quarantined / open source issues'),...items.map(i=>el('div',{className:'mute'},`${i.control_id} · ${i.code} · ${i.status} — ${i.message}`)))]}
const VIEWS={queue:['Review queue',viewQueue],unmapped:['Unmapped',viewUnmapped],masters:['Master controls',viewMasters],summary:['Summary',viewSummary]};
async function refresh(){
  $('#tabs').replaceChildren(...Object.entries(VIEWS).map(([k,[n]])=>el('button',{textContent:n,onclick:()=>{tab=k;refresh()}, ariaPressed:String(k===tab)})));
  try{summary=await api('/api/summary');$('#fw').textContent=summary.framework+' · '+summary.with_approved_mapping+'/'+summary.active_requirements+' mapped · '+summary.open_suggestions+' to review'}catch(e){say(e.message,true)}
  try{$('#main').replaceChildren(...[].concat(await VIEWS[tab][1]()))}catch(e){$('#main').replaceChildren(el('p',{className:'warn'},e.message))}}
$('#q').oninput=()=>{clearTimeout(refresh.t);refresh.t=setTimeout(refresh,250)};
refresh();
</script></body></html>
"""
