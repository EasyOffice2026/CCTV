# Shadow mode and tuning (plan 9.4, 9.5)

For our team, for the one or two weeks between the install ([install.md](install.md)) and
the official pilot. The system runs as it will in the pilot, but **alerts go only to us**
while we check every event and tune.

## What shadow mode does

While a branch is in shadow mode (Administration › Branches › Shadow mode, super-admins
only; it ends by itself at the end of the chosen day):

- Events are recorded, shown and searchable as usual; the client can see them.
- Alerts go **only** to accounts marked **Support team** (Administration › Users). The
  client's own alert rules send nothing.
- No webhooks go to the client's operational system, and report links go only to the
  support team. Reports are still made.
- Every page shows a banner saying so.

## Every day (about 30 minutes for a branch)

1. Open **Monitoring › Daily review**, choose the branch, and go through yesterday's and
   today's events, oldest first. Each event shows its clip and snapshot.
   - `V` valid · `F` false alarm · `R` reviewed (real, but not a problem, e.g. a manager
     opening the drawer) · `S` skip · `B` back. Typing a note doesn't trigger the keys.
   - Write a note on false alarms saying why ("blue gloves", "reflection in the window",
     "cleaner after hours"): it is the input for tuning and for the labellers.
2. Target: **nothing left unreviewed** from the day before. The day boxes at the bottom
   show reviewed/total per day.
3. Note anything that looks missed (something we saw in a clip or on live view that
   raised no event) in the pilot log with camera and time: the reviews only show what the
   system raised, never what it missed.

## Every few days: tuning

The table **Where the false alarms come from** (Daily review page, last 7 days) groups the
reviews by event type, camera and zone, most false alarms first, with precision
(valid ÷ (valid + false alarm)), false alarms per day and the average confidence of valid
events and of false alarms.

Fix the top rows first, one change at a time, and write each change in the pilot log:

| What the reviews show | What to change |
|---|---|
| False alarms with lower confidence than the valid events | Raise the confidence threshold for that event type (Settings › Features, the camera or branch) |
| Short events that aren't real (a hand passing, a person walking through) | Raise the minimum duration / dwell time |
| Always the same spot (TV, mirror, window, street, a poster) | Draw an **ignore** zone there (Settings › Zones) |
| Wrong zone (counter zone catching the queue) | Redraw the zone |
| One camera far worse than the others | Check its view, focus, lighting and resolution; move the camera if needed (plan §9) |

To choose thresholds from the data:

```bash
python -m cctv_cloud.cli export-reviewed --tenant <slug> --since 2026-11-15 \
    --media none --out D:/cctv-data/reviews/2026-11-29
python -m cctv_ml.review_sweep --manifest D:/cctv-data/reviews/2026-11-29/manifest.csv \
    --out D:/cctv-data/reviews/2026-11-29/sweep.md
```

The sweep shows, per event type, the lowest threshold that reaches 90% precision on the
reviewed events and how many confirmed events it would still raise. It can't see missed
events, so a threshold that looks free can still lose real ones: check it on the labelled
test set (`cctv_ml.evaluate`) before using it.

## Every week or two: retraining on false alarms (hard negatives)

```bash
python -m cctv_cloud.cli export-reviewed --tenant <slug> --since 2026-11-15 \
    --status false_alarm --media both --out D:/cctv-data/hard-negatives/2026-11-29
```

This copies the snapshots and clips of the false alarms, with `manifest.csv` (event,
camera, zone, time, confidence, the reviewer's note). The files are **footage**: they go
only to the project's approved storage (never git, never a personal laptop), follow the
labelling privacy rules ([ml/labelling/guides](../../ml/labelling/guides/README.md)), and
each file fetched is recorded in the client's audit log as a download "for training".
Delete them once labelled.

Then label them in Label Studio (they become negatives or corrected labels for the PPE,
drawer or person models), add them to the **training** split only (never to test), retrain,
run the offline evaluation (`ml/README.md`) and deploy the new model to the box only if it
is better on the test set.

## Leaving shadow mode

A feature is ready for the client's alerts when, over at least the last 7 days of shadow
mode:

- [ ] every event was reviewed;
- [ ] precision on the reviewed events is **at least 90%** (or the target agreed for that
      feature, e.g. gloves; plan §9);
- [ ] false alarms are **under 5 per branch per day** for all features together;
- [ ] the offline evaluation report on the test set still meets the target.

Then, with the owner:

- [ ] Train the owner and managers ([../training/owner-and-managers.md](../training/owner-and-managers.md)) (9.6).
- [ ] Help each of them add their alert rules (Alerts page) and check a test message
      arrives.
- [ ] End shadow mode (Administration › Branches › Shadow mode › End now), or let it run
      out. Features that aren't ready stay switched off for alerts; they don't hold up
      the others (plan 10.5).
- [ ] Record the date, the precision per feature and the false alarms per day in the
      pilot log: it is the start of the official 30-day pilot (Phase 10).
