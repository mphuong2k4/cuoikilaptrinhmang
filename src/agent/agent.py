from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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

DISCOVERY_MAGIC = b"DISCOVER_PC_MONITOR"
HEARTBEAT_INTERVAL_S = 2.0
METRICS_INTERVAL_S = 2.0

RECONNECT_BASE_S = 0.5
RECONNECT_MAX_S = 15.0


@dataclass(frozen=True)
class AgentConfig:
    server_host: str
    server_port: int
    auth_token: str
    name: str
    client_id: str
    use_tls: bool


def get_basic_sysinfo() -> Dict[str, Any]:
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


def get_metrics() -> Dict[str, Any]:
    if psutil is None:
        return {"cpu_percent": None, "mem_percent": None, "disk_percent": None}

    try:
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(os.path.abspath(os.sep)).percent
        return {"cpu_percent": cpu, "mem_percent": mem, "disk_percent": disk}
    except Exception:
        return {"cpu_percent": None, "mem_percent": None, "disk_percent": None}


def get_processes_top(n: int = 30) -> Any:
    if psutil is None:
        return {"error": "psutil not installed"}

    procs = []
    try:
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except Exception:
                pass
        procs.sort(key=lambda x: (x.get("cpu_percent") or 0), reverse=True)
        return procs[:n]
    except Exception as e:
        return {"error": f"failed to list processes: {e}"}


def get_net_connections_summary(limit: int = 50) -> Any:
    if psutil is None:
        return {"error": "psutil not installed"}

    conns = []
    try:
        for c in psutil.net_connections(kind="inet"):
            try:
                laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
                raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
                conns.append(
                    {
                        "fd": c.fd,
                        "type": str(c.type),
                        "status": c.status,
                        "laddr": laddr,
                        "raddr": raddr,
                        "pid": c.pid,
                    }
                )
                if len(conns) >= limit:
                    break
            except Exception:
                pass
        return conns
    except Exception as e:
        return {"error": f"failed to list connections: {e}"}


async def discover_server(timeout: float = 2.0) -> Optional[Tuple[str, int]]:
    """UDP broadcast discovery: find server on LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)

    try:
        sock.sendto(DISCOVERY_MAGIC, ("255.255.255.255", UDP_DISCOVERY_PORT))
        data, addr = sock.recvfrom(2048)
        payload = json.loads(data.decode("utf-8"))
        return addr[0], int(payload.get("tcp_port", TCP_SERVER_PORT))
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _pack_auth(cfg: AgentConfig) -> bytes:
    return dumps(
        {
            "v": PROTOCOL_VERSION,
            "type": "auth",
            "payload": {
                "token": cfg.auth_token,
                "client_id": cfg.client_id,
                "name": cfg.name,
                "sysinfo": get_basic_sysinfo(),
            },
        }
    )


def _pack_heartbeat() -> bytes:
    return dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", "payload": {"t": time.time()}})


def _pack_metrics() -> bytes:
    return dumps({"v": PROTOCOL_VERSION, "type": "metrics", "payload": get_metrics()})


def _pack_response(request_id: Any, payload: Dict[str, Any]) -> bytes:
    return dumps(
        {
            "v": PROTOCOL_VERSION,
            "type": "response",
            "request_id": request_id,
            "payload": payload,
        }
    )


async def _authenticate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cfg: AgentConfig) -> None:
    writer.write(_pack_auth(cfg))
    await writer.drain()

    line = await reader.readline()
    if not line:
        raise RuntimeError("server closed during auth")

    resp = loads(line)
    if resp.get("type") != "auth_ok":
        raise RuntimeError(f"auth failed: {resp}")


async def _send_loop(writer: asyncio.StreamWriter, stop: asyncio.Event) -> None:
    """
    Gửi heartbeat + metrics định kỳ.
    Tách interval vẫn đồng bộ 2s; nếu bạn muốn khác nhau, có thể tách 2 task.
    """
    next_hb = time.monotonic()
    next_metrics = time.monotonic()

    while not stop.is_set():
        now = time.monotonic()
        out = b""

        if now >= next_hb:
            out += _pack_heartbeat()
            next_hb = now + HEARTBEAT_INTERVAL_S

        if now >= next_metrics:
            out += _pack_metrics()
            next_metrics = now + METRICS_INTERVAL_S

        if out:
            writer.write(out)
            await writer.drain()

        await asyncio.sleep(0.1)


async def _recv_loop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stop: asyncio.Event) -> None:
    while not stop.is_set():
        line = await reader.readline()
        if not line:
            break

        try:
            msg = loads(line)
        except Exception:
            continue

        if msg.get("type") != "request":
            continue

        rid = msg.get("request_id")
        req_type = (msg.get("payload") or {}).get("req_type")

        if req_type == "sysinfo":
            payload = {"sysinfo": get_basic_sysinfo(), "metrics": get_metrics()}
        elif req_type == "processes":
            payload = {"processes": get_processes_top(30)}
        elif req_type == "netstat":
            payload = {"connections": get_net_connections_summary(50)}
        else:
            payload = {"error": f"unknown req_type: {req_type}"}

        try:
            writer.write(_pack_response(rid, payload))
            await writer.drain()
        except Exception:
            break


async def run_agent_once(cfg: AgentConfig) -> None:
    ssl_ctx = build_ssl_context() if cfg.use_tls else None
    reader, writer = await asyncio.open_connection(cfg.server_host, cfg.server_port, ssl=ssl_ctx)

    try:
        await _authenticate(reader, writer, cfg)
        print(f"[+] Connected & authenticated to {cfg.server_host}:{cfg.server_port} as {cfg.client_id}")

        stop = asyncio.Event()
        send_task = asyncio.create_task(_send_loop(writer, stop), name="send_loop")
        recv_task = asyncio.create_task(_recv_loop(reader, writer, stop), name="recv_loop")

        done, pending = await asyncio.wait(
            {send_task, recv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        stop.set()
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for t in done:
            exc = t.exception()
            if exc:
                raise exc

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_agent_forever(cfg: AgentConfig) -> None:
    backoff = RECONNECT_BASE_S
    while True:
        try:
            await run_agent_once(cfg)
            backoff = RECONNECT_BASE_S
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[!] Disconnected: {e}. Reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX_S, backoff * 2)


async def main() -> None:
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

    cfg = AgentConfig(
        server_host=server_host,
        server_port=server_port,
        auth_token=args.auth_token,
        name=args.name,
        client_id=client_id,
        use_tls=args.tls,
    )

    await run_agent_forever(cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
