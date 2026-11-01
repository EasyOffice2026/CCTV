"""Plan 2.10: over-the-air updates. The service's half (request, running, status) and the
box updater's half (pull, signature, switch, health watch, rollback) with a fake docker."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cctv_edge import updater
from cctv_edge.updater import Updater

REPO = "ghcr.io/easyoffice2026/cctv-edge"
OLD = f"{REPO}:1.0.0"
NEW = f"{REPO}@sha256:{'a' * 64}"
REQUESTED = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.fail: dict[str, str] = {}  # first word after docker (or "verify") -> stderr
        self.restarts = 0

    def __call__(self, args, env):
        args = list(args)
        self.calls.append(args)
        self.envs.append(dict(env))
        key = "verify" if "verify" in args else args[1]
        if key == "compose" and "ps" in args:
            return subprocess.CompletedProcess(args, 0, "c0ffee\n", "")
        if key == "inspect":
            return subprocess.CompletedProcess(args, 0, f"{self.restarts}\n", "")
        if key in self.fail:
            return subprocess.CompletedProcess(args, 1, "", self.fail[key])
        return subprocess.CompletedProcess(args, 0, "", "")

    def ran(self, word: str) -> list[list[str]]:
        return [c for c in self.calls if word in c]


class Clock:
    def __init__(self) -> None:
        self.now = REQUESTED + timedelta(minutes=1)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def box(tmp_path: Path):
    data, conf, install = tmp_path / "data", tmp_path / "conf", tmp_path / "opt"
    for folder in (data / "update", conf, install):
        folder.mkdir(parents=True)
    (conf / "edge.env").write_text(
        f"CCTV_EDGE_CLOUD_URL=https://cloud.test\nCCTV_EDGE_DEVICE_TOKEN=x\nCCTV_EDGE_IMAGE={OLD}\n"
    )
    docker, clock = FakeDocker(), Clock()
    return Updater(data, conf, install, runner=docker, clock=clock), docker, clock


def ask(u: Updater, image: str = NEW, version: str = "1.1.0", at: datetime = REQUESTED) -> None:
    updater.request_update(u.update_dir, version, image, at)


def status(u: Updater) -> dict:
    return json.loads((u.update_dir / updater.STATUS_FILE).read_text())


def beat(u: Updater, version: str, at: datetime) -> None:
    updater.mark_running(u.update_dir, version, at)


def test_update_switches_and_confirms(box):
    u, docker, clock = box
    assert u.run_once() is None  # nothing asked for
    ask(u)
    assert u.run_once() == "updating"
    assert docker.ran("pull") == [["docker", "pull", NEW]]
    [verify] = docker.ran("verify")
    assert verify[verify.index("--certificate-oidc-issuer") + 1] == updater.SIGNER_ISSUER
    assert verify[-1] == NEW
    [up] = docker.ran("up")
    assert up[-1] == "edge" and docker.envs[docker.calls.index(up)]["CCTV_EDGE_IMAGE"] == NEW
    assert f"CCTV_EDGE_IMAGE={NEW}" in (u.conf_dir / "edge.env").read_text()
    assert "CCTV_EDGE_DEVICE_TOKEN=x" in (u.conf_dir / "edge.env").read_text()
    assert status(u)["state"] == "updating"

    # A heartbeat right after the start isn't enough: it must have run 2 minutes.
    beat(u, "1.1.0", clock.now + timedelta(seconds=30))
    clock.now += timedelta(seconds=30)
    assert u.run_once() == "updating"
    beat(u, "1.1.0", clock.now + timedelta(minutes=2))
    clock.now += timedelta(minutes=2)
    assert u.run_once() == "updated"
    assert status(u) | {"at": None} == {
        "version": "1.1.0",
        "requested_at": REQUESTED.isoformat(),
        "state": "updated",
        "detail": None,
        "at": None,
    }
    assert not u.state_path.exists()
    # Answered: the same request isn't run again.
    calls = len(docker.calls)
    assert u.run_once() is None
    assert len(docker.calls) == calls


def test_no_heartbeat_rolls_back(box):
    u, docker, clock = box
    ask(u)
    u.run_once()
    beat(u, "1.0.0", clock.now)  # the old version's last heartbeat
    clock.now += timedelta(minutes=9)
    assert u.run_once() == "updating"
    clock.now += timedelta(minutes=2)
    assert u.run_once() == "rolled_back"
    assert f"CCTV_EDGE_IMAGE={OLD}" in (u.conf_dir / "edge.env").read_text()
    assert "up" in docker.calls[-1] and docker.envs[-1]["CCTV_EDGE_IMAGE"] == OLD
    assert "no heartbeat from the new version" in status(u)["detail"]
    assert u.run_once() is None  # not retried until the cloud asks again

    # Asking again (a retry from the admin panel) has a new time.
    ask(u, at=REQUESTED + timedelta(hours=1))
    assert u.run_once() == "updating"


def test_crash_loop_rolls_back_early(box):
    u, docker, _ = box
    ask(u)
    u.run_once()
    docker.restarts = 3
    assert u.run_once() == "rolled_back"
    assert status(u)["detail"] == "the new version restarted 3 times"


def test_bad_signature_or_download_keeps_the_running_version(box):
    u, docker, _ = box
    docker.fail["verify"] = "Error: no matching signatures"
    ask(u)
    assert u.run_once() == "failed"
    assert status(u)["detail"] == "signature check failed: Error: no matching signatures"
    assert not docker.ran("up")
    assert f"CCTV_EDGE_IMAGE={OLD}" in (u.conf_dir / "edge.env").read_text()

    docker.fail = {"pull": "manifest unknown"}
    ask(u, at=REQUESTED + timedelta(hours=1))
    assert u.run_once() == "failed"
    assert status(u)["detail"] == "download failed: manifest unknown"


@pytest.mark.parametrize(
    ("image", "version", "detail"),
    [
        (f"{REPO}:latest", "1.1.0", "pinned by digest"),
        (f"ghcr.io/someone-else/cctv-edge@sha256:{'b' * 64}", "1.1.0", f"not from {REPO}"),
        (NEW, "latest", "not a release version"),
    ],
)
def test_requests_from_the_container_are_checked(box, image, version, detail):
    u, docker, _ = box
    ask(u, image=image, version=version)
    assert u.run_once() == "failed"
    assert detail in status(u)["detail"]
    assert docker.calls == []


def test_unsigned_updates_only_when_allowed(box):
    u, docker, _ = box
    with (u.conf_dir / "edge.env").open("a") as f:
        f.write("CCTV_EDGE_UPDATE_UNSIGNED=yes\n")
    ask(u)
    assert u.run_once() == "updating"
    assert docker.ran("verify") == []


def test_request_file_is_rewritten_only_on_change(tmp_path):
    assert updater.request_update(tmp_path, "1.1.0", NEW, REQUESTED)
    assert not updater.request_update(tmp_path, "1.1.0", NEW, REQUESTED)
    assert updater.request_update(tmp_path, "1.1.0", NEW, REQUESTED + timedelta(hours=1))
    updater.cancel_request(tmp_path)
    assert updater.read_json(tmp_path / updater.REQUEST_FILE) is None


def test_repository_names():
    assert updater.repository(f"{REPO}:1.0.0") == REPO
    assert updater.repository(NEW) == REPO
    assert updater.repository("registry.local:5000/cctv-edge:1") == "registry.local:5000/cctv-edge"
    assert updater.repository("registry.local:5000/cctv-edge") == "registry.local:5000/cctv-edge"


def test_updater_runs_on_the_standard_library_alone():
    """The box runs updater.py with its own Python, without the image's packages."""
    script = Path(updater.__file__)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--data-dir" in result.stdout


def test_version_comes_from_the_image_or_the_package():
    from cctv_edge import __version__
    from cctv_edge.settings import EdgeSettings

    assert EdgeSettings(version="1.4.0").version == "1.4.0"
    assert EdgeSettings(version="").version == __version__  # built without VERSION
