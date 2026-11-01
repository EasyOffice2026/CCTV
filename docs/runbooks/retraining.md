# Retraining a model (plan 11.5)

For the ML engineer. How a model gets better from the system's own reviews, and how a new
version reaches the boxes. No training code is in the repository: which models are used
depends on the licence decision (plan §1 #12; edge/README.md "Models"), and each model's
own training repository (YOLOX, RT-DETR/D-FINE, a small classifier) does the training.
This repository provides the data in, the evaluation, and the packaging out.

## 1. Collect

- **Hard negatives** (false alarms) and confirmed events from the reviews:

  ```bash
  python -m cctv_cloud.cli export-reviewed --tenant <slug> --since <date> \
      --status false_alarm,valid --media both --out <approved storage>/reviews/<date>
  ```

  Footage: only on the project's approved storage, never in git or on a personal laptop;
  every file fetched is in the client's audit log ([shadow-mode.md](shadow-mode.md)).
- **New footage** for situations the model misses (from the pilot log and the reviewers'
  notes): export from the NVR, then `python -m cctv_ml.split` to place each camera-day in
  train, validation or test (**once in test, always in test**).

## 2. Label

Label Studio with the configs in `ml/labelling/configs` and the guides in
`ml/labelling/guides` (privacy rules first). False alarms become negatives (an image
with no target object, or the object with the right class, e.g. "gloves worn"). Event
intervals for the evaluation: `python -m cctv_ml.labelstudio`.

## 3. Train

In the model's own repository, on train + validation only. Write down the base weights,
the data version (manifest file names) and the training settings.

## 4. Evaluate on the test set

1. Export the model to ONNX.
2. Replay the test footage through the edge pipeline with the new model (edge/README.md
   "Offline evaluation") and run `python -m cctv_ml.evaluate` against the labelled test
   events; for classifiers, `python -m cctv_ml.classify_eval` on the test crops.
3. Compare with the current model's report. Ship only if precision and recall per feature
   are at least as good and meet the target (90%, or the agreed one), and false alarms per
   day don't go up.
4. Pick thresholds with the evaluation's threshold sweep (and `cctv_ml.review_sweep` on
   recent reviews, as a cross-check).

## 5. Package

A folder per model with the weights and `model.json` (edge/README.md "Models"):
`name`, a new `version`, `task`, `file`, the **`sha256`** of the weights (the box refuses
anything else), the **`licence`** (required), the source, input and output settings,
classes and thresholds. Store it with the evaluation report next to it; never in git.

## 6. Deploy

1. One branch first: copy the folder to the box (`/opt/cctv-edge/models/<name>/`, over
   the tailnet) or reprovision with `MODELS_SOURCE`, then `sudo systemctl restart cctv-edge`.
   The log shows the model loaded and its sha256 check.
2. Watch that branch for a few days in the Daily review and on the Pilot page (precision,
   false alarms per day). Note the change in the pilot log.
3. Then the other branches. Keep the previous model folder to roll back by swapping it in.
