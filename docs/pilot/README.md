# Running the official pilot (plan Phase 10)

For our team and the client's owner, from the end of shadow mode
([runbooks/shadow-mode.md](../runbooks/shadow-mode.md)) to the sign-off
([sign-off.md](sign-off.md)). Everything is tracked on the dashboard's **Pilot** page
(Monitoring › Pilot, per branch) and in its **pilot log**.

## Day 1: wave-1 alerts on (10.1)

- [ ] Shadow mode ended for the branch (Administration › Branches › Shadow mode › End now).
- [ ] **Pilot day 1** set on the Pilot page (super-admins only). The 30 days and all the
      pilot figures count from it.
- [ ] Wave-1 features on for the cameras that need them (Settings › Features): attendance,
      kitchen compliance, cashier, after-hours security, with the thresholds found in
      shadow mode. Features that didn't meet their target in shadow mode stay off for
      alerts (or in shadow for longer); note it in the pilot log as a decision.
- [ ] Every owner and manager has alert rules (Settings › Alerts) and has received a test
      message on each channel they chose.
- [ ] Pilot log entry: "Pilot started", with the features on and their thresholds.

## Week 2: wave 2 (10.2)

Turn wave 2 on only when wave 1 is stable (false alarms under target for a few days and no
open issues). Each is a feature switch per camera plus zones drawn on that camera.

**Customer count** (feature `customer_count`):

- Draw an **entrance line** across the door, a little inside so people standing outside
  aren't counted. The direction matters: crossing from the right of the line (as drawn,
  first point to second) to its left counts as "in"; swap the points if ins and outs come
  out reversed.
- Draw **dining** zones over the seating: the most people seated at once is reported, and
  `occupancy_high` fires (info, no alert by default) when a dining zone holds at least
  `min_count` people (30) for 2 minutes. Set `min_count` to about 90% of the seats.
- **Staff are left out**: anyone the camera sees in a **staff area**, **kitchen** or
  **counter** zone is staff from then on and isn't counted at the entrance line or in
  dining and queue zones. On the entrance camera, draw a staff-area zone over the staff
  door or the area behind the counter if it is in view. Staff who never step into such a
  zone in that camera's view are still counted; the report shows how many crossings were
  left out, so the share can be checked against a manual count (below).

**Queue** (feature `queue`):

- Draw a **queue** zone where people wait at the counter, not where they stand to order.
- `queue_long` fires when the zone holds at least `min_count` people (5) for 60 seconds;
  one event per 10 minutes. Set it per camera, e.g. `{"queue_long": {"min_count": 6,
  "min_duration_s": 90}}`, and add the event type to the managers' alert rules if they want
  to be told.
- Wait times: each person's time in the zone (from tracking), reported as the average and
  longest wait. Someone lost by the tracker and found again counts as two shorter waits,
  so waits are on the low side.

**Check both by hand once** before trusting them: count people in and out at the door for
an hour at a busy time, and time five people in the queue with a stopwatch. Note both
counts in the pilot log as a decision ("customer count within 10% of a manual count").

## Every day (10.3): the Pilot page

About 15 minutes, by our team:

1. Daily review first (Monitoring › Daily review): every event of the day before marked.
   The Pilot page's precision only counts reviewed events.
2. **Pilot** page, branch chosen: the checks at the top (accuracy, false alarms per day,
   alert time, camera uptime, pilot length) and the day table. Anything red:
   - false alarms over 5 a day: Daily review › "Where the false alarms come from", then tune;
   - alert time over 10 s: check the box's internet, the WhatsApp delivery reports
     (event › who was told), and the worker;
   - camera uptime under 99%: Edge devices › setup test from that day; the box or a camera.
3. Anything changed or broken goes in the pilot log the same day (kind **Change** or **Issue**).

## Every week (10.4): check-in with the owner

30 minutes, same day each week. Agenda:

1. The Pilot page together: the week's figures per feature.
2. Three events from the week worth looking at (a good catch, a false alarm, a missed one
   if the owner saw one).
3. What the owner and managers found useful or annoying (too many alerts, wrong times,
   missing information).
4. Changes we propose, and what the owner agrees to.

Write it up as a **Weekly check-in** entry in the pilot log, and the owner's own words as
**Feedback**.

## End of the pilot (10.5)

After at least 30 days (the Pilot page's "Pilot length" check), fill in
[sign-off.md](sign-off.md) with the owner:

- the figures from the Pilot page (the whole period), per feature;
- recall per feature from the offline evaluation on the labelled test set
  (`cctv_ml.evaluate`, ml/README.md): reviews can't show missed events;
- the **search test**: the owner (or a manager), on their own computer, finds three events
  we name (e.g. "the drawer opened at about 21:40 last Thursday at the counter camera") using
  Search; time each from opening the page to the clip playing. Target under 30 s each;
- the owner's confirmation and comments.

A feature that missed its target gets more time on its own (plan 10.5): note the decision
and the new date in the pilot log, keep its pilot running, and sign off the others.
