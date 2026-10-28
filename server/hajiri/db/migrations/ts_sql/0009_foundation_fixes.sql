-- Foundation hardening: retry accounting for the recompute queue, indexes for date-first attendance and punch lookups.
ALTER TABLE recompute_queue ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE recompute_queue ADD COLUMN IF NOT EXISTS last_error text;
CREATE INDEX IF NOT EXISTS attendance_day_date_idx ON attendance_day (local_date, employee_id, version DESC);
CREATE INDEX IF NOT EXISTS punch_event_employee_date_idx ON punch_event (employee_id, local_date);
CREATE INDEX IF NOT EXISTS attendance_day_flags_gin ON attendance_day USING gin (flags jsonb_path_ops);
