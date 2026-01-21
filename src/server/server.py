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
    def __init__(self, host: str, port: int, auth_token: str) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token

        self.clients: Dict[str, ClientState] = {}
        self.last_responses: Dict[str, dict] = {}

        self._server: Optional[asyncio.AbstractServer] = None
        self.on_response: Optional[ResponseCallback] = None

    async def start(self, ssl_ctx: Optional[ssl.SSLContext] = None) -> None:
        self._server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port,
            ssl=ssl_ctx,
        )
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"[TCP] Server listening on {addrs}")

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
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

            payload = msg.get("payload", {})
            client_id = payload.get("client_id", "")
            name = payload.get("name", "unknown")

            if not client_id:
                writer.write(dumps({"type": "error", "payload": {"reason": "missing client_id"}}))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            self.clients[client_id] = ClientState(
                client_id=client_id,
                name=name,
                addr=peer,
                writer=writer,
            )
            print(f"[+] Client {client_id} ({name}) connected from {peer}")

            writer.write(dumps({"type": "auth_ok", "payload": {"server_time": time.time()}}))
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break

                msg = loads(line)
                client = self.clients.get(client_id)
                if client:
                    client.last_seen = time.time()

                msg_type = msg.get("type")
                payload = msg.get("payload", {})

                if msg_type == "metrics":
                    if client:
                        client.last_metrics = payload

                elif msg_type == "response":
                    request_id = msg.get("request_id")
                    if request_id:
                        self.last_responses[request_id] = {
                            "client_id": client_id,
                            "payload": payload,
                            "t": time.time(),
                        }

                    if self.on_response:
                        try:
                            await self.on_response(client_id, request_id, payload)
                        except Exception as exc:
                            print(f"[!] on_response callback error: {exc}")

                    print(
                        f"[response] client={client_id} "
                        f"request_id={request_id} "
                        f"payload={json.dumps(payload, ensure_ascii=False)}"
                    )

                elif msg_type == "heartbeat":
                    pass

                else:
                    print(f"[?] Unknown message from {client_id}: {msg}")

        except Exception as exc:
            print(f"[!] Error with client {peer}: {exc}")

        finally:
            if client_id and client_id in self.clients:
                print(f"[-] Client {client_id} disconnected")
                self.clients.pop(client_id, None)

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def send_request(
        self,
        client_id: str,
        req_type: str,
        request_id: str,
        payload: dict,
    ) -> None:
        client = self.clients.get(client_id)
        if not client:
            raise ValueError("client not found")

        msg = {
            "v": PROTOCOL_VERSION,
            "type": "request",
            "request_id": request_id,
            "payload": {"req_type": req_type, "data": payload},
        }
        client.writer.write(dumps(msg))
        await client.writer.drain()

    def list_clients(self):
        now = time.time()
        rows = []

        for cid, client in self.clients.items():
            age = now - client.last_seen
            rows.append(
                (
                    cid,
                    client.name,
                    f"{client.addr[0]}:{client.addr[1]}",
                    f"{age:0.1f}s",
                    client.last_metrics,
                )
            )
        return rows


async def udp_discovery_responder(
    bind_ip: str,
    port: int,
    tcp_port: int,
) -> None:
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


async def interactive_cli(server: MonitorServer) -> None:
    """Simple interactive CLI for demo."""
    print("\nCommands:")
    print("  list                          - show connected clients")
    print("  req <client_id> sysinfo       - request system info")
    print("  req <client_id> processes     - request process list")
    print("  req <client_id> netstat       - request network summary")
    print("  help                          - show commands")
    print("  quit                          - exit\n")

    counter = 0
    loop = asyncio.get_running_loop()

    while True:
        cmd = await loop.run_in_executor(None, lambda: input("monitor> ").strip())
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
                print(
                    f"- {cid:12} | {name:18} | {addr:21} | "
                    f"last_seen={age:>6} | cpu={cpu}% mem={mem}%"
                )
            continue

        if cmd.startswith("req "):
            parts = cmd.split()
            if len(parts) < 3:
                print("Usage: req <client_id> <sysinfo|processes|netstat>")
                continue

            _, cid, req_type = parts[:3]
            counter += 1
            request_id = f"r{counter}"

            try:
                await server.send_request(cid, req_type, request_id, {})
                print(f"sent request {req_type} to {cid} (request_id={request_id})")
            except Exception as exc:
                print(f"error: {exc}")
            continue

        print("Unknown command. Type 'help'.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="PC Network Monitor - Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=TCP_SERVER_PORT)
    parser.add_argument("--udp-bind", default="0.0.0.0")
    parser.add_argument("--udp-port", type=int, default=UDP_DISCOVERY_PORT)
    parser.add_argument("--auth-token", default=DEFAULT_AUTH_TOKEN)
    parser.add_argument("--tls", action="store_true", help="Enable TLS")
    parser.add_argument("--certfile", default="certs/server.crt")
    parser.add_argument("--keyfile", default="certs/server.key")
    args = parser.parse_args()

    ssl_ctx = build_ssl_context(args.certfile, args.keyfile) if args.tls else None

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
