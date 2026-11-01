"""Entry point: `python -m cctv_edge` (or `cctv-edge`). `--healthcheck` is used by Docker;
`--check` is the install-day site check (cameras, NVRs, cloud, clock, disk; plan 9.1)."""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from cctv_edge import sitecheck
from cctv_edge.cloud import CloudClient
from cctv_edge.credentials import load_credentials
from cctv_edge.outbox import Outbox
from cctv_edge.service import EdgeService
from cctv_edge.settings import get_settings
from cctv_edge.token_store import TokenStore

log = logging.getLogger("cctv_edge")

# The housekeeping loop touches the alive file every 10 s.
ALIVE_MAX_AGE_SECONDS = 60


def healthcheck() -> int:
    path = get_settings().alive_path
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return 1
    return 0 if age < ALIVE_MAX_AGE_SECONDS else 1


def site_check(as_json: bool) -> int:
    settings = get_settings()
    tokens = TokenStore(settings.token_path, settings.device_token.get_secret_value())
    client = CloudClient(settings.cloud_url, tokens.load(), settings.http_timeout_seconds)
    try:
        findings = sitecheck.run(settings, client)
    finally:
        client.close()
    print(sitecheck.report(findings, as_json))
    return 1 if any(f.level == "fail" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv-edge")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 if the service is alive")
    parser.add_argument("--check", action="store_true", help="site check; exit 1 on failures")
    parser.add_argument("--json", action="store_true", help="with --check: JSON output")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return healthcheck()
    if args.check:
        return site_check(args.json)

    logging.basicConfig(
        level=os.environ.get("CCTV_EDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    token = settings.device_token.get_secret_value()
    if not token:
        log.error("CCTV_EDGE_DEVICE_TOKEN is not set")
        return 2

    tokens = TokenStore(settings.token_path, token)
    client = CloudClient(settings.cloud_url, tokens.load(), settings.http_timeout_seconds)
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(
        settings,
        client,
        outbox,
        credentials=load_credentials(settings.credentials_file),
        tokens=tokens,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    service.start()
    log.info("edge service started (cloud %s)", settings.cloud_url)
    while not stop.wait(1):  # a timeout keeps Ctrl+C working on Windows
        stalled = service.stalled_loops()
        if stalled:
            log.critical("loops %s are stuck; exiting so the container restarts", stalled)
            return 1
    log.info("shutting down")
    service.shutdown()
    outbox.close()
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
