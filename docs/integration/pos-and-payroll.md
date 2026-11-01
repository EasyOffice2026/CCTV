# Connecting the till (POS) and the payroll system (HRM)

For the developers of the restaurant's till (point of sale) and payroll / HR systems. Both
connections are plain HTTPS calls with an API key; nothing has to be installed on our side.
They can be used separately. In the examples, `https://cctv.example.com` stands for the
dashboard address we give you.

| Connection | Direction | What it does |
|---|---|---|
| [1. Till sales](#1-till-sales-pos) | Your till → us, every few minutes | Each cash drawer opening the cameras see is checked against your sales. An opening with no sale near it is flagged *No sale on the till*, alerted to the managers who asked for cashier warnings, and counted in the reports |
| [2. Attendance for payroll](#2-attendance-for-payroll-hrm) | Your payroll system ← us, when it runs payroll | Each staff member's days: on time, late, absent, check-in and check-out, minutes worked |

## API keys

The client's owner creates the keys under **Administration › Integrations › API keys** and
gives each to the system that needs it, through a secure channel:

| System | Scope | Can |
|---|---|---|
| Till | `pos:write` | Send sales. Nothing else: it can't read events, video or people |
| Payroll | `attendance:read` | Read attendance (names, employee codes, times). Nothing else |

Send the key in the `X-API-Key` header. A key can be revoked on the same page at any time;
every call is over HTTPS, and every attendance export is written to the audit log.

## 1. Till sales (POS)

```
POST https://cctv.example.com/v1/pos/sales
X-API-Key: cctv_...
Content-Type: application/json

{
  "branch_id": "6f1c...",                        // given to you per branch
  "synced_until": "2026-11-02T13:05:00+03:00",
  "sales": [
    {"receipt_id": "R-104233", "sold_at": "2026-11-02T13:01:12+03:00",
     "total": "4.750", "payment": "cash", "till": "1"},
    {"receipt_id": "R-104234", "sold_at": "2026-11-02T13:03:40+03:00",
     "total": "12.000", "payment": "card"}
  ]
}
```

Answer: `{"accepted": 2, "synced_until": "2026-11-02T10:05:00Z"}`.

| Field | |
|---|---|
| `receipt_id` | Unique within the branch. Sending the same receipt again replaces it, so retries are safe. Refunds: their own receipt, negative `total` |
| `sold_at` | When the sale was completed, **with its UTC offset** (times without one are refused). The till's clock must be right (NTP): matching is by time |
| `total` | Up to 3 decimals (KWD fils) |
| `payment` | `cash`, `card` or `other`. Cash sales are preferred when matching drawer openings |
| `till` | Optional: which till, if the branch has several |
| `synced_until` | **Every sale up to this time has been sent**, in this call or before. Send it every 2–5 minutes, also when there were no sales (`"sales": []`) |

How the check works: a drawer opening is explained by a sale from 1 minute before it to 2
minutes after it (cash first, then the nearest), and each sale explains one opening.
Openings are only checked once `synced_until` is 2 minutes past them, so if your till is
offline or behind, nothing is flagged; the openings are checked when it catches up (for up to
7 days). At most 1,000 sales per call; older backlogs in several calls, oldest first.

Errors: `401` bad or revoked key, `403` the key lacks `pos:write`, `404` unknown branch,
`422` invalid data (the body says which field), or `synced_until` more than 5 minutes ahead of
our clock.

What we keep: receipt id, time, total, payment type and till, no items and no customer data,
for as long as event records (1 year by default).

## 2. Attendance for payroll (HRM)

```
GET https://cctv.example.com/v1/payroll/attendance?from=2026-10-01&to=2026-10-31&format=json
X-API-Key: cctv_...
```

| Parameter | |
|---|---|
| `from`, `to` | Work days, both included, at most 62 days |
| `branch_id` | Optional: one branch |
| `format` | `json` (default) or `csv` (UTF-8 with a BOM, opens in Excel with Arabic names) |

JSON answer:

```json
{
  "start": "2026-10-01",
  "end": "2026-10-31",
  "days": [
    {"staff_id": "…", "employee_code": "E1", "name": "Sara Ahmed", "branch": "Ardiya",
     "date": "2026-10-02", "status": "late", "check_in": "09:12", "check_out": "17:05",
     "worked_minutes": 473, "corrected": false, "note": null}
  ],
  "staff": [
    {"staff_id": "…", "employee_code": "E1", "name": "Sara Ahmed", "days_on_time": 21,
     "days_late": 3, "days_absent": 1, "days_off": 5, "worked_minutes": 11520}
  ]
}
```

- Match people by **`employee_code`**: it is the code entered for each staff member in the
  dashboard (Staff), so use the same codes as in your payroll system.
- `status`: `on_time`, `late`, `absent` or `day_off`. Times are local to the branch (HH:MM).
  `worked_minutes` is check-out minus check-in, 0 when either is missing.
- `corrected: true` means a manager set or changed the day by hand (with `note`).
- A day is final once the branch's manager has reviewed it; run payroll a few days after the
  month ends, or fetch again before paying.

The owner can download the same CSV for one month on the **Attendance** page.
