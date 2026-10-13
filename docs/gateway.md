# Device gateway: connecting terminals

The gateway (`apps/gateway`, port 8080) is the only service terminals ever talk to (plan §7, §8.2, §12.5). Terminals are
HTTP clients: they push punches to the gateway and poll it for commands, so branches need no static IP and no inbound
firewall rule (plan §8.7).

## Network rules (plan §7.6, §12.5)
- Put terminals on their own VLAN with no internet and no DNS. Allow only `terminal → gateway:8080` (HTTPS 443 once a
  certificate is in place) and `jump host → terminal` for administration. Block everything else, including terminal to
  terminal.
- Change every terminal's admin password and ADMS comm key from the factory default before it goes on the network.
- Set the terminal time zone to UTC+05:45 and let it take time from the gateway host (NTP on the same host). Hikvision
  Digest auth breaks at more than 5 minutes of drift; the gateway records `clock_drift_sec` per device and warns above 60 s.

## Approving a new terminal
1. Configure the terminal (below). On first contact it appears in the admin web under Devices with status **pending**;
   nothing it sends is accepted until approved (HTTP 403, so the terminal keeps buffering and retrying).
2. Approve it: `POST /api/v1/devices/:id/activate` with `{ "branchId": "...", "name": "Front door" }` (HR/admin), or the
   Devices screen. More than `GATEWAY_MAX_PENDING_DEVICES` (50) unapproved serials are refused outright.
3. Map terminal user PINs to employees: `POST /api/v1/devices/:id/map` `{ "pin": "12", "employeeId": "...", "from": "2026-09-01" }`.
   Punches already received for that PIN are re-attributed and recomputed; for ZKTeco the name is pushed to the terminal.
4. Do a test punch and confirm it appears in Team Today within a minute (plan §16.4: verified within 5 minutes).

## ZKTeco / eSSL (ADMS / PUSH, plan Appendix B)
On the terminal: Menu → Comm → Cloud Server Setting (names vary by firmware: "ADMS", "Cloud server", "Webserver"):
- Server mode: ADMS / cloud server **enabled**
- Server address: the gateway host IP or name; Server port: `8080` (or 443 behind TLS)
- Enable domain name: as needed; Proxy: off
- The URL path is fixed by the firmware (`/iclock/...`); nothing to type.
- Comm key: set a non-zero value on the terminal and record it in the password manager. The PUSH protocol does not
  carry it per request, so the gateway cannot verify it; the serial allow-list and the VLAN are the controls (see gaps).
- Serial numbers must be 1–64 characters of letters, digits, `_ . : -`; anything else is rejected with HTTP 400.
Endpoints served: `GET /iclock/cdata?SN=..&options=all` (handshake), `POST /iclock/cdata?SN=..&table=ATTLOG|OPERLOG|…`,
`GET /iclock/getrequest`, `POST /iclock/devicecmd`, `POST /iclock/fdata`. Templates arriving in OPERLOG/fdata are stored
encrypted (same key as the API) or discarded if `DATA_ENCRYPTION_KEY` is unset; photos are always discarded.

Simulator: `pnpm --filter @hajiri/gateway simulate -- --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03" --template`

## Hikvision (ISAPI HTTP listening host, plan Appendix C)
Configure the "HTTP listening" / "alarm server" host once per terminal, from the jump host (Digest auth, admin user):
```
PUT http://<terminal>/ISAPI/Event/notification/httpHosts/1
Content-Type: application/json

{"HttpHostNotification": {
  "id": "1",
  "url": "/hikvision/event?sn=<terminal serial number>&key=<device secret>",
  "protocolType": "HTTP",
  "parameterFormatType": "JSON",
  "addressingFormatType": "ipaddress",
  "ipAddress": "<gateway ip>",
  "portNo": 8080,
  "httpAuthenticationMethod": "none",
  "uploadImagesDataType": "URL",
  "eventType": "AccessControllerEvent",
  "intervalBetweenEvents": 0,
  "heartbeat": 30
}}
```
Both JSON and the older `<EventNotificationAlert>` XML are parsed (XML through a minimal field extractor; prefer JSON).
The gateway keeps attendance events with major 5 / minor 75 (face), 113 (fingerprint), 38 (card); heartbeats only
update "last seen". Unapproved serials get 403 so the terminal re-sends after approval. Put the serial in the URL
(`?sn=`); the JSON `serialNo` is used only as a fallback.

**Per-device secret (plan §12.5).** Because anyone on the VLAN could post an event with a known serial, give each
Hikvision terminal its own secret: store `sha256(secret)` in `device.comm_key_hash` (HR/admin, via SQL or the Devices
screen once exposed) and put `&key=<secret>` in the listening-host URL (or send it as the `x-device-key` header). Events
without the right key are refused with 403. ADMS terminals cannot send a key, so the check is skipped for them.

Simulator: `pnpm --filter @hajiri/gateway simulate -- --vendor hikvision --sn HIK-01 --pin 1 --time "2026-09-21 10:17:03"`

## Daily reconciliation (plan §7.7)
`pnpm --filter @hajiri/gateway reconcile -- --all` asks every active ADMS terminal to re-send yesterday's log; the
idempotent ingest fills any gap and ignores the rest. Run it from cron at 01:00 (`crontab`: `0 1 * * * cd <repo> &&
pnpm --filter @hajiri/gateway --silent reconcile -- --all >> logs/reconcile.log 2>&1`). Hikvision terminals are
reconciled through `AcsEvent` search from the API once device credentials are stored there (not yet implemented).

## Commands to terminals
Commands queued in `device_command` are delivered on the next `getrequest` poll (about every 10 s). A command that is
not acknowledged within 10 minutes (terminal rebooted, poll lost) is re-sent once and then marked `failed`; late
acknowledgements of failed commands are ignored. Every command must be a single line; `SHELL` is refused everywhere,
and `CLEAR DATA`, `CONTROL DEVICE`, firmware and file-transfer verbs are refused unless
`GATEWAY_ALLOW_DANGEROUS_COMMANDS=true`.

## Raw archive
Every POST body from an **approved** terminal is appended verbatim to `apps/gateway/data/raw/<serial>/<date>.log`
(`GATEWAY_RAW_DIR`), rotating at 50 MB per file with at most five files per device per day (bodies beyond that are
dropped and counted). Unknown serials are never archived. The archive is the forensic record for disputes and protocol
debugging; it contains template uploads, so treat the directory as sensitive and include it in the backup/retention
policy (plan §12.9, §12.11).

## Device onboarding SOP (plan §16.4)
1. Receive the unit; check model and firmware against the acceptance checklist (plan Appendix D); update to the
   CVE-fixed firmware before it touches the network (plan §7.6).
2. Set the admin password and (ZKTeco) comm key from the password manager; (Hikvision) generate the device secret,
   store its sha256 in `device.comm_key_hash` after approval.
3. Configure the server address/port (above), time zone **UTC+05:45**, and NTP to the gateway host.
4. Connect it to the terminal VLAN. It appears as *pending*; approve it in Devices with the branch and a name.
5. Map PINs to employees (or import the terminal's user list), with consent recorded per employee
   (`POST /api/v1/biometrics/consent`) before enrolling faces/fingerprints.
6. Test punch: it must show in Team Today within 5 minutes. Record the firmware hash, a photo of the installation and
   the serial in the asset register; add the device to monitoring (offline alert after 120 s, page at 15 min).
7. Disposal or RMA: disable the device, `shred-all` templates for anyone enrolled only there, and wipe the terminal
   (`CLEAR DATA` with `GATEWAY_ALLOW_DANGEROUS_COMMANDS=true`, then factory reset locally).

## Environment (apps/gateway/.env)
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | shared database; omit for the in-memory demo store |
| `GATEWAY_SIGNING_KEY` | Ed25519 key that signs every stored punch |
| `DATA_ENCRYPTION_KEY` | same value as the API; templates are discarded without it |
| `GATEWAY_MAX_PENDING_DEVICES` | cap on unapproved serials (default 50) |
| `GATEWAY_MAX_CLOCK_SKEW_MIN` | punches further in the future are flagged `clock_skew` (default 10) |
| `GATEWAY_RAW_DIR` | raw archive directory |
| `GATEWAY_ALLOW_DANGEROUS_COMMANDS` | `CLEAR DATA`/firmware verbs refused unless `true`; `SHELL` is never allowed |

## Known gaps: verify on purchased hardware before go-live
- ZKTeco comm key is not validated by the gateway (the PUSH protocol does not carry it per request on all firmwares);
  the SN allow-list and VLAN are the controls. HTTPS with a real certificate is required for production; some firmwares
  reject self-signed certificates (plan Appendix B).
- ADMS status/verify code tables, BIODATA `Type` codes, template line layouts (`FP`, `FACE`, `BIODATA`) and the time
  sync command come from community implementations, not the vendor PUSH SDK document; capture traffic from the bench
  units and adjust `apps/gateway/src/adms/*.ts`.
- Hikvision JSON and XML field names (`AccessControllerEvent`, `employeeNoString`, `majorEventType`, `subEventType`,
  `attendanceStatus`) are from community captures; the official ISAPI document is under NDA.
- Per-device certificates / mTLS (plan §12.5) are not implemented; ADMS terminals are trusted by serial number,
  Hikvision terminals by serial plus the optional shared secret.
- Device offline alerts (120 s silence, page at 15 min) are raised by the API worker from `device.last_seen_at`.
- Hikvision `AcsEvent` reconciliation (Appendix C) needs device credentials stored on the API side; not implemented.
- The database outage path returns HTTP 503 so terminals keep buffering; confirm on the bench that the firmware
  retries on 503 as it does on timeouts.
