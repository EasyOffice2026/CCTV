# Cashier area

Two kinds of tasks: event intervals on video (`configs/event_intervals.xml`) and
drawer crops (`configs/cash_drawer.xml`). The lead gives you a still of the camera with
the `counter` and `cash_drawer` zones drawn.

## counter_unattended (video)

**No staff member** is in the counter zone during working hours.

- Start: the frame the last staff member's feet leave the counter zone. End: the frame
  a staff member's feet are back inside.
- Customers standing at the counter don't count as attending it.
- Mark every gap, however short; the system's own minimum time (for example 60 s) is a
  setting, and the evaluation must see the short gaps too. Add `short` in the comment for
  gaps under 10 s.
- A staff member bending down behind the counter (hidden) is still attending it if you
  saw them go down and come up. If you can't tell, mark the range and comment
  `hidden?`.

## drawer_open (video)

Mark from the frame the drawer starts opening to the frame it is fully closed. Every
opening is one range, even if they follow each other quickly.

## Drawer crops (open / closed / not_visible)

- `open`: the drawer is pulled out, even partly, so the tray or cash is visible.
- `closed`: the drawer front is flush with the till.
- `not_visible`: a hand, a body, a bag or glare covers the drawer so you can't tell.
  Use it whenever you hesitate; the model is allowed to answer "unknown" too.
- Don't use the money, a receipt or what the cashier is doing as a clue. Judge the
  drawer only.
