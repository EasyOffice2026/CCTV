"""NVR/camera credentials stored on the device.

The cloud config only names a `credentials_ref`; the username and password stay in a local
file and are added to the RTSP URL just before ffmpeg opens it. Never log a URL without
passing it through `redact` first.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_USERINFO = re.compile(r"(rtsps?://)[^/@\s]*@", re.IGNORECASE)


@dataclass(frozen=True)
class Credential:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credential(username={self.username!r}, password='***')"


def load_credentials(path: Path) -> dict[str, Credential]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {ref: Credential(item["username"], item["password"]) for ref, item in raw.items()}


def with_credentials(url: str, credential: Credential | None) -> str:
    if credential is None:
        return url
    scheme, rest = url.split("://", 1)
    userinfo = f"{quote(credential.username, safe='')}:{quote(credential.password, safe='')}"
    return f"{scheme}://{userinfo}@{rest}"


def redact(text: str) -> str:
    return _USERINFO.sub(r"\1***@", text)
