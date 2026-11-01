# cctv-ml: labelling, dataset split and evaluation

Tools for plan items **5.0** (labelling setup) and **5.5** (per-feature evaluation
report). There is no training code yet: it depends on the model and licence decision
(plan §1 #12, see [Model licences](#model-licences)).

Footage, frames, crops, label exports and model files **never go into git**. They live on
the project's approved storage (for example `D:\cctv-data\` or `/srv/cctv-data/`);
`ml/.gitignore` blocks the usual file types as a safety net. Read the privacy rules in
[labelling/guides/README.md](labelling/guides/README.md) before touching any footage.

## The loop for every feature

```
label ─► train / configure ─► offline evaluation on held-out footage ─► shadow mode on site ─► tune ─► enable alerts
  ▲                                                                                             │
  └──────────────────── false alarms and misses from review become new labels ◄────────────────┘
```

1. **Label** in Label Studio with the configs in [labelling/configs](labelling/configs) and
   the guides in [labelling/guides](labelling/guides).
2. **Split** footage by camera-day with `cctv_ml.split`; train and tune only on `train`
   and `val`.
3. **Train or configure**: train the model (detector, PPE or drawer classifier) or, for
   rule-only features, set zones, durations and thresholds.
4. **Evaluate offline** on the `test` split: replay the footage through the edge pipeline
   (the edge replay tool writes `events.jsonl`) and run `cctv_ml.evaluate`; for
   classifiers run `cctv_ml.classify_eval` on the test crops. The report is the feature's
   evaluation report (definition of done, item 4) and must meet the 90% target, or an
   agreed adjusted target, before shadow mode.
5. **Shadow mode** on site (plan 9.4): events are reviewed daily as valid / false alarm.
6. **Tune** thresholds with the threshold sweep (and `cctv_ml.review_sweep` on the shadow
   reviews); add hard negatives from false alarms to the training labels (never from the
   test split). Exported with `python -m cctv_cloud.cli export-reviewed`
   (docs/runbooks/shadow-mode.md).
7. **Enable alerts** once the feature passes.

## Install and test

```bash
# from the repository root, inside the virtual environment
pip install -e contracts -e "ml[test]"
cd ml && pytest
```

Only the standard library and `cctv-contracts` (event types) are needed.

## Tools

### Split footage: `python -m cctv_ml.split`

```bash
python -m cctv_ml.split --inventory footage.csv --out manifest.csv [--ratios 70,15,15] [--stratify-site]
```

`footage.csv`: `path, camera_id, started_at, ended_at[, site]` (times ISO-8601 with
offset, e.g. `2026-10-01T08:00:00+03:00`). Extra columns are kept. `manifest.csv` adds
`day` (local calendar day, `--timezone Asia/Kuwait`) and `split`.

- All footage of a camera on one day is one group and goes to a single split, so
  near-identical frames can't sit in both train and test.
- The split comes from a hash of (seed, camera, day). Adding footage never moves an
  existing day. If `manifest.csv` already exists, its assignments are kept (use
  `--fresh` to start over, and only before anything was trained). **Once in test,
  always in test.**
- `--stratify-site` balances hours per site (useful with few days per camera); it is
  stable as long as you keep re-running with the same `--out` file.
- The printed summary shows hours per split per camera and warns about cameras with no
  test footage.

### Event evaluation: `python -m cctv_ml.evaluate`

```bash
python -m cctv_ml.evaluate --predictions events.jsonl --ground-truth gt.csv \
    --coverage coverage.csv --out report.md --json report.json [--tolerance 2] [--check]
```

| File | Format |
|---|---|
| `events.jsonl` | One JSON object per line from the edge replay tool: `event_id, camera_id, feature, event_type, zone_id, track_id, started_at, ended_at, confidence, attributes`. `ended_at` may be null (open event). |
| `gt.csv` | `camera_id, event_type, started_at, ended_at[, note]`; made by `cctv_ml.labelstudio` |
| `coverage.csv` | `camera_id, started_at, ended_at`: periods both labelled **and** replayed. Events starting outside are ignored. |

Event types are the contract values (`cctv_contracts.enums.EventType`) and are grouped
into features with `EVENT_FEATURE`.

**Matching.** Same camera and event type; intervals overlap, allowing `--tolerance`
seconds of gap; pairs taken by largest overlap, each event matched at most once. Extra
predictions overlapping an already matched labelled event are **duplicates** (e.g. a long
event split into 2-minute parts). They are reported separately and are not counted as
false alarms. Many duplicates still mean repeated alerts, so check the cooldown.

**Metrics** per feature and per event type: TP, FP, FN, duplicates, precision
`TP/(TP+FP)`, recall `TP/(TP+FN)`, F1, median start delay (predicted start - labelled
start), and false alarms:

- `FA/day = FP / (hours covered by at least one camera / 24)`. For one branch's cameras
  over the same period, this is the plan's "false alarms per branch per day" (target
  < 5).
- `FA/camera-day = FP / (sum of each camera's covered hours / 24)`.

The report also has the **target check** (precision and recall >= `--target`, default
0.9, per feature), a **confidence threshold sweep** per event type to choose thresholds,
and the list of **false alarms and missed events** with camera and local time for the
confusion review. `--check` exits with 1 when a feature with data misses the target
(for CI or release scripts).

### Label Studio export: `python -m cctv_ml.labelstudio`

```bash
python -m cctv_ml.labelstudio --export export.json --videos videos.csv --out gt.csv
```

Converts the JSON export of an `event_intervals.xml` project to `gt.csv`. Label Studio
only knows frame numbers, so `videos.csv` (`video, camera_id, started_at, fps`) gives
each video's camera and the **real time of its first frame** (from the NVR export or the
footage inventory, never from the picture's overlay). Frames are 1-based and the end
frame is included (`--frame-base`, `--units seconds` for other setups). The latest
annotation that was not skipped is used. Check one hand-placed label after every Label
Studio upgrade.

### Classifier evaluation: `python -m cctv_ml.classify_eval`

```bash
python -m cctv_ml.classify_eval --predictions preds.csv --labels labels.csv \
    --out report.md --json report.json [--min-score 0.6]
```

| File | Format |
|---|---|
| `labels.csv` | `id, attribute, label` e.g. `crop-0012, gloves, not_worn`; labels as in the configs (`worn / not_worn / not_visible`, `open / closed / not_visible`) |
| `preds.csv` | `id, attribute, predicted[, score]`; the model may answer `unknown` |

Per attribute: precision and recall per class on items the annotator could see (an
`unknown` answer counts against recall, not precision), decided accuracy, the share of
`unknown` answers on visible items, answers on items labelled `not_visible` (guesses),
and the confusion matrix. `--min-score` turns low-score answers into `unknown` to test
a stricter threshold.

### Thresholds from shadow-mode reviews: `python -m cctv_ml.review_sweep`

```bash
python -m cctv_ml.review_sweep --manifest manifest.csv --out sweep.md [--json sweep.json] \
    [--target 0.9] [--days 14]
```

`manifest.csv` from `python -m cctv_cloud.cli export-reviewed` (events people marked
valid or false alarm, with the model's confidence). Per event type: every threshold the
data allows, with confirmed events still raised, false alarms left, precision and false
alarms per day, and the lowest threshold reaching `--target`. It only sees events the
system raised, not the ones it missed: confirm a new threshold on the labelled test set
with `cctv_ml.evaluate`.

## Labelling

| Config | Task data | Used for |
|---|---|---|
| [person_detection.xml](labelling/configs/person_detection.xml) | `{"image": ...}` frame | Person / phone detector; ignore polygons; optional staff/customer role |
| [ppe_attributes.xml](labelling/configs/ppe_attributes.xml) | `{"image": ...}` person crop | Mask / gloves / hair cap classifier |
| [cash_drawer.xml](labelling/configs/cash_drawer.xml) | `{"image": ...}` drawer crop | Drawer open / closed classifier |
| [event_intervals.xml](labelling/configs/event_intervals.xml) | `{"video": ...}` | Event-level ground truth for `evaluate` |

Set `frameRate` in `event_intervals.xml` to the footage's real frame rate (25 by
default) and use the same number in `videos.csv`. Guides:
[general and privacy](labelling/guides/README.md),
[person boxes](labelling/guides/person_boxes.md),
[security events](labelling/guides/security_events.md),
[cashier](labelling/guides/cashier.md),
[kitchen PPE](labelling/guides/kitchen_ppe.md).

## Model licences

The product is commercial, so model licences must be decided before training
(plan §1 #12):

- **Ultralytics YOLO (v5/v8/11)** is AGPL-3.0: using it in the product needs an
  Ultralytics enterprise licence.
- **InsightFace** pretrained weights (ArcFace, SCRFD, buffalo packs) are for
  non-commercial research only.
- Licence-safe options to evaluate: **YOLOX**, **RT-DETR**, **D-FINE** (Apache-2.0)
  for person/phone detection; **YuNet** (MIT) for face detection with **SFace**
  (Apache-2.0) for face embeddings; classifiers trained by us on the client's footage.
  Check the licence of the **weights** too, and the datasets they were trained on, not
  just the code.

Record the chosen model, version, licence and its evaluation report together.
