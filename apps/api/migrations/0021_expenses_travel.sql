-- Expense claims and travel (TADA): categories with limits per grade, claims with receipts, travel requests with
-- daily allowance by grade and destination, post-trip settlements, and payouts that the next pay run picks up as
-- non-taxable reimbursements (or recoveries). Every payout is paid exactly once: it is claimed by one run when that
-- run is computed and marked paid when the run is locked.
CREATE TABLE IF NOT EXISTS expense_category (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id             uuid NOT NULL REFERENCES company(id),
  code                   text NOT NULL CHECK (code ~ '^[A-Z][A-Z0-9_]{1,29}$'),
  name                   text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  kind                   text NOT NULL DEFAULT 'amount' CHECK (kind IN ('amount', 'mileage')),
  receipt_required_above numeric(14,2) CHECK (receipt_required_above >= 0),
  active                 boolean NOT NULL DEFAULT true,
  sort_order             integer NOT NULL DEFAULT 0,
  created_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

-- Limits per grade; grade '*' applies to every grade without its own row. NULL = no limit.
CREATE TABLE IF NOT EXISTS expense_limit (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  category_id uuid NOT NULL REFERENCES expense_category(id) ON DELETE CASCADE,
  grade       text NOT NULL CHECK (length(grade) BETWEEN 1 AND 40),
  per_claim   numeric(14,2) CHECK (per_claim >= 0),
  per_month   numeric(14,2) CHECK (per_month >= 0),
  UNIQUE (category_id, grade)
);

-- Daily allowance and lodging limit per night, by grade ('*' = default) and destination category.
-- International rates are in US dollars and converted with the rate entered on the travel request.
CREATE TABLE IF NOT EXISTS tada_rate (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  grade        text NOT NULL CHECK (length(grade) BETWEEN 1 AND 40),
  destination  text NOT NULL CHECK (destination IN ('valley', 'city', 'rural', 'international')),
  da_rate      numeric(14,2) NOT NULL CHECK (da_rate >= 0),
  lodging_rate numeric(14,2) NOT NULL CHECK (lodging_rate >= 0),
  UNIQUE (company_id, grade, destination)
);

CREATE TABLE IF NOT EXISTS expense_claim (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  title                text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  status               text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'pending', 'approved', 'rejected', 'cancelled', 'paid')),
  total_claimed        numeric(14,2) NOT NULL DEFAULT 0,
  total_approved       numeric(14,2),
  workflow_instance_id uuid REFERENCES workflow_instance(id),
  submitted_at         timestamptz,
  decided_at           timestamptz,
  decision_note        text,
  payment_method       text CHECK (payment_method IN ('payroll', 'cash')),
  paid_at              timestamptz,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS expense_claim_employee_idx ON expense_claim (employee_id, created_at DESC);
CREATE INDEX IF NOT EXISTS expense_claim_company_idx ON expense_claim (company_id, status);

CREATE TABLE IF NOT EXISTS expense_item (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  claim_id        uuid NOT NULL REFERENCES expense_claim(id) ON DELETE CASCADE,
  expense_date    date NOT NULL,
  category_id     uuid NOT NULL REFERENCES expense_category(id),
  description     text NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  amount          numeric(14,2) NOT NULL CHECK (amount > 0),
  km              numeric(8,1) CHECK (km > 0),
  receipt_file_id uuid REFERENCES stored_file(id),
  approved_amount numeric(14,2) CHECK (approved_amount >= 0),
  adjust_reason   text,
  sort_order      integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS expense_item_claim_idx ON expense_item (claim_id);

CREATE TABLE IF NOT EXISTS travel_request (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  destination          text NOT NULL CHECK (length(destination) BETWEEN 1 AND 120),
  destination_category text NOT NULL CHECK (destination_category IN ('valley', 'city', 'rural', 'international')),
  purpose              text NOT NULL CHECK (length(purpose) BETWEEN 3 AND 1000),
  from_date            date NOT NULL,
  to_date              date NOT NULL CHECK (to_date >= from_date),
  mode                 text NOT NULL CHECK (mode IN ('bus', 'air', 'office_vehicle', 'own_vehicle', 'other')),
  estimated_cost       numeric(14,2) NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
  advance_requested    numeric(14,2) NOT NULL DEFAULT 0 CHECK (advance_requested >= 0),
  advance_approved     numeric(14,2),
  advance_method       text CHECK (advance_method IN ('payroll', 'cash')),
  advance_status       text NOT NULL DEFAULT 'none' CHECK (advance_status IN ('none', 'pending', 'paid')),
  fx_rate              numeric(10,4) CHECK (fx_rate > 0),
  days                 integer NOT NULL CHECK (days > 0),
  nights               integer NOT NULL CHECK (nights >= 0),
  da_rate              numeric(14,2) NOT NULL,
  lodging_rate         numeric(14,2) NOT NULL,
  da_amount            numeric(14,2) NOT NULL,
  lodging_limit        numeric(14,2) NOT NULL,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'settling', 'settled')),
  workflow_instance_id uuid REFERENCES workflow_instance(id),
  decided_at           timestamptz,
  decision_note        text,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS travel_request_employee_idx ON travel_request (employee_id, from_date DESC);
CREATE INDEX IF NOT EXISTS travel_request_company_idx ON travel_request (company_id, status);

CREATE TABLE IF NOT EXISTS travel_settlement (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  travel_request_id    uuid NOT NULL REFERENCES travel_request(id),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  actual_from          date NOT NULL,
  actual_to            date NOT NULL CHECK (actual_to >= actual_from),
  days                 integer NOT NULL,
  nights               integer NOT NULL,
  da_amount            numeric(14,2) NOT NULL,
  lodging_claimed      numeric(14,2) NOT NULL,
  lodging_allowed      numeric(14,2) NOT NULL,
  other_bills          numeric(14,2) NOT NULL,
  entitlement          numeric(14,2) NOT NULL,
  advance              numeric(14,2) NOT NULL,
  net                  numeric(14,2) NOT NULL,
  note                 text,
  workflow_instance_id uuid REFERENCES workflow_instance(id),
  decided_at           timestamptz,
  decision_note        text,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS travel_settlement_active_idx ON travel_settlement (travel_request_id) WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS travel_bill (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  settlement_id uuid NOT NULL REFERENCES travel_settlement(id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('transport', 'lodging', 'food', 'other')),
  bill_date     date NOT NULL,
  description   text NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  amount        numeric(14,2) NOT NULL CHECK (amount > 0),
  file_id       uuid REFERENCES stored_file(id)
);
CREATE INDEX IF NOT EXISTS travel_bill_settlement_idx ON travel_bill (settlement_id);

-- What the company owes (earnings) or recovers (deductions) through payroll or in cash.
CREATE TABLE IF NOT EXISTS expense_payout (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id       uuid NOT NULL REFERENCES company(id),
  employee_id      uuid NOT NULL REFERENCES employee(id),
  source_type      text NOT NULL CHECK (source_type IN ('expense_claim', 'travel_advance', 'travel_settlement')),
  source_id        uuid NOT NULL,
  component_code   text NOT NULL CHECK (component_code IN ('EXP_REIMB', 'TRAVEL_ADV', 'EXP_RECOVERY')),
  amount           numeric(14,2) NOT NULL CHECK (amount > 0),
  note             text,
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'cash', 'cancelled')),
  payroll_run_id   uuid REFERENCES payroll_run(id) ON DELETE SET NULL,
  paid_amount      numeric(14,2),
  paid_at          timestamptz,
  parent_payout_id uuid REFERENCES expense_payout(id),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS expense_payout_source_idx ON expense_payout (source_type, source_id, component_code) WHERE parent_payout_id IS NULL;
CREATE INDEX IF NOT EXISTS expense_payout_pending_idx ON expense_payout (company_id, employee_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS expense_payout_run_idx ON expense_payout (payroll_run_id) WHERE payroll_run_id IS NOT NULL;

-- Defaults for every existing company (new companies get the same from seed-defaults).
INSERT INTO expense_category (company_id, code, name, kind, receipt_required_above, sort_order)
SELECT c.id, v.code, v.name, v.kind, v.receipt::numeric, v.sort
  FROM company c CROSS JOIN (VALUES
    ('LOCAL_TRAVEL', 'Local travel', 'amount', '500', 10), ('MEALS', 'Meals', 'amount', '500', 20), ('FUEL', 'Fuel (own vehicle)', 'mileage', NULL, 30),
    ('MOBILE', 'Mobile & internet', 'amount', '0', 40), ('MEDICAL', 'Medical', 'amount', '0', 50), ('OFFICE', 'Office supplies', 'amount', '0', 60),
    ('OTHER', 'Other', 'amount', '0', 70)) AS v(code, name, kind, receipt, sort)
ON CONFLICT (company_id, code) DO NOTHING;

INSERT INTO tada_rate (company_id, grade, destination, da_rate, lodging_rate)
SELECT c.id, '*', v.dest, v.da::numeric, v.lodging::numeric
  FROM company c CROSS JOIN (VALUES ('valley', '1000', '2500'), ('city', '1500', '3000'), ('rural', '1200', '2000'), ('international', '50', '100')) AS v(dest, da, lodging)
ON CONFLICT (company_id, grade, destination) DO NOTHING;

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT c.id, v.t, '[{"role":"manager"},{"role":"hr"}]'::jsonb FROM company c CROSS JOIN (VALUES ('expense_claim'), ('travel_request'), ('travel_settlement')) AS v(t)
ON CONFLICT (company_id, object_type) DO NOTHING;

INSERT INTO company_setting (company_id, key, value)
SELECT c.id, 'expense.policy', '{"mileageRatePerKm": "15", "limitMode": "block", "claimWindowDays": 90}'::jsonb FROM company c
ON CONFLICT DO NOTHING;
