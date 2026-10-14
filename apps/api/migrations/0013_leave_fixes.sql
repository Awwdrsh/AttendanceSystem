-- Event-based statutory leave (Labour Act 2074 Sec 45-47): granted per event, not accrued into a balance.
-- event_days is the paid entitlement per event; NULL means the type is balance-based.
ALTER TABLE leave_type ADD COLUMN IF NOT EXISTS event_days numeric(6,2);

UPDATE leave_type SET event_days = v.days
  FROM (VALUES ('MATERNITY', 60), ('PATERNITY', 15), ('MOURNING', 13)) AS v(code, days)
 WHERE leave_type.code = v.code AND leave_type.statutory AND leave_type.event_days IS NULL;
