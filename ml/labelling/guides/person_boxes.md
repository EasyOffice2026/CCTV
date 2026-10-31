# Person and phone boxes

Config: `configs/person_detection.xml`. One frame per task.

## person

- Box **every** real person, staff or customer, including people who are partly hidden
  by a counter, a door or another person.
- The box covers the **visible** body tightly, from the top of the head (or cap) to the
  lowest visible point. Don't guess hidden legs behind a counter: stop the box at the
  counter.
- **Feet matter.** The system decides which zone someone is in from the bottom-centre of
  the box. If the feet are visible, the box must reach them.
- A person cut by the picture edge: box the visible part.
- Very small people (less than about 15 px tall, e.g. on the street far away): skip them,
  and if the area is always like that, cover it with an `ignore` polygon.
- People in a crowd: one box per person, even when the boxes overlap a lot.
- People on TVs, posters, in mirrors or reflected in glass: **no box**; cover the area
  with `ignore`.
- Mannequins, coats on hooks, dark shapes you aren't sure about: no box. If it keeps
  confusing you, add a comment so the lead can check.

## phone

- Box a phone only when it is **in a hand and being used** (looked at, held to the ear,
  typed on). A phone lying on a table is not boxed.
- The box covers the phone only, not the hand.

## ignore polygons

Draw once per frame around areas where people should never be detected: TV screens,
mirrors, glass doors showing the street, posters with people. The same area is set as an
`ignore` zone on the camera in the dashboard.

## role (optional)

When a person box is selected, choose `staff` (uniform, behind the counter, in the
kitchen), `customer`, or `unsure`. Only fill this in if the lead asked for it on the
project; it is used later for customer counting.

## frame_quality (required)

`ok`, `blurred` (motion blur, out of focus, heavy fisheye stretch), `night_ir` (grey
infrared picture) or `glare` (sunlight or lights washing out part of the picture).
