# Email Channel

The Email channel adapter drives the AgentOS runtime from a standard inbox,
using **IMAP** to receive instructions and **SMTP** to deliver replies. It is
designed for self-hosted, local-first use — web-API relays such as Mailgun or
SendGrid are deliberately out of scope. The runtime behaviour described here is
implemented by the adapter introduced in **PR #523** (`Feature/email robustness
369`, `Refs #522`); this page documents how to operate it.

## When to Use It

Use the Email channel when:

- Operators already have a mailbox they want to turn into an agent inbox
  (personal, team shared, or a dedicated subaddress).
- The deployment must stay self-hosted and cannot depend on an external
  webhook service or paid API tier.
- The agent needs to read messages on arrival (sub-second with IMAP IDLE)
  rather than on a user-triggered refresh.

The Email channel is marked **YELLOW-experimental** while the adapter
stabilises. Expect the configuration surface to evolve alongside the
runtime.

## Setup Flow

Standard add-channel flow:

```sh
agentos channels add email --name inbox
agentos channels describe email
agentos channels status inbox --json
```

`channels describe email` is the source of truth for required fields, the
auto-redacted secrets list, and restart behaviour — always read its output
before committing credentials to the config.

The recognised fields are:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `imap_server` | string | `""` | Hostname of the IMAP service. |
| `imap_port` | int | `993` | Standard IMAPS port. |
| `imap_use_ssl` | bool | `true` | Use `imaplib.IMAP4_SSL`. |
| `imap_username` | string | `""` | Often the full mailbox address. |
| `imap_password` | secret | `""` | App password or token, never the account password. |
| `smtp_server` | string | `""` | Hostname of the SMTP submission service. |
| `smtp_port` | int | `587` | Submission port; use `465` for SSL. |
| `smtp_use_tls` | bool | `true` | `STARTTLS` upgrade. Ignored when `smtp_use_ssl=true`. |
| `smtp_use_ssl` | bool | `false` | TLS from the first byte (`SMTP_SSL`). |
| `smtp_username` | string | `""` | Often identical to `imap_username`. |
| `smtp_password` | secret | `""` | Same shape as `imap_password`. |
| `allowed_from_addresses` | list[string] | `[]` | Empty = reject all inbound; non-empty = allowlist. |
| `poll_interval_s` | float | `30.0` | Polling fallback interval when IDLE is unavailable. |
| `timeout_s` | float | `15.0` | Socket connect/read timeout in seconds. |

Restart the gateway after config edits:

```sh
agentos gateway restart
```

## Transport Selection

### SMTP — STARTTLS vs. SSL-on-Connect

Two distinct security postures are available, and they map cleanly onto
provider conventions:

- **STARTTLS (port 587).** Plain TCP, then `STARTTLS` to upgrade. This is
  the default (`smtp_use_tls=true`, `smtp_ssl_use=false`).
- **SSL-on-connect (port 465).** TLS from the first byte via `smtplib.SMTP_SSL`.
  Required by providers such as **Zoho Mail**, **Fastmail**, and many
  private enterprise relays that do not run a STARTTLS service on 587.
  Set `smtp_use_ssl = true` and switch `smtp_port = 465`.

Setting both `smtp_use_tls` and `smtp_use_ssl` to `true` is **not** a
double-encrypt — the adapter uses `SMTP_SSL` first and ignores STARTTLS in
that mode. Set exactly one of them.

### IMAP — IDLE Push vs. Polling Fallback

IMAP **IDLE** ([RFC 2177](https://datatracker.ietf.org/doc/html/rfc2177))
is a server-side push mechanism. The adapter attempts IDLE whenever the
connecting mailbox advertises the `IDLE` capability after `LOGIN`. While
IDLE is active the agent holds a single persistent connection and
receives new-message notifications in well under a second, with no
repeated handshake overhead on the mail server.

When the server does **not** advertise IDLE, the adapter falls back to a
persistent polling loop that reuses its connection at `poll_interval_s`
intervals instead of disconnecting and logging in again every poll. That
fallback is the durable default; falling back is graceful, not an error.

IDLE is chosen automatically — no flag is required to enable it. The
adapter logs whether it entered IDLE or fell back to polling on start,
and the same line shows up in `agentos channels status inbox --json` under
the channel's `extra` field.

## Timeout Recommendations

Network timeouts are governed by a single field, `timeout_s`, applied to
every IMAP and SMTP connect/read call:

| Profile | `timeout_s` | Trade-off |
| --- | --- | --- |
| Conservative (recommended) | `15.0` | Same as the default. Detects half-open sockets within ~15 s and bounds stuck threads. |
| Aggressive LAN / loopback | `5.0` | Lower floor for local test servers; raises spurious failures on flaky links. |
| Latency-tolerant mobile / satellite | `30.0` | Lets one slow round-trip complete; do not exceed `45.0`. |

Hard guidance:

- **Start at `15.0`** — that is also the adapter default and matches the
  PR #523 baseline. Most operators do not need to change it.
- **Do not exceed `45.0`.** Above that, a stuck socket holds a worker
  thread long enough to starve the gateway executor under load.
- **Keep `poll_interval_s >= 2 * timeout_s`.** A poll cycle that can take
  longer than the interval will queue reconnects and trigger rate limits
  on the mail server.

## Redacted Configuration Example

This snippet shows the field set with secrets replaced by placeholders.
Paste it under `[channels.channels]` in `agentos.toml`, then run
`agentos channels describe email` to confirm every required field is
present before restarting the gateway.

```toml
[[channels.channels]]
name = "inbox"
type = "email"

# Receive (IMAPS, IDLE-capable)
imap_server   = "imap.example.com"
imap_port     = 993
imap_use_ssl  = true
imap_username = "agent@example.com"
imap_password = "<IMAP_APP_PASSWORD_REDACTED>"

# Submit (direct SMTP over SSL on port 465 — required by Zoho, Fastmail,
# many enterprise relays; idomatic for any provider that doesn't offer
# STARTTLS on 587).
smtp_server   = "smtp.example.com"
smtp_port     = 465
smtp_use_tls  = false
smtp_use_ssl  = true
smtp_username = "agent@example.com"
smtp_password = "<SMTP_APP_PASSWORD_REDACTED>"

# Allowlist before the adapter ever logs in.
allowed_from_addresses = ["owner@example.com", "ops@example.com"]

# Network posture: 15s default — both IMAP and SMTP share timeout_s.
timeout_s      = 15.0
poll_interval_s = 30.0
```

For STARTTLS providers (Gmail, Microsoft 365 with basic auth disabled,
most ISP relays), flip `smtp_port = 587`, `smtp_use_tls = true`, and
`smtp_use_ssl = false` and keep `imap_*` untouched.

## Troubleshooting

A few common failure modes and the first thing to check for each:

- **`Authentication failed` on a working mailbox.** Almost always the
  account password rather than an app password; OAuth providers need an
  XOAUTH2 token, which the adapter does not negotiate.
- **Repeated reconnect log spam.** IDLE was negotiated but the server
  closed the socket — usually a NAT/firewall idle-kill. Lower
  `poll_interval_s` and verify the gateway host has a keepalive route.
- **`Connection timed out` cycling.** Bump `timeout_s` in 5-second
  increments up to 30, then inspect upstream firewall logs; refusing at
  the edge before the timeout elapses is the server hint that a CIDR or
  port is blocked.
- **No inbound traffic at all.** `allowed_from_addresses` is empty —
  empty means *reject everything*, not *allow everything*. Add at least
  one entry before testing.

For deeper diagnosis, see
[`docs/troubleshooting.md`](../troubleshooting.md) and
[`docs/operations.md`](../operations.md). The runtime status output is:

```sh
agentos channels status inbox --json
```

## Reference

The runtime behaviour described here — `SMTP_SSL` on port 465, `IMAP
IDLE` with persistent-connection fallback, and the shared `timeout_s`
socket ceiling — is implemented by **PR #523** (`Feature/email robustness
369`) and tracks the original request in **issue #522**. This page is
the operator-facing complement to that change; schema details live in
the adapter source under `src/agentos/channels/email.py`.

---

[Channels overview](../channels.md) ·
[Configuration](../configuration.md) ·
[Troubleshooting](../troubleshooting.md) ·
[Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
