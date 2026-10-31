"""The device token, after rotation (plan 7.1).

Pairing puts the first token in CCTV_EDGE_DEVICE_TOKEN (/etc/cctv-edge/edge.env). When
the cloud asks for a rotation, the new token is written to <data_dir>/device_token
(mode 600) together with a hash of the paired token it descends from. At start-up that
file wins over the environment, unless the box was paired again (a different token in
the environment), in which case the file is stale and ignored.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path, paired_token: str) -> None:
        self.path = path
        self.paired_token = paired_token

    def load(self) -> str:
        """The newest token: the rotated one if it descends from the paired token."""
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.paired_token
        except (OSError, ValueError):
            log.exception("can't read %s; using the paired token", self.path)
            return self.paired_token
        if saved.get("paired") != _fingerprint(self.paired_token):
            log.info("the box was paired again; ignoring the old rotated token")
            return self.paired_token
        return saved["token"]

    def writable(self) -> bool:
        return os.access(self.path.parent, os.W_OK) and (
            not self.path.exists() or os.access(self.path, os.W_OK)
        )

    def save(self, token: str) -> None:
        """Atomically, readable by this user only. Raises OSError on failure."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token, "paired": _fingerprint(self.paired_token)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
