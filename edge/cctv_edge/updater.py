"""Over-the-air updates of the edge software (plan 2.10).

Two halves share the files in <data dir>/update:

- the edge service (in its container) writes request.json when the cloud offers a release,
  removes it when the offer is withdrawn, and writes running.json after every heartbeat the
  cloud accepted;
- the updater (`python3 updater.py`, run as root on the box every minute by
  cctv-edge-update.timer, installed by provision.sh) pulls the requested image, checks its
  signature, switches the box to it and watches the new version's heartbeats. If the new
  version hasn't sent one within UPDATE_HEALTH_SECONDS, or keeps restarting, it switches
  back to the previous image. It writes status.json, which the service sends to the cloud.

The updater runs on the box's own Python, outside the image, so this module uses the
standard library only and stays compatible with Python 3.10. The request comes from the
container and is not trusted: the image must be in the box's own repository, pinned by
digest, and signed by our release workflow (checked with cosign).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("cctv_edge.updater")

REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"
RUNNING_FILE = "running.json"
STATE_FILE = "update-state.json"  # in the root-only config folder
MAX_FILE_BYTES = 64 * 1024

VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(-[0-9A-Za-z.]{1,30})?$")
IMAGE_RE = re.compile(r"^([a-z0-9][a-z0-9._/:-]{0,250})@sha256:[0-9a-f]{64}$")

DEFAULT_IMAGE = "ghcr.io/easyoffice2026/cctv-edge:latest"  # as in docker-compose.yml
# Images are signed keylessly by .github/workflows/release.yml on a v* tag; cosign checks
# the certificate GitHub issued to that workflow.
DEFAULT_SIGNER = (
    r"(?i)^https://github\.com/easyoffice2026/cctv/\.github/workflows/release\.yml"
    r"@refs/tags/v"
)
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_COSIGN_IMAGE = "ghcr.io/sigstore/cosign/cosign:v2.4.1"

# The new version must send a heartbeat at least HEALTHY_AFTER after the switch (so it ran
# that long and reached the cloud) and within UPDATE_HEALTH_SECONDS; more restarts than
# MAX_RESTARTS mean it crashes.
HEALTHY_AFTER_SECONDS = 120
UPDATE_HEALTH_SECONDS = 600
MAX_RESTARTS = 2

UPDATING, UPDATED, FAILED, ROLLED_BACK = "updating", "updated", "failed", "rolled_back"
DONE_STATES = (UPDATED, FAILED, ROLLED_BACK)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def repository(image: str) -> str:
    """ghcr.io/org/cctv-edge:1.2.0 or ...@sha256:... -> ghcr.io/org/cctv-edge."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name


# --- files ------------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any] | None:
    """A small JSON object, or None. Doesn't follow symlinks: the update folder belongs to
    the container's user."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Mapping[str, Any], mode: int = 0o644) -> None:
    """Written to a new temporary file and renamed over the old one, so a reader never sees
    half a file and a planted symlink is replaced, not followed."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f)
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- the service's half -------------------------------------------------------------------


def request_update(update_dir: Path, version: str, image: str, requested_at: datetime) -> bool:
    """Ask the updater for this release. Returns False when it was already asked for."""
    request = {"version": version, "image": image, "requested_at": _iso(requested_at)}
    if read_json(update_dir / REQUEST_FILE) == request:
        return False
    write_json(update_dir / REQUEST_FILE, request)
    log.info("software update to %s requested", version)
    return True


def cancel_request(update_dir: Path) -> None:
    (update_dir / REQUEST_FILE).unlink(missing_ok=True)


def mark_running(update_dir: Path, version: str, at: datetime) -> None:
    """This version just had a heartbeat accepted by the cloud."""
    write_json(update_dir / RUNNING_FILE, {"version": version, "heartbeat_at": _iso(at)})


def read_status(update_dir: Path) -> dict[str, Any] | None:
    """The updater's last report, with the fields the heartbeat needs, or None."""
    status = read_json(update_dir / STATUS_FILE)
    if status is None:
        return None
    if not all(isinstance(status.get(k), str) for k in ("version", "requested_at", "state", "at")):
        return None
    return status


# --- the updater's half (root, on the box) ----------------------------------------------

Runner = Callable[[Sequence[str], Mapping[str, str]], "subprocess.CompletedProcess[str]"]


def run_command(args: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed commands, no shell
        list(args),
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=40 * 60,
        check=False,
    )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace or add KEY=value, keeping the file's owner and mode."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = [line for line in lines if not line.strip().startswith(f"{key}=")]
    out.append(f"{key}={value}")
    st = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), st.st_mode & 0o777)
            if hasattr(os, "fchown"):
                os.fchown(f.fileno(), st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _tail(text: str, limit: int = 300) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "")[-limit:]


@dataclass
class Updater:
    data_dir: Path = Path("/var/lib/cctv-edge")
    conf_dir: Path = Path("/etc/cctv-edge")
    install_dir: Path = Path("/opt/cctv-edge")
    runner: Runner = run_command
    clock: Callable[[], datetime] = _now

    @property
    def update_dir(self) -> Path:
        return self.data_dir / "update"

    @property
    def env_path(self) -> Path:
        return self.conf_dir / "edge.env"

    @property
    def state_path(self) -> Path:
        return self.conf_dir / STATE_FILE

    def env(self) -> dict[str, str]:
        return read_env_file(self.env_path)

    # -- settings from edge.env

    def current_image(self) -> str:
        return self.env().get("CCTV_EDGE_IMAGE") or DEFAULT_IMAGE

    def allowed_repository(self) -> str:
        return self.env().get("CCTV_EDGE_UPDATE_REPOSITORY") or repository(self.current_image())

    # -- one run

    def run_once(self) -> str | None:
        """Do the next step of an update, if there is one. Returns the state written."""
        self.update_dir.mkdir(parents=True, exist_ok=True)
        pending = read_json(self.state_path)
        if pending:
            return self._watch(pending)
        request = read_json(self.update_dir / REQUEST_FILE)
        if request is None:
            return None
        status = read_status(self.update_dir)
        if (
            status is not None
            and status["state"] in DONE_STATES
            and status.get("version") == request.get("version")
            and status.get("requested_at") == request.get("requested_at")
        ):
            return None  # answered already; a retry comes with a new requested_at
        problem = self._check_request(request)
        if problem:
            return self._report(request, FAILED, problem)
        return self._switch(request)

    def _check_request(self, request: Mapping[str, Any]) -> str | None:
        version, image = request.get("version"), request.get("image")
        if not isinstance(version, str) or not VERSION_RE.match(version):
            return "refused: not a release version"
        if not isinstance(image, str) or not IMAGE_RE.match(image):
            return "refused: the image must be pinned by digest (repository@sha256:...)"
        if _parse_time(request.get("requested_at")) is None:
            return "refused: no request time"
        allowed = self.allowed_repository()
        if repository(image) != allowed:
            return f"refused: image not from {allowed}"
        return None

    def _report(self, request: Mapping[str, Any], state: str, detail: str | None) -> str:
        write_json(
            self.update_dir / STATUS_FILE,
            {
                "version": str(request.get("version", ""))[:50],
                "requested_at": str(request.get("requested_at", "")),
                "state": state,
                "detail": detail[:500] if detail else None,
                "at": _iso(self.clock()),
            },
        )
        (log.info if state in (UPDATING, UPDATED) else log.error)(
            "update to %s: %s%s", request.get("version"), state, f" ({detail})" if detail else ""
        )
        return state

    def _signature_problem(self, image: str) -> str | None:
        env = self.env()
        if env.get("CCTV_EDGE_UPDATE_UNSIGNED") == "yes":
            log.warning("signature check skipped (CCTV_EDGE_UPDATE_UNSIGNED=yes)")
            return None
        args = ["docker", "run", "--rm", "--user", "0:0", "--network", "host"]
        if Path("/root/.docker/config.json").is_file():  # a login for a private registry
            args += ["-e", "DOCKER_CONFIG=/docker", "-v", "/root/.docker:/docker:ro"]
        args += [
            env.get("CCTV_EDGE_COSIGN_IMAGE") or DEFAULT_COSIGN_IMAGE,
            "verify",
            "--certificate-identity-regexp",
            env.get("CCTV_EDGE_UPDATE_SIGNER") or DEFAULT_SIGNER,
            "--certificate-oidc-issuer",
            SIGNER_ISSUER,
            image,
        ]
        result = self.runner(args, {})
        if result.returncode != 0:
            return f"signature check failed: {_tail(result.stderr or result.stdout)}"
        return None

    def _compose(self, image: str) -> subprocess.CompletedProcess[str]:
        compose = str(self.install_dir / "docker-compose.yml")
        return self.runner(
            ["docker", "compose", "-f", compose, "up", "-d", "--no-deps", "edge"],
            {**self.env(), "CCTV_EDGE_IMAGE": image},
        )

    def _switch(self, request: Mapping[str, Any]) -> str:
        image = str(request["image"])
        current = self.current_image()
        if current == image:
            return self._report(request, UPDATED, None)
        self._report(request, UPDATING, "downloading")
        pulled = self.runner(["docker", "pull", image], {})
        if pulled.returncode != 0:
            return self._report(request, FAILED, f"download failed: {_tail(pulled.stderr)}")
        problem = self._signature_problem(image)
        if problem:
            return self._report(request, FAILED, problem)
        # Saved before switching, so a reboot halfway is picked up by the next run.
        pending = {**request, "previous_image": current, "switched_at": _iso(self.clock())}
        write_json(self.state_path, pending, mode=0o600)
        set_env_value(self.env_path, "CCTV_EDGE_IMAGE", image)
        started = self._compose(image)
        if started.returncode != 0:
            return self._rollback(pending, f"could not start: {_tail(started.stderr)}")
        return self._report(request, UPDATING, "started; waiting for its heartbeat")

    def _restarts(self) -> int:
        compose = str(self.install_dir / "docker-compose.yml")
        ps = self.runner(["docker", "compose", "-f", compose, "ps", "-q", "edge"], self.env())
        container = (ps.stdout or "").strip().splitlines()
        if ps.returncode != 0 or not container:
            return 0
        inspect = self.runner(
            ["docker", "inspect", "--format", "{{.RestartCount}}", container[0]], {}
        )
        try:
            return int((inspect.stdout or "0").strip())
        except ValueError:
            return 0

    def _watch(self, pending: Mapping[str, Any]) -> str:
        now = self.clock()
        switched_at = _parse_time(pending.get("switched_at")) or now
        running = read_json(self.update_dir / RUNNING_FILE) or {}
        beat = _parse_time(running.get("heartbeat_at"))
        restarts = self._restarts()
        if restarts > MAX_RESTARTS:
            return self._rollback(pending, f"the new version restarted {restarts} times")
        if (
            running.get("version") == pending.get("version")
            and beat is not None
            and beat >= switched_at + timedelta(seconds=HEALTHY_AFTER_SECONDS)
        ):
            self.state_path.unlink(missing_ok=True)
            self.runner(["docker", "image", "prune", "-f"], {})  # the previous image
            return self._report(pending, UPDATED, None)
        if now - switched_at > timedelta(seconds=UPDATE_HEALTH_SECONDS):
            minutes = UPDATE_HEALTH_SECONDS // 60
            return self._rollback(pending, f"no heartbeat from the new version in {minutes} min")
        return UPDATING

    def _rollback(self, pending: Mapping[str, Any], reason: str) -> str:
        previous = str(pending.get("previous_image") or DEFAULT_IMAGE)
        set_env_value(self.env_path, "CCTV_EDGE_IMAGE", previous)
        started = self._compose(previous)
        self.state_path.unlink(missing_ok=True)
        if started.returncode != 0:
            reason += f"; restarting the previous version failed: {_tail(started.stderr)}"
        return self._report(pending, ROLLED_BACK, reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a software update the cloud offered.")
    parser.add_argument("--data-dir", type=Path, default=Updater.data_dir)
    parser.add_argument("--conf-dir", type=Path, default=Updater.conf_dir)
    parser.add_argument("--install-dir", type=Path, default=Updater.install_dir)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    updater = Updater(args.data_dir, args.conf_dir, args.install_dir)
    state = updater.run_once()
    if state is not None:
        print(state)
    return 1 if state in (FAILED, ROLLED_BACK) else 0


if __name__ == "__main__":
    raise SystemExit(main())
