-- Delivery scheduling for the notification outbox (retry backoff, quiet-hour deferral; plan §13.10).
ALTER TABLE notification_outbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX notification_due_idx ON notification_outbox (next_attempt_at) WHERE status = 'queued';
