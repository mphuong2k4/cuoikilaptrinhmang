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
      --bg: #0b1220;
      --card: rgba(255,255,255,0.06);
      --card2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --border: rgba(255,255,255,0.10);
      --accent: #7c9cff;
      --good: #2bd576;
      --warn: #ffcc66;
      --bad: #ff5d5d;
      --shadow: 0 10px 30px rgba(0,0,0,0.35);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
      --radius: 14px;
    }

    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: var(--sans);
      background: radial-gradient(1200px 700px at 20% 0%, rgba(124,156,255,0.18), transparent 60%),
                  radial-gradient(900px 600px at 90% 10%, rgba(43,213,118,0.12), transparent 55%),
                  var(--bg);
      color: var(--text);
    }

    .wrap{
      max-width: 1220px;
      margin: 0 auto;
      padding: 20px 18px 26px;
    }

    .top{
      display:flex;
      align-items:flex-end;
      justify-content:space-between;
      gap: 16px;
      margin-bottom: 14px;
    }

    .title{
      display:flex;
      flex-direction:column;
      gap: 6px;
    }

    h1{
      margin:0;
      font-size: 22px;
      letter-spacing: 0.2px;
      font-weight: 750;
    }

    .sub{
      margin:0;
      color: var(--muted);
      font-size: 13px;
    }

    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      font-size: 13px;
      color: var(--muted);
      white-space: nowrap;
    }

    .dot{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--good);
      box-shadow: 0 0 0 4px rgba(43,213,118,0.10);
    }

    .grid{
      display:grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 14px;
    }

    .card{
      background: linear-gradient(180deg, rgba(255,255,255,0.075), rgba(255,255,255,0.05));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .cardHeader{
      padding: 12px 14px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      border-bottom: 1px solid var(--border);
      background: rgba(255,255,255,0.03);
    }

    .cardHeader strong{
      font-size: 14px;
      letter-spacing: 0.2px;
    }

    .count{
      color: var(--muted);
      font-size: 12px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
    }

    table{
      width:100%;
      border-collapse: collapse;
    }
    thead th{
      text-align:left;
      font-size: 12px;
      color: var(--muted);
      font-weight: 650;
      letter-spacing: 0.2px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      background: rgba(255,255,255,0.02);
    }
    tbody td{
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      font-size: 13px;
      color: rgba(255,255,255,0.88);
      vertical-align: middle;
    }
    tbody tr:hover{
      background: rgba(124,156,255,0.08);
      cursor: pointer;
    }
    tbody tr.active{
      background: rgba(124,156,255,0.14);
    }

    .mono{ font-family: var(--mono); font-size: 12.5px; }
    .muted{ color: var(--muted); }

    .status{
      display:flex;
      align-items:center;
      gap:8px;
    }
    .sDot{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--good);
    }
    .sDot.warn{ background: var(--warn); }
    .sDot.bad{ background: var(--bad); }

    .actions{
      display:flex;
      gap:10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
    }

    button{
      appearance:none;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.06);
      color: rgba(255,255,255,0.92);
      padding: 9px 12px;
      border-radius: 12px;
      font-size: 13px;
      font-weight: 650;
      letter-spacing: 0.2px;
      cursor:pointer;
      transition: transform .08s ease, background .15s ease, border-color .15s ease;
    }
    button:hover{
      background: rgba(255,255,255,0.10);
      border-color: rgba(255,255,255,0.18);
    }
    button:active{
      transform: translateY(1px);
    }
    button.primary{
      background: rgba(124,156,255,0.18);
      border-color: rgba(124,156,255,0.40);
    }
    button.danger{
      background: rgba(255,93,93,0.14);
      border-color: rgba(255,93,93,0.32);
    }

    .rightTop{
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      background: rgba(255,255,255,0.03);
    }

    .selected{
      display:flex;
      flex-direction:column;
      gap: 4px;
      min-width: 0;
    }
    .selected .label{
      font-size: 12px;
      color: var(--muted);
    }
    .selected .value{
      display:flex;
      align-items:center;
      gap: 10px;
      font-weight: 750;
      letter-spacing: 0.2px;
      font-size: 14px;
      min-width: 0;
    }
    .badge{
      font-family: var(--mono);
      font-weight: 650;
      font-size: 12px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      overflow:hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 100%;
    }

    pre{
      margin:0;
      padding: 12px 14px;
      height: 520px;
      overflow:auto;
      background: linear-gradient(180deg, rgba(10,15,28,0.9), rgba(10,15,28,0.65));
      color: rgba(255,255,255,0.9);
      font-family: var(--mono);
      font-size: 12.5px;
      line-height: 1.35;
    }

    @media (max-width: 980px){
      .grid{ grid-template-columns: 1fr; }
      pre{ height: 420px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="title">
        <h1>PC Network Monitor</h1>
        <p class="sub">Realtime monitoring dashboard</p>
      </div>
      <div class="pill"><span class="dot"></span><span id="wsState">Connecting...</span></div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="cardHeader">
          <strong>Clients</strong>
          <span class="count" id="count">0</span>
        </div>
        <div style="overflow:auto;">
          <table>
            <thead>
              <tr>
                <th style="width:38%;">Client</th>
                <th style="width:20%;">Address</th>
                <th style="width:14%;">Seen</th>
                <th style="width:9%;">CPU</th>
                <th style="width:9%;">MEM</th>
                <th style="width:10%;">DISK</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="rightTop">
          <div class="selected">
            <div class="label">Selected</div>
            <div class="value">
              <span class="badge" id="selected">(none)</span>
            </div>
          </div>
          <div class="status muted" id="selectedStatus" style="display:none;">
            <span class="sDot" id="sDot"></span>
            <span id="sText"></span>
          </div>
        </div>

        <div class="actions">
          <button class="primary" onclick="sendReq('sysinfo')">Sysinfo</button>
          <button class="primary" onclick="sendReq('processes')">Processes</button>
          <button class="primary" onclick="sendReq('netstat')">Netstat</button>
          <button class="danger" onclick="clearLog()">Clear</button>
        </div>

        <pre id="log"></pre>
      </div>
    </div>
  </div>

<script>
  let selectedClientId = "";
  let ws = null;
  let lastSnapshot = [];

  const el = (id) => document.getElementById(id);

  function fmtPct(v){
    if (v === null || v === undefined) return "";
    const n = Number(v);
    if (Number.isNaN(n)) return "";
    return n.toFixed(1);
  }

  function statusFromSeen(seenSec){
    // green: < 5s, yellow: < 15s, red: >= 15s
    if (seenSec < 5) return {cls:"", text:"online"};
    if (seenSec < 15) return {cls:"warn", text:"idle"};
    return {cls:"bad", text:"stale"};
  }

  function setSelected(cid){
    selectedClientId = cid || "";
    el("selected").textContent = selectedClientId || "(none)";
    renderClients(lastSnapshot);
    renderSelectedStatus();
  }

  function renderSelectedStatus(){
    const wrap = el("selectedStatus");
    if (!selectedClientId){
      wrap.style.display = "none";
      return;
    }
    const item = lastSnapshot.find(x => x.client_id === selectedClientId);
    if (!item){
      wrap.style.display = "none";
      return;
    }
    const s = statusFromSeen(item.last_seen_sec);
    el("sDot").className = "sDot " + s.cls;
    el("sText").textContent = s.text;
    wrap.style.display = "flex";
  }

  function renderClients(clients){
    lastSnapshot = clients || [];
    el("count").textContent = String(lastSnapshot.length);

    const tbody = el("tbody");
    tbody.innerHTML = "";

    for (const c of lastSnapshot){
      const tr = document.createElement("tr");
      if (c.client_id === selectedClientId) tr.classList.add("active");
      tr.onclick = () => setSelected(c.client_id);

      const m = c.metrics || {};
      const seen = Number(c.last_seen_sec ?? 9999);
      const st = statusFromSeen(seen);

      tr.innerHTML = `
        <td>
          <div style="display:flex; flex-direction:column; gap:2px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="sDot ${st.cls}"></span>
              <span class="mono">${c.client_id}</span>
            </div>
            <div class="muted" style="font-size:12px;">${c.name || ""}</div>
          </div>
        </td>
        <td class="mono">${c.addr || ""}</td>
        <td class="mono">${seen.toFixed(2)}s</td>
        <td class="mono">${fmtPct(m.cpu_percent)}</td>
        <td class="mono">${fmtPct(m.mem_percent)}</td>
        <td class="mono">${fmtPct(m.disk_percent)}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  function appendLog(line){
    const pre = el("log");
    pre.textContent += line + "\\n";
    pre.scrollTop = pre.scrollHeight;
  }

  function clearLog(){
    el("log").textContent = "";
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

  function setWsState(text, ok){
    el("wsState").textContent = text;
    const dot = document.querySelector(".dot");
    dot.style.background = ok ? "var(--good)" : "var(--warn)";
    dot.style.boxShadow = ok ? "0 0 0 4px rgba(43,213,118,0.10)" : "0 0 0 4px rgba(255,204,102,0.10)";
  }

  function connectWS(){
    const proto = (location.protocol === "https:") ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      setWsState("Connected", true);
    };

    ws.onclose = () => {
      setWsState("Reconnecting...", false);
      setTimeout(connectWS, 900);
    };

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);

      if (msg.type === "snapshot"){
        renderClients(msg.payload.clients || []);
        renderSelectedStatus();
      }

      if (msg.type === "response"){
        const p = msg.payload || {};
        appendLog(`[response] ${p.client_id} (${p.request_id})`);
        appendLog(JSON.stringify(p.payload, null, 2));
      }
    };
  }

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
        
