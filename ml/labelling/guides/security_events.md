# Security events (after-hours, restricted areas)

Config: `configs/event_intervals.xml`. You mark **when** something happens on a video;
the evaluation compares these ranges with the events the system produces.

For each video, the lead tells you the branch's working hours and which zones are
restricted (store room, cash office...) and gives you a still with the zones drawn.

## after_hours_person

Someone is visible in the area the camera covers **outside working hours**.

- Start: the first frame a real person is visible. End: the last frame they are visible.
- If the same person leaves the picture and comes back within **3 seconds**, keep one
  range. A longer gap starts a new range.
- Several people at the same time on the same camera: **one** range covering all of
  them (the system raises one alert per camera area, not per person).
- Cleaners or security staff working at night are still marked; the client decides
  later which of them should not raise alerts.
- Don't mark: people only on the street seen through the window or glass door, people
  on TVs or in reflections, shadows, headlights moving across the room.
- Don't mark during working hours, even if the shop looks closed.

## after_hours_motion

Something moves in the picture outside working hours **and no person is visible**:
a door, an animal, a fallen object, a light switched on. Mark from the start to the end of
the movement. Don't mark constant flicker (a TV left on, a ceiling fan, rain on the
window, the camera's infrared switching on); mention it in a comment so the area can get
an ignore zone.

## restricted_area_entry

Someone's **feet** enter a restricted zone, at any time of day.

- Start: the frame the feet are inside the zone outline. End: the frame they leave.
- Reaching into the zone without stepping in is **not** an entry.
- Two people entering together: one range per person (they may overlap).
- Staff who are allowed in are still marked; permissions are handled by the client's
  settings, not by the labels.

## Checks before you submit

- Ranges start and end on the person, not on when you noticed them.
- No range of only a few frames unless the person really is visible that briefly.
- Every range has exactly one event type.
