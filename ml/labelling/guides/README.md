# Labelling guides

Read this page first, then the guide for your task.

| Task | Label Studio config | Guide |
|---|---|---|
| Boxes for the person/phone detector | `configs/person_detection.xml` | [person_boxes.md](person_boxes.md) |
| Event intervals on video (security, cashier, kitchen) | `configs/event_intervals.xml` | [security_events.md](security_events.md), [cashier.md](cashier.md), [kitchen_ppe.md](kitchen_ppe.md) |
| Mask / gloves / hair cap on person crops | `configs/ppe_attributes.xml` | [kitchen_ppe.md](kitchen_ppe.md) |
| Cash drawer open / closed | `configs/cash_drawer.xml` | [cashier.md](cashier.md) |

There is no labelling for face recognition. Staff are enrolled with consent in the
dashboard, and attendance is checked against the attendance records, not against
labelled faces.

## Privacy rules (non-negotiable)

The footage shows real staff and customers of the client. It is personal data.

1. **Work only on approved machines** and only in the project's Label Studio. Never copy
   footage, frames or crops to a personal laptop, phone, USB stick, chat app, email or a
   public cloud drive.
2. **No screenshots or screen recordings** for any purpose except a bug report to the
   project lead, and delete them afterwards.
3. **Never identify customers.** Don't write names, descriptions ("the man in the red
   shirt is a regular") or anything else about a customer in notes or comments.
4. **Staff:** label what they do, not who they are. Don't put staff names in notes. The
   only exception is the attendance check, which uses the system's own records.
5. Footage of every branch stays inside the project folders; nothing leaves for
   "examples" or "demos" without the client's written approval.
6. If you see something sensitive (an accident, a crime, a private moment), stop, don't
   label it, and tell the project lead. They decide whether the clip is removed.
7. Footage and labels are deleted at the end of the project or when the client asks,
   whichever comes first.

## General rules

- **Label what you see, not what you guess.** If you can't tell, use `not_visible` /
  `unsure`, or leave it out and add a note. A wrong label hurts the model more than a
  missing one.
- **Clocks:** never read time from the overlay burned into the picture; the NVR clocks
  are wrong at several sites. Times come from the inventory and the export tools.
- **Fisheye / ceiling cameras:** people near the edge of the picture are stretched and
  seen from above. Label them all the same; if a box would cover mostly distortion,
  label it and pick `frame_quality = blurred`.
- **Night / infrared:** grey pictures, glowing eyes and reflective strips are normal.
  Label as usual and set the quality choice.
- **Reflections, TVs, posters, glass doors:** people *in* them are not real; cover the
  area with an `ignore` polygon (boxes) or don't mark an event (video).
- **Consistency checks:** every week the lead picks 5% of tasks and has a second person
  label them blind. Agreement below 90% on a task type means the guide is unclear; we fix
  the guide, not just the labels. Write questions into the task comments instead of
  guessing; answers are added to the guides.
- **Test footage:** some videos are marked `test` in the task data. Label them exactly the
  same way. They are used only to measure the models, never to train them, so don't look
  at model predictions while labelling them.
