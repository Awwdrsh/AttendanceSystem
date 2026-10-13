# Notifications

`notify()` writes to the transactional outbox inside the caller's transaction. In-app rows are visible immediately at
`GET /api/v1/notifications`; email/sms/push/webhook rows are delivered by the worker (`deliverQueued`) with retry
backoff (5 attempts) and quiet-hour deferral for sms/push (company setting `notifications.policy`).

Senders are configured from the API process environment (no extra packages):

| Variable | Effect |
|---|---|
| `SMS_WEBHOOK_URL` | POST `{channel:"sms", to, title, body, template, payload, id}` for each SMS (Sparrow/NTC/Ncell bridge) |
| `EMAIL_WEBHOOK_URL` | same JSON for email (point at an SMTP bridge) |
| `PUSH_WEBHOOK_URL` | same JSON for push |

Unset channels use a log-only sender in development. SMS bodies never contain amounts (`stripAmounts`).
