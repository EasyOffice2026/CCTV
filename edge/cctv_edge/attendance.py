"""Attendance on a camera: faces -> match against enrolled staff -> sightings.

The cloud turns sightings into check-in (first of the work day) and check-out (last).
One sighting per staff member and camera per `sighting_interval_s` is enough for that.
Faces that match nobody are dropped right here: never stored, logged or sent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import numpy as np

from cctv_contracts.v1 import StaffSighting
from cctv_edge.face import FaceAnalyzer, FaceGallery, FaceQuality


@dataclass(frozen=True)
class AttendanceSettings:
    match_threshold: float = 0.40
    min_face_px: float = 60
    min_sharpness: float = 30.0
    min_detector_score: float = 0.85
    sighting_interval_s: float = 60

    @classmethod
    def from_params(cls, params: Mapping[str, float]) -> "AttendanceSettings":
        return cls(**{k: v for k, v in params.items() if k in cls.__dataclass_fields__})

    @property
    def quality(self) -> FaceQuality:
        return FaceQuality(self.min_face_px, self.min_sharpness, self.min_detector_score)


class AttendanceWatcher:
    def __init__(
        self,
        camera_id: UUID,
        analyzer: FaceAnalyzer,
        gallery: FaceGallery,
        settings: AttendanceSettings,
    ) -> None:
        self.camera_id = camera_id
        self.analyzer = analyzer
        self.gallery = gallery
        self.settings = settings
        self._last_sent: dict[UUID, float] = {}

    def process(self, frame: np.ndarray, ts: float) -> list[StaffSighting]:
        # Nobody enrolled (or templates not loaded yet): don't look at faces at all.
        if len(self.gallery) == 0 or self.gallery.model_version != self.analyzer.model_version:
            return []
        sightings = []
        for face in self.analyzer.analyze(frame, self.settings.quality):
            match = self.gallery.match(face.embedding, self.settings.match_threshold)
            if match is None:
                continue  # not a member of staff: forgotten
            staff_id, similarity = match
            if (
                ts - self._last_sent.get(staff_id, float("-inf"))
                < self.settings.sighting_interval_s
            ):
                continue
            self._last_sent[staff_id] = ts
            sightings.append(
                StaffSighting(
                    staff_id=staff_id,
                    camera_id=self.camera_id,
                    seen_at=datetime.fromtimestamp(ts, timezone.utc),
                    similarity=round(min(max(similarity, 0.0), 1.0), 4),
                )
            )
        return sightings
