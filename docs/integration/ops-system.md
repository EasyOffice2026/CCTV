# Connecting the operational system (plan Phase 8)

For the developers of Mudawara's operational system (and any other client system). It
covers four things, which can be used separately:

1. **Sign-in**: users of your system open the CCTV dashboard without a second password.
2. **Embedding**: the CCTV *owner overview* shown inside your own pages.
3. **KPIs by API**: branch figures pulled into your own screens.
4. **Webhooks**: new events and reports pushed to your server.

Everything below works with any server language; examples are in Node.js, Python and PHP.
In the examples, `https://cctv.example.com` stands for the dashboard address we give you.

## What we need from you, and what you get

| From you | Used for |
|---|---|
| Your system's **issuer**: a fixed string, usually your base URL, e.g. `https://ops.mudawara.example` | Sign-in tokens (`iss`) |
| How you will sign: a **shared secret** (HS256, we generate it) or your **public key** (RS256 with RSA ≥ 2048 bits, or ES256 with P-256; you keep the private key) | Sign-in tokens |
| Whether your system asks owners for a **second factor** (code, authenticator app, SMS, security key) | Owners must use 2FA on the dashboard; see [§1](#1-sign-in) |
| The **origin(s)** of the pages that will embed the overview, e.g. `https://ops.mudawara.example` | Allowed to frame the page ([§2](#2-embedding-the-owner-overview)) |
| The **https URL** that should receive webhooks | [§4](#4-webhooks) |

| You get | |
|---|---|
| The audience (`aud`) to put in tokens: the dashboard address, e.g. `https://cctv.example.com` | |
| The shared secret, if you chose HS256 (once, through a secure channel) | |
| An API key with the `kpis:read` scope (the client's owner creates it under **Administration › Integrations**) | |
| A webhook secret (shown once when the webhook is created on the same page) | |

Users still exist on the dashboard: the owner invites them under **Administration ›
Users** with their **email address as in your system**, a role and their branches. Your
system decides *who the user is*; the dashboard decides *what they may see*. Nobody gets
in who hasn't been invited.

## 1. Sign-in

Your server makes a short-lived token (a JWT) for the user who is signed in to your system,
and passes it to the browser, which hands it to the dashboard. The dashboard checks it and
starts a normal session for the user with that email.

### The token

| Claim | Value |
|---|---|
| `iss` | Your issuer (as agreed) |
| `aud` | The dashboard address we give you, e.g. `https://cctv.example.com` |
| `email` | The user's email (matched without regard to upper/lower case) |
| `sub` | The user's id in your system (recorded in the dashboard's audit log) |
| `iat`, `exp` | Issued now; expires at most **5 minutes** later. Two minutes is plenty |
| `jti` | A random unique id (UUID). Each token can be used **once** |
| `amr` | The sign-in methods used, as in RFC 8176, e.g. `["pwd", "otp"]`. Required for **owners**: it must include a second factor (`otp`, `mfa`, `sms`, `hwk`, `swk`, `fpt`, `face`, …). Optional for other roles |

Header: `{"alg": "HS256" | "RS256" | "ES256", "typ": "JWT"}`. Only the algorithm agreed for
your issuer is accepted.

If your system has no second factor for owners, tell us: we can switch the requirement
off for your issuer, which the client's owner then sees on the Integrations page. We
recommend adding 2FA instead, because the dashboard shows camera footage.

**Node.js** (`npm i jsonwebtoken`):

```js
const jwt = require("jsonwebtoken");
const { randomUUID } = require("crypto");

function cctvToken(user) {
  return jwt.sign(
    { email: user.email, sub: String(user.id), amr: user.usedTwoFactor ? ["pwd", "otp"] : ["pwd"], jti: randomUUID() },
    process.env.CCTV_SSO_SECRET, // HS256; for RS256 pass your private key and { algorithm: "RS256" }
    { algorithm: "HS256", issuer: "https://ops.mudawara.example", audience: "https://cctv.example.com", expiresIn: 120 },
  );
}
```

**Python** (`pip install pyjwt`):

```python
import time, uuid, jwt


def cctv_token(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "https://ops.mudawara.example",
            "aud": "https://cctv.example.com",
            "email": user.email,
            "sub": str(user.id),
            "amr": ["pwd", "otp"],
            "iat": now,
            "exp": now + 120,
            "jti": str(uuid.uuid4()),
        },
        CCTV_SSO_SECRET,
        algorithm="HS256",
    )
```

**PHP** (`composer require firebase/php-jwt`):

```php
use Firebase\JWT\JWT;

$token = JWT::encode([
    'iss' => 'https://ops.mudawara.example', 'aud' => 'https://cctv.example.com',
    'email' => $user->email, 'sub' => (string) $user->id, 'amr' => ['pwd', 'otp'],
    'iat' => time(), 'exp' => time() + 120, 'jti' => bin2hex(random_bytes(16)),
], getenv('CCTV_SSO_SECRET'), 'HS256');
```

Make the token **on your server**, only for the user signed in there, when the page or
link is rendered. Never put the secret or private key in browser code.

### A link that opens the dashboard

```
https://cctv.example.com/sso/#token=<token>&next=/events/&lang=ar
```

The token goes after `#` (the URL *fragment*): browsers never send the fragment to any
server, so it stays out of logs and `Referer` headers. The dashboard removes it from the
address bar at once. `next` (optional) is the dashboard page to open, e.g. `/overview/`,
`/events/`, `/reports/`; `lang` (optional) is `en` or `ar`. Make a fresh token each time
the link is shown (or make the link point to your server, which redirects with a fresh
token).

If the token is refused (expired, used, unknown email, owner without 2FA), the page says
so and offers the normal password sign-in. The reason is in the dashboard's audit log
(**Failed sign-in**, with the reason).

## 2. Embedding the owner overview

The overview is one page per client: for each branch, today so far: events, issues
(after-hours security, kitchen compliance, cashier), staff attendance, customers and the
busiest hour, the state of cameras and edge boxes, alert times, the latest events (with
snapshot and clip) and the latest reports. Sections only appear for features switched on
for that branch. It refreshes every minute and follows the user's role and branches.

```html
<iframe
  id="cctv"
  src="https://cctv.example.com/embed/overview/#token=<token>&lang=ar"
  title="CCTV"
  style="width: 100%; border: 0; height: 900px"
></iframe>
```

Fragment parameters: `token` (as in §1, required unless you send it later), `lang` (`en`,
`ar`), `branch` (a branch id, to show only that branch; the user can switch between their
branches inside the page).

The page talks to yours with `postMessage` (nothing secret is sent). Listen for it and
check the origin:

```js
const CCTV = "https://cctv.example.com";
const frame = document.getElementById("cctv");
window.addEventListener("message", async (event) => {
  if (event.origin !== CCTV) return;
  switch (event.data.type) {
    case "cctv:height": // fit the frame to the page
      frame.style.height = `${event.data.height + 20}px`;
      break;
    case "cctv:need-token": // opened without a token
    case "cctv:session-ended": // idle for 8 hours, signed out, or the user was deactivated
      const token = await fetch("/cctv-token", { credentials: "include" }).then((r) => r.text()); // your endpoint
      frame.contentWindow.postMessage({ type: "cctv:token", token }, CCTV);
      break;
    // also: "cctv:signed-in", "cctv:sign-in-failed"
  }
});
```

Inside the frame the session works without cookies (browsers block them in frames of
other sites), so reloading the frame needs a new token: render the iframe with a fresh
token on every page load, or answer `cctv:need-token` as above.

**Only the `/embed/` pages can be framed, and only by the origins we allow** (the
`EMBED_ANCESTORS` setting of the dashboard's web server, sent as the
`Content-Security-Policy: frame-ancestors` header). Send us every origin that will embed
it, including test systems. Every other dashboard page refuses to be framed.

## 3. KPIs by API

```
GET https://cctv.example.com/v1/overview?branch_id=<optional>&day=<YYYY-MM-DD, optional>
X-API-Key: cctv_xxxxxxxx_...
```

With an API key that has only the `kpis:read` scope ("Branch figures"), the answer has
branch totals only: no names, no event list, no reports, no footage. That key opens no
other endpoint. `day` defaults to today in the branch's time zone (Asia/Kuwait); past
days work for up to two years. Figures for today are "so far" (`partial: true`) and are
worked out on every call, so **call at most once a minute**.

```json
[
  {
    "branch_id": "4f6c…",
    "branch_name": "العارضية",
    "timezone": "Asia/Kuwait",
    "day": "2026-11-01",
    "generated_at": "2026-11-01T06:21:11Z",
    "partial": true,
    "features": { "attendance": true, "cashier": false, "kitchen_compliance": true, "security": true, "customer_count": false, "…": false },
    "events": { "total": 2, "critical": 1, "false_alarms": 0, "unreviewed": 2 },
    "attendance": { "on_time": 7, "late": 1, "absent": 0, "day_off": 2, "attendance_pct": 100.0, "punctuality_pct": 87.5 },
    "kitchen_compliance": 1,
    "cashier": null,
    "security": 1,
    "customers": null,
    "alerts": { "sent": 3, "failed": 0, "p50_ms": 2100, "p95_ms": 4800, "within_10s_pct": 100.0, "delivered_p95_ms": null },
    "system": { "devices_online": 1, "devices_offline": 0, "cameras": 8, "cameras_online": 8 },
    "recent_events": null,
    "reports": null
  }
]
```

A section is `null` when its feature is off for the branch (`cashier` and `customers`
above). `customers`, when on, is `{customers_in, peak_hour, peak_occupancy, hours[24]}`;
`cashier` is `{issues, counter_staffed_pct}`. Issues don't count events the managers
marked as false alarms. The full schema is in the API description
we send you (`openapi.json`, schema `BranchOverview`).

## 4. Webhooks

The owner adds your URL under **Administration › Integrations › Webhooks**, chooses what
to send (new events, finished events, new reports), optionally one branch and a minimum
severity, and copies the secret shown once. We POST JSON to your URL within seconds:

```http
POST /hooks/cctv HTTP/1.1
Content-Type: application/json
X-CCTV-Webhook-Id: event.new:9b1e…
X-CCTV-Topic: event.new
X-CCTV-Timestamp: 1793513682
X-CCTV-Signature: v1=5c0f…

{"id":"event.new:9b1e…","topic":"event.new","sent_at":"2026-11-01T06:21:16Z","data":{
  "event_id":"9b1e…","branch_id":"4f6c…","branch":"العارضية","camera_id":"…","camera":"Kitchen",
  "zone":"Prep line","feature":"kitchen_compliance","event_type":"missing_gloves","severity":"warning",
  "status":"open","started_at":"2026-11-01T06:21:12Z","ended_at":null,"confidence":0.87,
  "url":"https://cctv.example.com/events/?event=9b1e…"}}
```

| Topic | When | `data` |
|---|---|---|
| `event.new` | An event starts | As above |
| `event.closed` | It ends (`status: "closed"`, `ended_at` set) | As above |
| `report.ready` | A daily, weekly or monthly report is made (each morning after 02:00) | `report_id`, `branch_id`, `branch`, `period` (`day`/`week`/`month`), `period_start`, `period_end`, `totals` (branch totals as in the report), `url` |
| `ping` | The owner pressed **Send test** | `webhook_id`, `name` |

Messages never contain snapshots, clips or staff names: `url` opens the event or report on
the dashboard, after sign-in (use the sign-in link of §1 with `next=/events/?event=…`).

**Check every request** before trusting it: compute HMAC-SHA256 over
`<X-CCTV-Timestamp>.<raw body>` with the webhook secret, compare with the hex after `v1=`
in constant time, and refuse timestamps more than 5 minutes old.

```js
const crypto = require("crypto");
function verified(req, rawBody, secret) {
  const ts = req.headers["x-cctv-timestamp"];
  const mac = crypto.createHmac("sha256", secret).update(`${ts}.${rawBody}`).digest("hex");
  const given = Buffer.from(String(req.headers["x-cctv-signature"] || "").replace(/^v1=/, ""));
  return Math.abs(Date.now() / 1000 - Number(ts)) < 300 && given.length === mac.length && crypto.timingSafeEqual(given, Buffer.from(mac));
}
```

```python
import hashlib, hmac, time


def verified(headers, raw_body: bytes, secret: str) -> bool:
    ts = headers["X-CCTV-Timestamp"]
    mac = hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return abs(time.time() - int(ts)) < 300 and hmac.compare_digest(
        headers["X-CCTV-Signature"], f"v1={mac}"
    )
```

Answer with any **2xx** within 10 seconds (do slow work afterwards). On a timeout, 5xx,
408, 425 or 429 we retry with growing pauses (30 s, 1 min, 2 min, … up to 1 h) for up to
a day, so the same message can arrive more than once: use `X-CCTV-Webhook-Id` to ignore
repeats. Other 4xx answers are not retried. Redirects are not followed. The URL must be
`https://` and reach a public address. The owner sees the last 30 days of sends, with
your answers, on the same page.

## Security summary

* Sign-in tokens: signed with your secret or key, checked against your issuer, audience
  and our clock (30 s tolerance), valid 5 minutes at most, usable once; owners need 2FA
  on your side. Every sign-in through your system is in the dashboard's audit log with
  your `sub`.
* The dashboard keeps its own roles and branch access; a token can't raise them.
* Embedding is limited to the origins we allow; the rest of the dashboard can't be framed.
* API keys are per client, scoped, revocable, and stored hashed.
* Webhooks carry no images or names, are signed, go only to public https addresses, and
  the secret can be replaced at any time (**New secret**; the old one stops at once).
* Shared secrets (sign-in and webhooks) are stored encrypted.
