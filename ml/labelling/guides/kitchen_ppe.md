# Kitchen compliance: mask, gloves, hair cap

Two kinds of tasks: person crops (`configs/ppe_attributes.xml`) and event intervals on
kitchen video (`configs/event_intervals.xml`). Which items are required in which kitchen
zone is agreed with the client; the lead tells you. Label every item on every crop anyway.

## Crops: worn / not_worn / not_visible

Judge **only what this crop shows**. Most kitchen cameras look down from the ceiling with
wide lenses, so many items are simply not visible: that is expected and fine.

### mask

- `worn`: covers mouth **and** nose.
- `not_worn`: face visible without a mask, **or** the mask is pulled down under the nose
  or chin.
- `not_visible`: face turned away, seen from straight above, covered by a cap brim, or
  too small to tell.

### gloves

- `worn`: every hand you can see has a glove (blue, black, white or clear plastic; look
  for the cuff line and the sheen).
- `not_worn`: at least one clearly visible bare hand while handling food or utensils.
- `not_visible`: hands are hidden, out of the crop, blurred by movement, or too small to
  see. **A partly visible hand is `not_visible`, not `not_worn`.**
- Clear gloves are hard to see. If you can't find the cuff or the shine, choose
  `not_visible`.

### hair_cap

- `worn`: a hairnet, kitchen cap or hat covers the hair (a little hair at the sides or
  the neck is fine).
- `not_worn`: the top of the head shows uncovered hair.
- `not_visible`: the head is cut off or hidden.
- Bald or shaved heads without a cap: `not_worn` (the rule is about wearing a cap); add a
  comment so the client can decide.

### crop_quality

`ok`, `too_small` (person less than about 100 px tall), `blurred`, `cut_off` (person
not completely in the crop). It helps us set the system's own "too small to judge"
limit.

## Video: missing_mask / missing_gloves / missing_hair_cap

Mark when a person **inside the kitchen zone** is clearly without the item.

- Only mark while you can actually see the item missing, then mark the whole time it is
  visible. Don't extend a range over frames where the item can't be seen.
- One range per person per period. If the same person stays without gloves for a long
  time, keep one range while they are continuously visible.
- Two people without masks at the same time: two overlapping ranges.
- People outside the kitchen zone (at the pass, in the corridor) are not marked.
- Taking gloves off to wash hands or change gloves at the sink is **not** a violation;
  don't mark it.
