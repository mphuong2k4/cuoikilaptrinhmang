from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import ssl
import time
from typing import Optional, Tuple

try:
    import psutil  
except Exception:
    psutil = None

from src.shared.protocol import (
    TCP_SERVER_PORT,
    UDP_DISCOVERY_PORT,
    DEFAULT_AUTH_TOKEN,
    dumps,
    loads,
    new_client_id,
    PROTOCOL_VERSION,
)


def get_basic_sysinfo():
    """
    Trả về thông tin hệ điều hành CHUẨN.
    Windows 11 được xác định theo OS Build >= 22000
    (do nhiều bản Win11 registry vẫn ghi ProductName = Windows 10)
    """
    os_str = f"{platform.system()} {platform.release()}"

    if platform.system().lower() == "windows":
        try:
            import winreg

            key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                product_name, _ = winreg.QueryValueEx(key, "ProductName")
                display_version, _ = winreg.QueryValueEx(key, "DisplayVersion")
                build, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")

            build_int = int(build)

            if build_int >= 22000:
                os_str = f"Windows 11 ({display_version}, build {build})"
            else:
                os_str = f"{product_name} ({display_version}, build {build})"

        except Exception:
            os_str = f"{platform.system()} {platform.release()}"

    return {
        "hostname": platform.node(),
        "os": os_str,
        "python": platform.python_version(),
        "arch": platform.machine(),
    }



def get_metrics():
    if psutil is None:
        return {"cpu_percent": None, "mem_percent": None, "disk_percent": None}
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage(os.path.abspath(os.sep)).percent,
    }


def get_processes_top(n: int = 30):
    if psutil is None:
        return {"error": "psutil not installed"}
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            procs.append(info)
        except Exception:
            pass
    procs.sort(key=lambda x: (x.get("cpu_percent") or 0), reverse=True)
    return procs[:n]


def get_net_connections_summary(limit: int = 50):
    if psutil is None:
        return {"error": "psutil not installed"}
    conns = []
    for c in psutil.net_connections(kind="inet"):
        try:
            laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
            raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
            conns.append({
                "fd": c.fd,
                "type": str(c.type),
                "status": c.status,
                "laddr": laddr,
                "raddr": raddr,
                "pid": c.pid,
            })
        except Exception:
            pass
    return conns[:limit]


async def discover_server(timeout: float = 2.0) -> Optional[Tuple[str, int]]:
    """UDP broadcast discovery: find server on LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    msg = b"DISCOVER_PC_MONITOR"
    try:
        sock.sendto(msg, ("255.255.255.255", UDP_DISCOVERY_PORT))
        data, addr = sock.recvfrom(2048)
        payload = json.loads(data.decode("utf-8"))
        return addr[0], int(payload.get("tcp_port", TCP_SERVER_PORT))
    except Exception:
        return None
    finally:
        sock.close()


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def run_agent(server_host: str, server_port: int, auth_token: str, name: str, client_id: str, use_tls: bool):
    ssl_ctx = build_ssl_context() if use_tls else None
    reader, writer = await asyncio.open_connection(server_host, server_port, ssl=ssl_ctx)
    writer.write(dumps({
        "v": PROTOCOL_VERSION,
        "type": "auth",
        "payload": {
            "token": auth_token,
            "client_id": client_id,
            "name": name,
            "sysinfo": get_basic_sysinfo(),
        }
    }))
    await writer.drain()
    line = await reader.readline()
    if not line:
        raise RuntimeError("server closed")
    resp = loads(line)
    if resp.get("type") != "auth_ok":
        raise RuntimeError(f"auth failed: {resp}")
    print(f"[+] Connected & authenticated to {server_host}:{server_port} as {client_id}")

    async def send_loop():
        while True:
             writer.write(
                dumps({
                    "v": PROTOCOL_VERSION,
                    "type": "heartbeat",
                    "payload": {"t": time.time()},
                }) +
                dumps({
                    "v": PROTOCOL_VERSION,
                    "type": "metrics",
                    "payload": get_metrics(),
                })
            )
            await writer.drain()
            await asyncio.sleep(2.0)

    async def recv_loop():
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = loads(line)
            if msg.get("type") == "request":
                rid = msg.get("request_id")
                req_type = msg.get("payload", {}).get("req_type")
                if req_type == "sysinfo":
                    payload = {"sysinfo": get_basic_sysinfo(), "metrics": get_metrics()}
                elif req_type == "processes":
                    payload = {"processes": get_processes_top(30)}
                elif req_type == "netstat":
                    payload = {"connections": get_net_connections_summary(50)}
                else:
                    payload = {"error": f"unknown req_type: {req_type}"}
                writer.write(dumps({"v": PROTOCOL_VERSION, "type": "response", "request_id": rid, "payload": payload}))
                await writer.drain()

    await asyncio.gather(send_loop(), recv_loop())


async def main():
    ap = argparse.ArgumentParser(description="PC Network Monitor - Agent")
    ap.add_argument("--server-host", default="")
    ap.add_argument("--server-port", type=int, default=TCP_SERVER_PORT)
    ap.add_argument("--discover", action="store_true", help="Use UDP broadcast discovery to find server.")
    ap.add_argument("--auth-token", default=DEFAULT_AUTH_TOKEN)
    ap.add_argument("--name", default=platform.node())
    ap.add_argument("--client-id", default="")
    ap.add_argument("--tls", action="store_true", help="Enable TLS (server must run with --tls).")
    args = ap.parse_args()

    client_id = args.client_id or new_client_id()

    server_host = args.server_host
    server_port = args.server_port

    if args.discover or not server_host:
        found = await discover_server()
        if not found:
            raise RuntimeError("Could not discover server. Provide --server-host or ensure LAN broadcast works.")
        server_host, server_port = found
        print(f"[UDP] Discovered server: {server_host}:{server_port}")

    await run_agent(server_host, server_port, args.auth_token, args.name, client_id, args.tls)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
