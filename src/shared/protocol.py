"""Shared protocol definitions for the PC Network Monitor project.

JSON line-delimited (NDJSON) over TCP.
Each message is a JSON object terminated by '\n'.

Security note: This project is intended for **explicitly authorized** monitoring only.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "1.0"

UDP_DISCOVERY_PORT = 9999
TCP_SERVER_PORT = 9009

DEFAULT_AUTH_TOKEN = "NHOM8-DEMO-TOKEN"


def dumps(obj: Dict[str, Any]) -> bytes:
    """Encode a message to NDJSON bytes."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def loads(line: bytes) -> Dict[str, Any]:
    """Decode a NDJSON line to dict."""
    return json.loads(line.decode("utf-8"))


def new_client_id() -> str:
    return secrets.token_urlsafe(8)


@dataclass
class Message:
    """Helper wrapper (optional)."""
    type: str
    payload: Dict[str, Any]
    request_id: Optional[str] = None
    version: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]: 
        d: Dict[str, Any] = {
            "v": self.version,
            "type": self.type,
            "payload": self.payload,
        }
        if self.request_id is not None:
            d["request_id"] = self.request_id
        return d
