from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any, Dict, List, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from src.server.server import MonitorServer, udp_discovery_responder
from src.shared.protocol import TCP_SERVER_PORT, UDP_DISCOVERY_PORT, DEFAULT_AUTH_TOKEN

app = FastAPI(title="PC Network Monitor")

SERVER: MonitorServer | None = None
WS_CLIENTS: Set[WebSocket] = set()


def snapshot_clients() -> List[Dict[str, Any]]:
    """Return a stable snapshot for UI rendering."""
    if SERVER is None:
        return []

    now = time.time()
    items: List[Dict[str, Any]] = []
    for cid, c in SERVER.clients.items():
        age = max(0.0, now - float(c.last_seen))
        m = c.last_metrics or {}
        items.append(
            {
                "client_id": cid,
                "name": c.name,
                "addr": f"{c.addr[0]}:{c.addr[1]}",
                "last_seen_sec": round(age, 2),
                "metrics": {
                    "cpu_percent": m.get("cpu_percent"),
                    "mem_percent": m.get("mem_percent"),
                    "disk_percent": m.get("disk_percent"),
                },
            }
        )

    items.sort(key=lambda x: x["last_seen_sec"])
    return items


async def ws_broadcast(message: Dict[str, Any]) -> None:
    """Broadcast JSON message to all websocket clients."""
    if not WS_CLIENTS:
        return

    payload = json.dumps(message, ensure_ascii=False)
    dead: List[WebSocket] = []

    for ws in WS_CLIENTS:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        WS_CLIENTS.discard(ws)


INDEX_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>PC Network Monitor</title>
  <style>
    :root{
      --bg:#070b14;
      --card:rgba(255,255,255,.06);
      --card2:rgba(255,255,255,.09);
      --text:rgba(255,255,255,.92);
      --muted:rgba(255,255,255,.62);
      --border:rgba(255,255,255,.12);
      --shadow:0 18px 50px rgba(0,0,0,.45);
      --radius:16px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      --accent:#7c9cff;
      --good:#34d399;
      --warn:#fbbf24;
      --bad:#fb7185;
    }

    *{box-sizing:border-box}
   nd
    body{
      margin:0;
      font-family:var(--sans);
      color:var(--text);
      background:
        radial-gradient(1200px 700px at 15% 0%, rgba(124,156,255,.22), transparent 60%),
        radial-gradient(900px 600px at 90% 10%, rgba(52,211,153,.12), transparent 55%),
        radial-gradient(900px 600px at 70% 90%, rgba(251,113,133,.10), transparent 55%),
        var(--bg);
    }

    .wrap{max-width:1240px; margin:0 auto; padding:18px 16px 28px;}
    .topbar{
      display:flex; align-items:center; justify-content:space-between; gap:14px;
      margin-bottom:14px;
    }
    .brand{display:flex; flex-direction:column; gap:6px;}
    .brand h1{margin:0; font-size:20px; font-weight:800; letter-spacing:.2px;}
    .brand .sub{margin:0; font-size:12.5px; color:var(--muted);}

    .rightbar{display:flex; align-items:center; gap:10px; flex-wrap:wrap;}
    .chip{
      display:inline-flex; align-items:center; gap:8px;
      padding:8px 12px; border-radius:999px;
      background:rgba(255,255,255,.07); border:1px solid var(--border);
      box-shadow:var(--shadow); color:var(--muted); font-size:13px;
      white-space:nowrap;
    }
    .dot{width:9px; height:9px; border-radius:50%; background:var(--warn); box-shadow:0 0 0 4px rgba(251,191,36,.12);}
    .dot.ok{background:var(--good); box-shadow:0 0 0 4px rgba(52,211,153,.12);}

    .grid{display:grid; grid-template-columns: 1.25fr 1fr; gap:14px;}
    @media (max-width: 980px){ .grid{grid-template-columns:1fr;} }

    .card{
      background:linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.05));
      border:1px solid var(--border);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      overflow:hidden;
    }

    .cardHeader{
      padding:12px 14px;
      display:flex; align-items:center; justify-content:space-between; gap:10px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.03);
    }
    .cardHeader strong{font-size:14px; letter-spacing:.2px;}
    .headerTools{display:flex; align-items:center; gap:10px; flex-wrap:wrap;}
    .pill{
      padding:4px 9px; border-radius:999px;
      background:rgba(255,255,255,.06); border:1px solid var(--border);
      color:var(--muted); font-size:12px;
    }

    .input{
      display:flex; align-items:center; gap:8px;
      padding:8px 10px; border-radius:12px;
      background:rgba(255,255,255,.06); border:1px solid var(--border);
      color:var(--text);
      min-width:220px;
    }
    .input input{
      width:100%;
      border:none; outline:none; background:transparent; color:var(--text);
      font-size:13px;
    }

    .select{
      padding:8px 10px; border-radius:12px;
      background:rgba(255,255,255,.06); border:1px solid var(--border);
      color:var(--text); font-size:13px;
      outline:none;
    }

    table{width:100%; border-collapse:collapse;}
    thead th{
      text-align:left;
      font-size:12px; color:var(--muted); font-weight:700;
      padding:10px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.02);
      position:sticky; top:0;
    }
    tbody td{
      padding:10px 12px;
      border-bottom:1px solid rgba(255,255,255,.06);
      font-size:13px;
      vertical-align:middle;
    }
    tbody tr:hover{background:rgba(124,156,255,.08); cursor:pointer;}
    tbody tr.active{background:rgba(124,156,255,.14);}

    .mono{font-family:var(--mono); font-size:12.5px;}
    .muted{color:var(--muted);}
    .row1{display:flex; align-items:center; gap:10px;}
    .name{font-size:12px; color:var(--muted); margin-top:2px;}
    .statusDot{width:8px; height:8px; border-radius:50%; background:var(--good);}
    .statusDot.warn{background:var(--warn);}
    .statusDot.bad{background:var(--bad);}

    .bar{
      height:8px; border-radius:999px;
      background:rgba(255,255,255,.07);
      border:1px solid rgba(255,255,255,.10);
      overflow:hidden;
    }
    .bar > i{
      display:block; height:100%;
      width:0%;
      background:rgba(124,156,255,.80);
    }
    .bar.mem > i{ background: rgba(52,211,153,.80); }
    .bar.disk > i{ background: rgba(251,191,36,.80); }

    .sideTop{
      padding:12px 14px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.03);
      display:flex; align-items:center; justify-content:space-between; gap:10px;
    }
    .selected{
      display:flex; flex-direction:column; gap:4px; min-width:0;
    }
    .selected .label{font-size:12px; color:var(--muted);}
    .badge{
      font-family:var(--mono);
      font-size:12px; font-weight:700;
      padding:5px 10px;
      border-radius:999px;
      background:rgba(255,255,255,.06);
      border:1px solid var(--border);
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
      max-width: 340px;
    }

    .actions{
      padding:12px 14px;
      display:flex; gap:10px; flex-wrap:wrap;
      border-bottom:1px solid var(--border);
    }
    button{
      appearance:none;
      border:1px solid var(--border);
      background:rgba(255,255,255,.06);
      color:rgba(255,255,255,.92);
      padding:9px 12px;
      border-radius:12px;
      font-size:13px; font-weight:700;
      cursor:pointer;
      transition: transform .08s ease, background .15s ease, border-color .15s ease;
    }
    button:hover{background:rgba(255,255,255,.10); border-color:rgba(255,255,255,.18);}
    button:active{transform:translateY(1px);}
    button.primary{background:rgba(124,156,255,.18); border-color:rgba(124,156,255,.38);}
    button.ghost{background:transparent;}

    .tabs{
      display:flex; gap:8px; padding:10px 14px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.02);
    }
    .tab{
      padding:7px 10px;
      border-radius:12px;
      border:1px solid rgba(255,255,255,.12);
      background:rgba(255,255,255,.05);
      color:rgba(255,255,255,.85);
      font-size:12.5px;
      cursor:pointer;
      user-select:none;
    }
    .tab.active{
      background:rgba(124,156,255,.18);
      border-color:rgba(124,156,255,.40);
      color:rgba(255,255,255,.92);
    }

    .pane{
      display:none;
      padding: 12px 14px;
    }
    .pane.active{display:block;}

    pre{
      margin:0;
      padding: 12px 14px;
      height: 430px;
      overflow:auto;
      background:linear-gradient(180deg, rgba(10,15,28,.90), rgba(10,15,28,.65));
      color:rgba(255,255,255,.92);
      font-family:var(--mono);
      font-size:12.5px;
      line-height:1.35;
      border-top:1px solid rgba(255,255,255,.06);
    }
    @media (max-width: 980px){ pre{height:360px;} }

    .kv{
      display:grid; grid-template-columns: 140px 1fr;
      gap:8px 12px;
      font-size:13px;
    }
    .kv div:nth-child(odd){ color: var(--muted); }
    .kv div:nth-child(even){ font-family: var(--mono); }

    .empty{
      padding: 18px 14px;
      color: var(--muted);
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <h1>PC Network Monitor</h1>
        <p class="sub">LAN monitoring dashboard</p>
      </div>

      <div class="rightbar">
        <div class="chip"><span class="dot" id="wsDot"></span><span id="wsState">Connecting...</span></div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="cardHeader">
          <strong>Clients</strong>
          <div class="headerTools">
            <span class="pill" id="count">0</span>
            <div class="input">
              <span class="muted">🔎</span>
              <input id="q" placeholder="Search by id / name / ip" autocomplete="off" />
            </div>
            <select id="sort" class="select">
              <option value="seen">Sort: last seen</option>
              <option value="cpu">Sort: CPU</option>
              <option value="mem">Sort: MEM</option>
              <option value="disk">Sort: DISK</option>
            </select>
          </div>
        </div>

        <div style="overflow:auto; max-height: 640px;">
          <table>
            <thead>
              <tr>
                <th style="width:40%;">Client</th>
                <th style="width:20%;">Address</th>
                <th style="width:12%;">Seen</th>
                <th style="width:28%;">Usage</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="sideTop">
          <div class="selected">
            <div class="label">Selected</div>
            <div class="badge" id="selected">(none)</div>
          </div>
          <button class="ghost" onclick="copyActive()">Copy JSON</button>
        </div>

        <div class="actions">
          <button class="primary" onclick="sendReq('sysinfo')">Sysinfo</button>
          <button class="primary" onclick="sendReq('processes')">Processes</button>
          <button class="primary" onclick="sendReq('netstat')">Netstat</button>
          <button onclick="clearAll()">Clear</button>
        </div>

        <div class="tabs">
          <div class="tab active" data-tab="tSys" onclick="setTab('tSys')">Sysinfo</div>
          <div class="tab" data-tab="tProc" onclick="setTab('tProc')">Processes</div>
          <div class="tab" data-tab="tNet" onclick="setTab('tNet')">Netstat</div>
          <div class="tab" data-tab="tRaw" onclick="setTab('tRaw')">Raw</div>
        </div>

        <div class="pane active" id="tSys">
          <div id="sysPane" class="empty">No data.</div>
        </div>
        <div class="pane" id="tProc">
          <div id="procPane" class="empty">No data.</div>
        </div>
        <div class="pane" id="tNet">
          <div id="netPane" class="empty">No data.</div>
        </div>

        <pre id="log"></pre>
      </div>
    </div>
  </div>

<script>
  let selectedClientId = "";
  let ws = null;

  let lastSnapshot = [];
  const lastByTab = { sysinfo: null, processes: null, netstat: null };
  let activeRaw = null;

  const el = (id) => document.getElementById(id);
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));

  function pct(v){
    if (v === null || v === undefined) return null;
    const n = Number(v);
    if (Number.isNaN(n)) return null;
    return clamp(n, 0, 100);
  }
  function fmt(v){
    const p = pct(v);
    return p === null ? "" : p.toFixed(1) + "%";
  }

  function statusFromSeen(seenSec){
    if (seenSec < 5) return {cls:"", text:"online"};
    if (seenSec < 15) return {cls:"warn", text:"idle"};
    return {cls:"bad", text:"stale"};
  }

  function setWsState(text, ok){
    el("wsState").textContent = text;
    const d = el("wsDot");
    d.className = ok ? "dot ok" : "dot";
  }

  function setSelected(cid){
    selectedClientId = cid || "";
    el("selected").textContent = selectedClientId || "(none)";
    renderClients();
  }

  function clearAll(){
    el("log").textContent = "";
    el("sysPane").textContent = "No data.";
    el("sysPane").className = "empty";
    el("procPane").textContent = "No data.";
    el("procPane").className = "empty";
    el("netPane").textContent = "No data.";
    el("netPane").className = "empty";
    activeRaw = null;
    lastByTab.sysinfo = null;
    lastByTab.processes = null;
    lastByTab.netstat = null;
  }

  function appendLog(line){
    const pre = el("log");
    pre.textContent += line + "\n";
    pre.scrollTop = pre.scrollHeight;
  }

  function setTab(tabId){
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".pane").forEach(p => p.classList.remove("active"));

    document.querySelector(`.tab[data-tab="${tabId}"]`).classList.add("active");
    el(tabId).classList.add("active");

    // Raw tab shows latest response JSON
    if (tabId === "tRaw"){
      const pre = el("log");
      if (activeRaw) pre.textContent = JSON.stringify(activeRaw, null, 2) + "\n";
    }
  }

  function copyActive(){
    if (!activeRaw){
      navigator.clipboard?.writeText("");
      return;
    }
    navigator.clipboard?.writeText(JSON.stringify(activeRaw, null, 2));
  }

  function renderKV(targetId, obj){
    const host = el(targetId);
    if (!obj){
      host.textContent = "No data.";
      host.className = "empty";
      return;
    }
    host.className = "";
    const html = `
      <div class="kv">
        <div>Hostname</div><div>${(obj.hostname ?? "")}</div>
        <div>OS</div><div>${(obj.os ?? "")}</div>
        <div>Python</div><div>${(obj.python ?? "")}</div>
        <div>Arch</div><div>${(obj.arch ?? "")}</div>
        <div>CPU</div><div>${(obj.metrics?.cpu_percent ?? "")}%</div>
        <div>Memory</div><div>${(obj.metrics?.mem_percent ?? "")}%</div>
        <div>Disk</div><div>${(obj.metrics?.disk_percent ?? "")}%</div>
      </div>`;
    host.innerHTML = html;
  }

  function renderJSON(targetId, obj){
    const host = el(targetId);
    if (!obj){
      host.textContent = "No data.";
      host.className = "empty";
      return;
    }
    host.className = "";
    host.innerHTML = `<pre style="height:380px; border-radius:12px; margin:0;">${escapeHtml(JSON.stringify(obj, null, 2))}</pre>`;
  }

  function escapeHtml(s){
    return String(s)
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;");
  }

  function passesQuery(c, q){
    if (!q) return true;
    const hay = (c.client_id + " " + (c.name||"") + " " + (c.addr||"")).toLowerCase();
    return hay.includes(q);
  }

  function renderClients(){
    const q = (el("q").value || "").trim().toLowerCase();
    const sortKey = el("sort").value;

    let list = (lastSnapshot || []).filter(c => passesQuery(c, q));

    const num = (x) => {
      if (x === null || x === undefined) return -1;
      const n = Number(x);
      return Number.isNaN(n) ? -1 : n;
    };

    list.sort((a,b) => {
      if (sortKey === "seen") return (a.last_seen_sec ?? 9999) - (b.last_seen_sec ?? 9999);
      if (sortKey === "cpu") return num(b.metrics?.cpu_percent) - num(a.metrics?.cpu_percent);
      if (sortKey === "mem") return num(b.metrics?.mem_percent) - num(a.metrics?.mem_percent);
      if (sortKey === "disk") return num(b.metrics?.disk_percent) - num(a.metrics?.disk_percent);
      return 0;
    });

    el("count").textContent = String(list.length);

    const tbody = el("tbody");
    tbody.innerHTML = "";

    for (const c of list){
      const tr = document.createElement("tr");
      if (c.client_id === selectedClientId) tr.classList.add("active");
      tr.onclick = () => setSelected(c.client_id);

      const seen = Number(c.last_seen_sec ?? 9999);
      const st = statusFromSeen(seen);

      const cpu = pct(c.metrics?.cpu_percent);
      const mem = pct(c.metrics?.mem_percent);
      const disk = pct(c.metrics?.disk_percent);

      tr.innerHTML = `
        <td>
          <div style="display:flex; flex-direction:column; gap:4px;">
            <div class="row1">
              <span class="statusDot ${st.cls}"></span>
              <span class="mono">${c.client_id}</span>
            </div>
            <div class="name">${c.name || ""}</div>
          </div>
        </td>
        <td class="mono">${c.addr || ""}</td>
        <td class="mono">${seen.toFixed(2)}s</td>
        <td>
          <div style="display:grid; grid-template-columns: 54px 1fr; gap:6px 10px; align-items:center;">
            <div class="muted mono">CPU</div>
            <div class="bar"><i style="width:${cpu===null?0:cpu}%"></i></div>
            <div class="muted mono">MEM</div>
            <div class="bar mem"><i style="width:${mem===null?0:mem}%"></i></div>
            <div class="muted mono">DISK</div>
            <div class="bar disk"><i style="width:${disk===null?0:disk}%"></i></div>
          </div>
          <div class="muted mono" style="margin-top:6px;">
            ${fmt(cpu)} · ${fmt(mem)} · ${fmt(disk)}
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    }
  }

  async function sendReq(reqType){
    if (!selectedClientId){
      appendLog("[error] select a client first");
      return;
    }

    const res = await fetch("/api/request", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ client_id: selectedClientId, req_type: reqType })
    });

    const data = await res.json();
    if (!res.ok){
      appendLog("[error] " + (data.detail || JSON.stringify(data)));
      return;
    }
    appendLog(`[sent] ${reqType} -> ${selectedClientId} (${data.request_id})`);
  }

  function onResponse(payload){
    // payload: {client_id, request_id, payload:{...}}
    activeRaw = payload;

    // try to infer which pane should be updated
    const inner = payload.payload || {};
    // sysinfo usually contains hostname/os/python/arch
    if (inner.hostname || inner.os || inner.python || inner.arch){
      lastByTab.sysinfo = inner;
      renderKV("sysPane", inner);
      setTab("tSys");
      return;
    }
    // processes: usually list-like
    if (inner.processes || Array.isArray(inner) || inner.top){
      lastByTab.processes = inner;
      renderJSON("procPane", inner);
      setTab("tProc");
      return;
    }
    // netstat: connections
    lastByTab.netstat = inner;
    renderJSON("netPane", inner);
    setTab("tNet");
  }

  function connectWS(){
    const proto = (location.protocol === "https:") ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => setWsState("Connected", true);

    ws.onclose = () => {
      setWsState("Reconnecting...", false);
      setTimeout(connectWS, 900);
    };

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);

      if (msg.type === "snapshot"){
        lastSnapshot = msg.payload.clients || [];
        renderClients();
      } else if (msg.type === "response"){
        const p = msg.payload || {};
        appendLog(`[response] ${p.client_id} (${p.request_id})`);
        onResponse(p);
      }
    };
  }

  el("q").addEventListener("input", () => renderClients());
  el("sort").addEventListener("change", () => renderClients());

  connectWS();
</script>
</body>
</html>
"""



@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/api/clients")
async def api_clients():
    return JSONResponse(snapshot_clients())


@app.post("/api/request")
async def api_request(body: Dict[str, Any]):
    """
    Body:
      { "client_id": "...", "req_type": "sysinfo|processes|netstat" }
    """
    if SERVER is None:
        return JSONResponse({"detail": "server not ready"}, status_code=503)

    client_id = str(body.get("client_id", "")).strip()
    req_type = str(body.get("req_type", "")).strip()

    if req_type not in ("sysinfo", "processes", "netstat"):
        return JSONResponse({"detail": "invalid req_type"}, status_code=400)
    if not client_id:
        return JSONResponse({"detail": "missing client_id"}, status_code=400)
    if client_id not in SERVER.clients:
        return JSONResponse({"detail": "client not connected"}, status_code=404)

    api_request.counter += 1
    request_id = f"w{api_request.counter}"

    await SERVER.send_request(client_id, req_type, request_id, {})
    return {"request_id": request_id}


api_request.counter = 0  


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    try:
        await websocket.send_text(
            json.dumps({"type": "snapshot", "payload": {"clients": snapshot_clients()}}, ensure_ascii=False)
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.discard(websocket)


async def broadcast_snapshots():
    """Push live snapshot to UI periodically."""
    while True:
        await asyncio.sleep(1.0)
        await ws_broadcast({"type": "snapshot", "payload": {"clients": snapshot_clients()}})


async def broadcast_response(client_id: str, request_id: str | None, payload: Dict[str, Any]):
    """Callback from MonitorServer -> push response to UI."""
    await ws_broadcast(
        {
            "type": "response",
            "payload": {"client_id": client_id, "request_id": request_id, "payload": payload},
        }
    )


async def main():
    global SERVER

    ap = argparse.ArgumentParser(description="PC Network Monitor - Web Dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="TCP server bind host")
    ap.add_argument("--port", type=int, default=TCP_SERVER_PORT, help="TCP server port")
    ap.add_argument("--udp-bind", default="0.0.0.0")
    ap.add_argument("--udp-port", type=int, default=UDP_DISCOVERY_PORT)
    ap.add_argument("--auth-token", default=DEFAULT_AUTH_TOKEN)
    ap.add_argument("--web-host", default="0.0.0.0", help="Web dashboard bind host")
    ap.add_argument("--web-port", type=int, default=8000, help="Web dashboard port")
    args = ap.parse_args()

    SERVER = MonitorServer(args.host, args.port, args.auth_token)
    SERVER.on_response = broadcast_response

    await SERVER.start(ssl_ctx=None)

    config = uvicorn.Config(app, host=args.web_host, port=args.web_port, log_level="info")
    uv_server = uvicorn.Server(config)

    await asyncio.gather(
        udp_discovery_responder(args.udp_bind, args.udp_port, args.port),
        broadcast_snapshots(),
        uv_server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
        
