-- Device clock drift reported by the gateway (plan §7.7: alert at > 60 s drift).
ALTER TABLE device ADD COLUMN IF NOT EXISTS clock_drift_sec integer;
