"""Local dashboard for SHADOW AI.

Serves one page and a small JSON API on 127.0.0.1 by default. It is a window
onto the running system, not a second control plane: the only write endpoints
are "run this goal" and "approve this paused task", both of which go through
the same Workflow Engine the CLI uses. Everything is served from memory, with
relative URLs, so it works behind a proxy or on another port unchanged.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..core.config import ShadowConfig
from ..runtime.orchestrator import ShadowAI
from ..util import jdump


class Dashboard:
    """Local window onto a running system.

    Loopback by default. A non-loopback bind is allowed (a container, a phone
    on the same network) but then every write needs the token: a dashboard
    that can type a password and approve a payment must not be open to the
    whole LAN just because someone passed `--host 0.0.0.0`.
    """

    def __init__(self, cfg: ShadowConfig, host: str = "127.0.0.1", port: int = 40330,
                 brain: Optional[ShadowAI] = None, token: str = ""):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.token = token
        self.read_token = ""
        # Fail at construction, not on the first request: a misconfigured bind
        # should be a startup error someone sees, not a silent exposure.
        if host not in ("127.0.0.1", "localhost", "::1") and not token:
            raise ValueError("refusing to serve a write-capable dashboard on "
                             f"{host} without a token: pass --token or bind 127.0.0.1")
        self.brain = brain or ShadowAI(cfg, printer=lambda line: None)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._worker: Optional[threading.Thread] = None
        self._last: Dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------
    def start(self) -> "Dashboard":
        self.brain.boot()
        loopback = self.host in ("127.0.0.1", "localhost", "::1")
        self.read_token = "" if loopback else self.token
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, payload: Any, status: int = 200) -> None:
                body = jdump(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if not dashboard.token or dashboard.host in ("127.0.0.1", "localhost", "::1"):
                    return True
                header = self.headers.get("X-Shadow-Token", "")
                if header == dashboard.token:
                    return True
                self._json({"error": "this dashboard needs X-Shadow-Token"}, 401)
                return False

            def _html(self, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path)
                if path.path in ("/", "/index.html"):
                    page = PAGE.replace("__SHADOW_TOKEN__", json.dumps(dashboard.read_token))
                    return self._html(page)
                if path.path == "/api/state":
                    return self._json(dashboard.state())
                if path.path == "/api/journal":
                    task = (parse_qs(path.query).get("task") or [""])[0]
                    entries = dashboard.brain.journal.entries(task_id=task)
                    return self._json([e.to_dict() for e in entries[-200:]])
                if path.path == "/api/fleet":
                    return self._json(dashboard.fleet_report())
                if path.path == "/api/schedule":
                    return self._json(dashboard.schedule_report())
                if path.path == "/api/health":
                    return self._json({"ok": True, "uptime_s": round(time.time() - dashboard.started, 1)})
                return self._json({"error": "not found"}, 404)

            def do_POST(self):
                if not self._authorized():
                    return
                path = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                except ValueError:
                    return self._json({"error": "invalid JSON"}, 400)
                if path.path == "/api/run":
                    goal = (body.get("goal") or "").strip()
                    if not goal:
                        return self._json({"error": "a goal is required"}, 400)
                    if dashboard.busy():
                        return self._json({"error": "a task is already running", **dashboard.last()}, 409)
                    dashboard.run_async(goal, approved=bool(body.get("approved")))
                    return self._json({"started": goal})
                if path.path == "/api/schedule":
                    action = (body.get("action") or "add").strip()
                    minutes = float(body.get("minutes") or 60)
                    if action == "add":
                        goal = (body.get("goal") or "").strip()
                        if not goal:
                            return self._json({"error": "a goal is required"}, 400)
                        job = dashboard.schedule_add(goal, minutes, body.get("name") or "")
                        return self._json(job)
                    job_id = (body.get("job_id") or "").strip()
                    ok = {"cancel": dashboard.schedule_cancel, "pause": dashboard.schedule_pause,
                          "resume": dashboard.schedule_resume}.get(action, lambda _: False)(job_id)
                    return self._json({"job_id": job_id, "ok": ok})
                if path.path == "/api/fleet":
                    goal = (body.get("goal") or "").strip()
                    if not goal:
                        return self._json({"error": "a goal is required"}, 400)
                    profiles = body.get("profiles") or None
                    if isinstance(profiles, str):
                        profiles = [p.strip() for p in profiles.split(",") if p.strip()]
                    dashboard.fan_out(goal, profiles, approved=bool(body.get("approved")))
                    return self._json({"started": goal, "profiles": profiles or "all"})
                if path.path == "/api/approve":
                    task_id = (body.get("task_id") or "").strip()
                    if not task_id:
                        return self._json({"error": "task_id is required"}, 400)
                    try:
                        result = dashboard.brain.approve(task_id)
                    except Exception as exc:  # noqa: BLE001
                        return self._json({"error": str(exc)[:200]}, 400)
                    return self._json(result.to_dict())
                return self._json({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.started = time.time()
        threading.Thread(target=self.httpd.serve_forever, name="shadow-dashboard", daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        self.brain.shutdown()

    # -- work ---------------------------------------------------------
    def busy(self) -> bool:
        workflow = self.brain.engine("workflow")
        return workflow.current is not None

    def last(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last)

    def run_async(self, goal: str, approved: bool = False) -> None:
        def work() -> None:
            try:
                result = self.brain.run(goal, approved=approved)
                with self._lock:
                    self._last = result.to_dict()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last = {"status": "failed", "goal": goal,
                                  "error": f"{type(exc).__name__}: {exc}"}

        self._worker = threading.Thread(target=work, name="shadow-dashboard-run", daemon=True)
        self._worker.start()

    def fan_out(self, goal: str, profiles=None, approved: bool = False) -> None:
        """The fleet runs its own workers, so it does not take the run slot."""
        def work() -> None:
            try:
                self.brain.fan_out(goal, profiles, approved=approved)
            except Exception as exc:  # noqa: BLE001
                self.brain.ui.notify("err", f"fleet: {type(exc).__name__}: {exc}")
        threading.Thread(target=work, name="shadow-dashboard-fleet", daemon=True).start()

    def schedule_add(self, goal: str, minutes: float, name: str = "") -> Dict[str, Any]:
        scheduler = self.brain.engine("scheduler")
        job = scheduler.every_goal(name or goal[:40], goal, max(1.0, float(minutes)) * 60.0)
        return job.to_dict()

    def schedule_cancel(self, job_id: str) -> bool:
        return self.brain.engine("scheduler").cancel(job_id)

    def schedule_pause(self, job_id: str) -> bool:
        return self.brain.engine("scheduler").pause(job_id)

    def schedule_resume(self, job_id: str) -> bool:
        return self.brain.engine("scheduler").resume(job_id)

    def schedule_report(self) -> Dict[str, Any]:
        scheduler = self.brain.engine("scheduler")
        # a saved goal is the only kind a person can act on from the dashboard
        return {"jobs": [j.to_dict() for j in scheduler.jobs.values() if j.goal]}

    def fleet_report(self) -> Dict[str, Any]:
        engine = self.brain.engine("fleet")
        return engine.report()

    def state(self) -> Dict[str, Any]:
        status = self.brain.status()
        ui = self.brain.engine("ui").state()
        return {
            "status": status,
            "notices": ui["notices"][-30:],
            "lines": ui["lines"][-60:],
            "running": self.busy(),
            "last": self.last(),
            "fleet": self.fleet_report(),
            "schedule": self.schedule_report(),
        }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHADOW AI</title>
<style>
  :root{--bg:#0e1116;--panel:#161b23;--line:#242c38;--ink:#e6edf6;--dim:#8b98ab;
        --ok:#4ade80;--warn:#fbbf24;--err:#f87171;--acc:#7dd3fc}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line)}
  header h1{font-size:15px;margin:0;letter-spacing:.08em}
  .pill{font-size:11px;padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--dim)}
  main{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:14px;padding:14px 18px}
  @media (max-width:900px){main{grid-template-columns:1fr}}
  section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
  h2{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);margin:0 0 10px}
  form{display:flex;gap:8px}
  input[type=text]{flex:1;background:#0b0f14;border:1px solid var(--line);border-radius:8px;
                   color:var(--ink);padding:9px 10px;font:inherit}
  button{background:#1d2836;border:1px solid #2c3b4f;color:var(--ink);border-radius:8px;
         padding:9px 14px;font:inherit;cursor:pointer}
  button:hover{border-color:var(--acc)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  td{padding:3px 0;vertical-align:top}
  td.k{color:var(--dim);width:38%}
  .ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)} .dim{color:var(--dim)}
  .steps{margin:8px 0 0;padding:0;list-style:none;font-size:12.5px}
  .steps li{padding:3px 0;border-bottom:1px dotted #1e2733}
  .log{max-height:46vh;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
  .row{display:flex;justify-content:space-between;gap:8px}
  .bar{height:4px;background:#0b0f14;border-radius:3px;overflow:hidden;margin-top:8px}
  .bar>i{display:block;height:100%;background:var(--acc);width:0}
</style></head>
<body>
<header><h1>SHADOW AI</h1><span class="pill" id="profile">—</span>
  <span class="pill" id="engines">—</span><span class="pill" id="sup">—</span>
  <span class="pill" id="mock" style="margin-left:auto">—</span></header>
<main>
  <section>
    <h2>Goal</h2>
    <form id="f"><input id="goal" type="text" placeholder="Open shop.local, fill the search field with lamp, press Search" autocomplete="off">
      <button id="go">Run</button></form>
    <div class="bar"><i id="prog"></i></div>
    <div id="result" class="dim" style="margin-top:10px">no run yet</div>
    <ul class="steps" id="steps"></ul>
  </section>
  <section>
    <h2>Fleet</h2>
    <form id="ff"><input id="fleetGoal" type="text" placeholder="the same goal on every profile" autocomplete="off">
      <button id="fan">Fan out</button></form>
    <table id="fleetTbl"></table>
  </section>
  <section>
    <h2>Schedule</h2>
    <form id="sf"><input id="schedGoal" type="text" placeholder="a goal to repeat" autocomplete="off">
      <input id="schedEvery" type="number" min="1" value="60" style="width:80px" title="minutes">
      <button id="schedAdd">Save</button></form>
    <table id="schedTbl"></table>
  </section>
  <section>
    <h2>Engines</h2>
    <table id="enginesTbl"></table>
  </section>
  <section>
    <h2>Supervisor</h2>
    <table id="supTbl"></table>
  </section>
  <section>
    <h2>Live</h2>
    <div class="log" id="log"></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const TOKEN = __SHADOW_TOKEN__;
const authHeaders = Object.assign({"Content-Type":"application/json"},
                                  TOKEN ? {"X-Shadow-Token": TOKEN} : {});
let lastTask = null;

async function refresh() {
  try {
    const r = await fetch("api/state", {cache: "no-store"});
    const s = await r.json();
    const c = s.status.context;
    $("profile").textContent = c.profile || "no profile";
    $("mock").textContent = c.mock ? "mock launcher" : "live ShardX";
    $("mock").className = "pill " + (c.mock ? "warn" : "ok");
    const h = s.status.engines, names = Object.keys(h);
    const down = names.filter(n => !h[n].healthy);
    $("engines").textContent = (names.length - down.length) + "/" + names.length + " engines";
    $("engines").className = "pill " + (down.length ? "warn" : "ok");
    $("enginesTbl").innerHTML = names.map(n => `<tr><td class="${h[n].healthy?'ok':'err'}">${h[n].healthy?'ok':'DOWN'}</td>
        <td>${esc(n)}</td><td class="dim">${esc(h[n].state)}</td></tr>`).join("");
    const sup = s.status.supervisor;
    $("sup").textContent = "hb " + sup.heartbeats;
    $("supTbl").innerHTML = [
      ["heartbeats", sup.heartbeats], ["watching", sup.watching ? "yes" : "no"],
      ["quarantined", Object.keys(sup.quarantined).join(", ") || "none"],
      ["resumed", (sup.resumed_tasks || []).length],
      ["incidents", sup.incidents.length],
    ].map(([k,v]) => `<tr><td class="k">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
    const fleet = (s.fleet && s.fleet.jobs) || [];
    $("fleetTbl").innerHTML = fleet.length ? fleet.slice(-8).reverse().map(j =>
      `<tr><td class="${j.status === "done" ? "ok" : (j.status === "failed" ? "err" : "warn")}">${esc(j.status)}</td>
        <td>${esc(j.profile)}</td><td class="dim">${esc((j.answer || j.error || j.goal || "").slice(0, 70))}</td>
        <td class="dim">${esc(j.ms)}ms</td></tr>`).join("")
      : `<tr><td class="dim" colspan="4">no fleet jobs yet</td></tr>`;
    const sched = (s.schedule && s.schedule.jobs) || [];
    $("schedTbl").innerHTML = sched.length ? sched.map(j =>
      `<tr><td class="${j.paused ? "warn" : "ok"}">${j.paused ? "paused" : "saved"}</td>
        <td>${esc(j.name)}</td><td class="dim">every ${Math.round(j.every_s / 60)}m · runs ${esc(j.runs)}</td>
        <td><button data-job="${esc(j.id)}" data-act="${j.paused ? "resume" : "pause"}">${j.paused ? "resume" : "pause"}</button>
            <button data-job="${esc(j.id)}" data-act="cancel">cancel</button></td></tr>`).join("")
      : `<tr><td class="dim" colspan="4">no saved jobs</td></tr>`;
    $("schedTbl").querySelectorAll("button").forEach(b => b.onclick = async () => {
      await fetch("api/schedule", {method:"POST", headers: authHeaders,
                                   body: JSON.stringify({action: b.dataset.act, job_id: b.dataset.job})});
      refresh();
    });
    $("log").textContent = (s.lines || []).join("\n") || "…";
    $("log").scrollTop = 1e9;

    if (s.last && s.last.task_id && s.last.task_id !== lastTask) {
      lastTask = s.last.task_id;
      showResult(s.last);
    }
    if (s.running) { $("result").textContent = "running…"; $("prog").style.width = "60%"; }
    else { $("prog").style.width = s.last ? "100%" : "0"; }
  } catch (e) { $("log").textContent = "dashboard cannot reach the engines: " + e; }
}

function showResult(r) {
  const cls = r.status === "done" ? "ok" : (r.status === "needs_approval" ? "warn" : "err");
  $("result").innerHTML = `<span class="${cls}">${esc(r.status)}</span> — ${esc(r.goal || "")}` +
    (r.error ? `<br><span class="err">${esc(r.error)}</span>` : "") +
    (r.results && r.results.answer ? `<br>answer: <span class="ok">${esc(r.results.answer)}</span>` : "");
  $("steps").innerHTML = (r.steps || []).map((st, i) => {
    const mark = {done:"+", failed:"x", skipped:"-", needs_approval:"!", pending:"·", running:">"}[st.status] || "?";
    const c = st.status === "done" ? "ok" : (st.status === "failed" ? "err" : "dim");
    return `<li><span class="${c}">${mark}</span> ${i+1}. ${esc(st.action)} ${esc(st.target || "")} ${esc(st.value || "")}
            <span class="dim">${esc((st.detail || "").slice(0, 110))}</span></li>`;
  }).join("");
  if (r.status === "needs_approval" && r.task_id) {
    const b = document.createElement("button");
    b.textContent = "Approve and continue";
    b.onclick = async () => {
      await fetch("api/approve", {method:"POST", headers: authHeaders,
                                  body: JSON.stringify({task_id: r.task_id})});
      refresh();
    };
    $("steps").appendChild(b);
  }
}

$("sf").onsubmit = async (e) => {
  e.preventDefault();
  const goal = $("schedGoal").value.trim();
  if (!goal) return;
  await fetch("api/schedule", {method:"POST", headers: authHeaders,
                               body: JSON.stringify({goal, minutes: Number($("schedEvery").value) || 60})});
  $("schedGoal").value = "";
  refresh();
};

$("ff").onsubmit = async (e) => {
  e.preventDefault();
  const goal = $("fleetGoal").value.trim();
  if (!goal) return;
  const r = await fetch("api/fleet", {method:"POST", headers: authHeaders,
                                      body: JSON.stringify({goal})});
  if (!r.ok) { const err = await r.json(); $("result").innerHTML = `<span class="err">${esc(err.error)}</span>`; }
  refresh();
};

$("f").onsubmit = async (e) => {
  e.preventDefault();
  const goal = $("goal").value.trim();
  if (!goal) return;
  $("result").textContent = "starting…";
  const r = await fetch("api/run", {method:"POST", headers: authHeaders,
                                    body: JSON.stringify({goal})});
  if (!r.ok) { const e = await r.json(); $("result").innerHTML = `<span class="err">${esc(e.error)}</span>`; }
  else { setTimeout(refresh, 300); }
};

refresh();
setInterval(refresh, 1500);
</script>
</body></html>
"""
