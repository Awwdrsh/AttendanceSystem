-- Run parameters for special run types (final settlement employee, bonus pool, arrears reference) and per-payslip result cache.
ALTER TABLE payroll_run ADD COLUMN params jsonb NOT NULL DEFAULT '{}';
ALTER TABLE payroll_run ADD COLUMN totals jsonb;

-- Locked-run guard rewritten so each table only touches its own record fields.
CREATE OR REPLACE FUNCTION payslip_locked_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_locked timestamptz; target uuid;
BEGIN
  IF TG_TABLE_NAME = 'payslip' THEN
    IF TG_OP = 'DELETE' THEN target := OLD.run_id; ELSE target := NEW.run_id; END IF;
    SELECT r.locked_at INTO run_locked FROM payroll_run r WHERE r.id = target;
    IF run_locked IS NOT NULL THEN
      -- Publishing (published_at) is the only change allowed after lock; every amount must stay identical.
      IF TG_OP = 'UPDATE' AND OLD.published_at IS DISTINCT FROM NEW.published_at AND OLD.net = NEW.net AND OLD.gross = NEW.gross
         AND OLD.tds = NEW.tds AND OLD.total_deductions = NEW.total_deductions AND OLD.taxable = NEW.taxable THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'payroll run is locked; % on payslip is not allowed (create a supplementary run)', TG_OP;
    END IF;
  ELSE
    IF TG_OP = 'DELETE' THEN target := OLD.payslip_id; ELSE target := NEW.payslip_id; END IF;
    SELECT r.locked_at INTO run_locked FROM payslip p JOIN payroll_run r ON r.id = p.run_id WHERE p.id = target;
    IF run_locked IS NOT NULL THEN
      RAISE EXCEPTION 'payroll run is locked; % on payslip_line is not allowed (create a supplementary run)', TG_OP;
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
