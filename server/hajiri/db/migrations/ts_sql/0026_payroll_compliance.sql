-- Payroll and tax compliance extras: approved donations (Income Tax Act Sec 12) on declarations, a CIT amount that
-- starts from a chosen month (optimum CIT), opening balances for mid-year joiners and go-live, and held pay.

ALTER TABLE tax_declaration ADD COLUMN IF NOT EXISTS donation numeric(14,2) NOT NULL DEFAULT 0;
-- When HR applies a new CIT amount from a later month of the fiscal year (1 = Shrawan ... 12 = Ashadh), months before
-- it keep the amount that applied until then.
ALTER TABLE tax_declaration ADD COLUMN IF NOT EXISTS cit_from_month smallint CHECK (cit_from_month BETWEEN 1 AND 12);
ALTER TABLE tax_declaration ADD COLUMN IF NOT EXISTS cit_prev_monthly numeric(14,2) NOT NULL DEFAULT 0;
ALTER TABLE tax_declaration_request ADD COLUMN IF NOT EXISTS donation numeric(14,2) NOT NULL DEFAULT 0;

-- Income, tax and retirement contributions earned in this fiscal year before Hajiri ran the employee's payroll
-- (a previous employer, or months paid by the old system before go-live). Added to year-to-date for the tax projection,
-- the tax certificate and the annual tax summary.
CREATE TABLE IF NOT EXISTS payroll_opening_balance (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  employee_id         uuid NOT NULL REFERENCES employee(id),
  fiscal_year_start   integer NOT NULL CHECK (fiscal_year_start BETWEEN 2070 AND 2200),
  gross               numeric(14,2) NOT NULL DEFAULT 0 CHECK (gross >= 0),
  taxable             numeric(14,2) NOT NULL DEFAULT 0 CHECK (taxable >= 0),
  tds_paid            numeric(14,2) NOT NULL DEFAULT 0 CHECK (tds_paid >= 0),
  retirement_employee numeric(14,2) NOT NULL DEFAULT 0 CHECK (retirement_employee >= 0),
  retirement_employer numeric(14,2) NOT NULL DEFAULT 0 CHECK (retirement_employer >= 0),
  festival_paid       boolean NOT NULL DEFAULT false,
  months              smallint NOT NULL DEFAULT 0 CHECK (months BETWEEN 0 AND 12),
  source              text NOT NULL DEFAULT 'previous_employer' CHECK (source IN ('previous_employer', 'go_live', 'other')),
  note                text CHECK (length(note) <= 500),
  updated_by          uuid REFERENCES app_user(id),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (employee_id, fiscal_year_start)
);
CREATE INDEX IF NOT EXISTS payroll_opening_balance_company_idx ON payroll_opening_balance (company_id, fiscal_year_start);

-- Held pay: HR stops one employee's net pay in a run (pending documents, an investigation, an unreturned asset). The
-- payslip stays as computed (tax and contributions are still due); the net is paid later, through a later run as a
-- non-taxable "Released pay" line, or outside payroll.
CREATE TABLE IF NOT EXISTS payroll_hold (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  employee_id     uuid NOT NULL REFERENCES employee(id),
  run_id          uuid NOT NULL REFERENCES payroll_run(id),
  amount          numeric(14,2) NOT NULL DEFAULT 0 CHECK (amount >= 0),
  reason          text NOT NULL CHECK (length(reason) BETWEEN 3 AND 500),
  status          text NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'releasing', 'released', 'cancelled')),
  release_mode    text CHECK (release_mode IN ('payroll', 'outside')),
  release_run_id  uuid REFERENCES payroll_run(id),
  release_note    text CHECK (length(release_note) <= 500),
  held_by         uuid REFERENCES app_user(id),
  held_at         timestamptz NOT NULL DEFAULT now(),
  released_by     uuid REFERENCES app_user(id),
  released_at     timestamptz,
  UNIQUE (run_id, employee_id)
);
CREATE INDEX IF NOT EXISTS payroll_hold_company_idx ON payroll_hold (company_id, status);

UPDATE company_setting SET value = (value - 'twoApproverThreshold') || '{"publishOnLock": false}'::jsonb
 WHERE key = 'payroll.policy' AND NOT value ? 'publishOnLock';
