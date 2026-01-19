from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple

from src.shared.protocol import (
    TCP_SERVER_PORT,
    UDP_DISCOVERY_PORT,
    DEFAULT_AUTH_TOKEN,
    dumps,
    loads,
    PROTOCOL_VERSION,
)

ResponseCallback = Callable[[str, Optional[str], dict], Awaitable[None]]


@dataclass
class ClientState:
    client_id: str
    name: str
    addr: Tuple[str, int]
    writer: asyncio.StreamWriter
    last_seen: float = field(default_factory=lambda: time.time())
    last_metrics: dict = field(default_factory=dict)


class MonitorServer:
    def __init__(self, host: str, port: int, auth_token: str):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.clients: Dict[str, ClientState] = {}
        self._server: Optional[asyncio.AbstractServer] = None

        self.last_responses: Dict[str, dict] = {}

        self.on_response: Optional[ResponseCallback] = None

    async def start(self, ssl_ctx: Optional[ssl.SSLContext] = None):
        self._server = await asyncio.start_server(self.handle_client, self.host, self.port, ssl=ssl_ctx)
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"[TCP] Server listening on {addrs}")

    async def serve_forever(self):
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        client_id: Optional[str] = None
        try:
            line = await reader.readline()
            if not line:
                writer.close()
                await writer.wait_closed()
                return

            msg = loads(line)
            if msg.get("v") != PROTOCOL_VERSION or msg.get("type") != "auth":
                writer.write(dumps({"type": "error", "payload": {"reason": "expected auth"}}))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            token = msg.get("payload", {}).get("token", "")
            if token != self.auth_token:
                writer.write(dumps({"type": "error", "payload": {"reason": "bad token"}}))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            client_id = msg["payload"].get("client_id", "")
            name = msg["payload"].get("name", "unknown")
            if not client_id:
                writer.write(dumps({"type": "error", "payload": {"reason": "missing client_id"}}))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            self.clients[client_id] = ClientState(client_id=client_id, name=name, addr=peer, writer=writer)
            print(f"[+] Client {client_id} ({name}) connected from {peer}")

            writer.write(dumps({"type": "auth_ok", "payload": {"server_time": time.time()}}))
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = loads(line)

                c = self.clients.get(client_id)
                if c:
                    c.last_seen = time.time()

                mtype = msg.get("type")
                payload = msg.get("payload", {})

                if mtype == "metrics":
                    if c:
                        c.last_metrics = payload

                elif mtype == "response":
                    rid = msg.get("request_id")

                    if rid:
                        self.last_responses[rid] = {"client_id": client_id, "payload": payload, "t": time.time()}

                    if self.on_response is not None:
                        try:
                            await self.on_response(client_id, rid, payload)
                        except Exception as e:
                            print(f"[!] on_response callback error: {e}")

                    print(f"[response] client={client_id} request_id={rid} payload={json.dumps(payload, ensure_ascii=False)}")

                elif mtype == "heartbeat":
                    pass

                else:
                    print(f"[?] Unknown message from {client_id}: {msg}")

        except Exception as e:
            print(f"[!] Error with client {peer}: {e}")

        finally:
            if client_id and client_id in self.clients:
                print(f"[-] Client {client_id} disconnected")
                self.clients.pop(client_id, None)

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def send_request(self, client_id: str, req_type: str, request_id: str, payload: dict):
        c = self.clients.get(client_id)
        if not c:
            raise ValueError("client not found")
        msg = {
            "v": PROTOCOL_VERSION,
            "type": "request",
            "request_id": request_id,
            "payload": {"req_type": req_type, "data": payload},
        }
        c.writer.write(dumps(msg))
        await c.writer.drain()

    def list_clients(self):
        now = time.time()
        rows = []
        for cid, c in self.clients.items():
            age = now - c.last_seen
            rows.append((cid, c.name, f"{c.addr[0]}:{c.addr[1]}", f"{age:0.1f}s", c.last_metrics))
        return rows


async def udp_discovery_responder(bind_ip: str, port: int, tcp_port: int):
    """Respond to UDP broadcast discovery messages."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    print(f"[UDP] Discovery responder on {bind_ip}:{port}")
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.run_in_executor(None, sock.recvfrom, 2048)
        if data.strip() == b"DISCOVER_PC_MONITOR":
            reply = json.dumps({"tcp_port": tcp_port}).encode("utf-8")
            sock.sendto(reply, addr)


def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


async def interactive_cli(server: MonitorServer):
    """Simple interactive CLI for the instructor demo."""
    print("\nCommands:")
    print("  list                          - show connected clients")
    print("  req <client_id> sysinfo       - request system info")
    print("  req <client_id> processes     - request process list (top 30)")
    print("  req <client_id> netstat       - request network connections summary")
    print("  help                          - show commands")
    print("  quit                          - exit\n")

    counter = 0
    while True:
        cmd = await asyncio.get_running_loop().run_in_executor(None, lambda: input("monitor> ").strip())
        if not cmd:
            continue
        if cmd in ("quit", "exit"):
            break
        if cmd == "help":
            print("See command list above.")
            continue
        if cmd == "list":
            rows = server.list_clients()
            if not rows:
                print("(no clients)")
                continue
            for cid, name, addr, age, metrics in rows:
                cpu = metrics.get("cpu_percent")
                mem = metrics.get("mem_percent")
                print(f"- {cid:12} | {name:18} | {addr:21} | last_seen={age:>6} | cpu={cpu}% mem={mem}%")
            continue
        if cmd.startswith("req "):
            parts = cmd.split()
            if len(parts) < 3:
                print("Usage: req <client_id> <sysinfo|processes|netstat>")
                continue
            _, cid, req = parts[:3]
            counter += 1
            rid = f"r{counter}"
            try:
                await server.send_request(cid, req, rid, {})
                print(f"sent request {req} to {cid} (request_id={rid})")
            except Exception as e:
                print(f"error: {e}")
            continue

        print("Unknown command. Type 'help'.")

 
async def main():
    ap = argparse.ArgumentParser(description="PC Network Monitor - Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=TCP_SERVER_PORT)
    ap.add_argument("--udp-bind", default="0.0.0.0")
    ap.add_argument("--udp-port", type=int, default=UDP_DISCOVERY_PORT)
    ap.add_argument("--auth-token", default=DEFAULT_AUTH_TOKEN)
    ap.add_argument("--tls", action="store_true", help="Enable TLS (requires cert/key).")
    ap.add_argument("--certfile", default="certs/server.crt")
    ap.add_argument("--keyfile", default="certs/server.key")
    args = ap.parse_args()

    ssl_ctx = None
    if args.tls:
        ssl_ctx = build_ssl_context(args.certfile, args.keyfile)

    server = MonitorServer(args.host, args.port, args.auth_token)
    await server.start(ssl_ctx=ssl_ctx)

    await asyncio.gather(
        udp_discovery_responder(args.udp_bind, args.udp_port, args.port),
        interactive_cli(server),
    )

 
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
