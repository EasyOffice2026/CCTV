"""Last config received from the cloud, kept on disk so the box works after a reboot
even when the internet is down."""

import logging
import os
from pathlib import Path

from pydantic import ValidationError

from cctv_contracts.v1 import EdgeConfig

log = logging.getLogger(__name__)


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> EdgeConfig | None:
        if not self.path.exists():
            return None
        try:
            return EdgeConfig.model_validate_json(self.path.read_bytes())
        except ValidationError:
            log.exception("stored config %s is invalid; ignoring it", self.path)
            return None

    def save(self, config: EdgeConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never leaves a half-written config
