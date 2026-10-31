/**
 * Default thresholds of each feature, as the edge box applies them (edge/cctv_edge/rules.py,
 * documented in edge/README.md). Shown in Settings › Features so a value can be changed
 * without knowing the keys. Keep in sync with the edge.
 *
 * Per-rule keys (under the event type): enabled, min_duration_s, max_gap_s, cooldown_s,
 * min_confidence, severity. The other keys apply to the whole feature.
 */
export const FEATURE_PARAM_DEFAULTS: Record<string, Record<string, unknown>> = {
  security: {
    after_hours_person: { enabled: true, min_duration_s: 3, cooldown_s: 300, severity: "critical" },
    after_hours_motion: { enabled: true, min_duration_s: 2, cooldown_s: 600, severity: "warning" },
    restricted_area_entry: { enabled: true, min_duration_s: 2, cooldown_s: 120, severity: "critical" },
  },
  cashier: {
    drawer_open_threshold: 0.6,
    counter_unattended: { enabled: true, min_duration_s: 60, max_gap_s: 2, cooldown_s: 300, severity: "warning" },
    drawer_open: { enabled: true, min_duration_s: 1, max_gap_s: 2, cooldown_s: 5, severity: "info" },
  },
  kitchen_compliance: {
    min_crop_height_px: 96,
    min_sharpness: 20,
    worn_threshold: 0.6,
    missing_threshold: 0.3,
    classify_interval_s: 1,
    missing_mask: { enabled: true, min_duration_s: 5, max_gap_s: 3, cooldown_s: 900 },
    missing_gloves: { enabled: true, min_duration_s: 5, max_gap_s: 3, cooldown_s: 900 },
    missing_hair_cap: { enabled: true, min_duration_s: 5, max_gap_s: 3, cooldown_s: 900 },
  },
  attendance: {
    match_threshold: 0.4,
    min_face_px: 60,
    min_sharpness: 30,
    min_detector_score: 0.85,
    sighting_interval_s: 60,
  },
};
