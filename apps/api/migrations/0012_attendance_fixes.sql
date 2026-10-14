-- Overtime punch-outs must belong to the day: the late-out window covers the 4 h daily OT cap with margin.
-- Shifts still on the original 2 h default move to 6 h; deliberately configured values are kept.
ALTER TABLE shift ALTER COLUMN late_out_window_min SET DEFAULT 360;
UPDATE shift SET late_out_window_min = 360 WHERE late_out_window_min = 120;
