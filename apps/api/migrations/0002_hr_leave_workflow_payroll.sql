-- Hajiri Phase 2 + 3 schema (plan.md §9): auth, employee extensions, leave, regularisation, OT, workflow,
-- notifications, rule versions, salary structures, payroll runs/payslips, loans, tax declarations, consent,
-- biometric templates, locations for mobile punch, recompute queue, company settings.

-- ---------------------------------------------------------------- auth
CREATE TABLE app_user (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid REFERENCES company(id),
  employee_id   uuid UNIQUE REFERENCES employee(id),
  email         text NOT NULL UNIQUE,
  name          text NOT NULL,
  password_hash text NOT NULL,
  roles         text[] NOT NULL DEFAULT '{employee}',    -- admin, hr, finance, manager, employee
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  must_change_password boolean NOT NULL DEFAULT false,
  failed_logins integer NOT NULL DEFAULT 0,
  locked_until  timestamptz,
  last_login_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE session (
  token_hash  text PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  ip          text,
  user_agent  text
);
CREATE INDEX session_user_idx ON session (user_id);

-- ---------------------------------------------------------------- company settings (plain-language rule builder storage)
CREATE TABLE company_setting (
  company_id uuid NOT NULL REFERENCES company(id),
  key        text NOT NULL,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES app_user(id),
  PRIMARY KEY (company_id, key)
);

-- ---------------------------------------------------------------- employee master extensions (plan §5.1)
ALTER TABLE employee
  ADD COLUMN marital_status   text CHECK (marital_status IN ('single', 'couple')),
  ADD COLUMN join_date        date,
  ADD COLUMN exit_date        date,
  ADD COLUMN designation      text,
  ADD COLUMN grade            text,
  ADD COLUMN phone            text,
  ADD COLUMN email            text,
  ADD COLUMN regime           text NOT NULL DEFAULT 'ssf' CHECK (regime IN ('ssf', 'pf', 'none')),
  ADD COLUMN ssf_id           text,
  ADD COLUMN pf_id            text,
  ADD COLUMN cit_id           text,
  ADD COLUMN pan_enc          bytea,          -- AES-256-GCM envelope (apps/api/src/crypto.ts)
  ADD COLUMN pan_bidx         text,           -- blind index (HMAC) for lookup
  ADD COLUMN bank_code        text,
  ADD COLUMN bank_branch      text,
  ADD COLUMN bank_account_enc bytea,
  ADD COLUMN bank_account_last4 text,
  ADD COLUMN is_resident      boolean NOT NULL DEFAULT true,
  ADD COLUMN remote_area_class text CHECK (remote_area_class IN ('A', 'B', 'C', 'D')),
  ADD COLUMN disability       boolean NOT NULL DEFAULT false,
  ADD COLUMN festival_month   smallint CHECK (festival_month BETWEEN 1 AND 12),  -- Sec 37(2) chosen festival; NULL = Ashwin
  ADD COLUMN probation_end    date;
CREATE INDEX employee_pan_bidx ON employee (pan_bidx) WHERE pan_bidx IS NOT NULL;

-- ---------------------------------------------------------------- leave (plan §5.6, §6.2, §10.6)
CREATE TABLE leave_type (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  code               text NOT NULL,
  name_en            text NOT NULL,
  name_ne            text,
  paid               boolean NOT NULL DEFAULT true,
  statutory          boolean NOT NULL DEFAULT false,
  accrual_basis      text NOT NULL DEFAULT 'none' CHECK (accrual_basis IN ('none', 'worked_days', 'monthly', 'annual')),
  accrual_days       numeric(6,2) NOT NULL DEFAULT 0,     -- days credited per accrual unit (e.g. 1 per 20 worked days; 1 per month)
  accrual_per_worked_days integer,                        -- for worked_days basis (20 for home leave)
  max_balance        numeric(6,2),                        -- cap (90 home, 45 sick)
  carry_forward      boolean NOT NULL DEFAULT true,
  encashable         boolean NOT NULL DEFAULT false,
  certificate_after_days integer,                         -- sick > 3 consecutive days
  allow_half_day     boolean NOT NULL DEFAULT true,
  max_consecutive_days integer,
  min_service_days   integer NOT NULL DEFAULT 0,
  negative_balance_limit numeric(6,2) NOT NULL DEFAULT 0,
  gender_scope       text NOT NULL DEFAULT 'all' CHECK (gender_scope IN ('all', 'female', 'male')),
  sandwich_rule      boolean NOT NULL DEFAULT false,
  expiry_days        integer,                             -- substitute leave: 21 days
  sort_order         integer NOT NULL DEFAULT 0,
  UNIQUE (company_id, code)
);

-- Double-entry ledger: balances are sums of delta_days, never stored (plan §9.1 decision 3).
CREATE TABLE leave_ledger (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  leave_type_id  uuid NOT NULL REFERENCES leave_type(id),
  posted_at      timestamptz NOT NULL DEFAULT now(),
  effective_date date NOT NULL,
  delta_days     numeric(6,2) NOT NULL,
  reason         text NOT NULL CHECK (reason IN ('allocation', 'accrual', 'usage', 'cancellation', 'carry_forward', 'expiry', 'encashment', 'adjustment', 'comp_off')),
  ref_type       text,
  ref_id         uuid,
  expires_on     date,
  note           text,
  posted_by      uuid REFERENCES app_user(id)
);
CREATE INDEX leave_ledger_emp_idx ON leave_ledger (employee_id, leave_type_id, effective_date);

CREATE TABLE leave_request (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  leave_type_id        uuid NOT NULL REFERENCES leave_type(id),
  from_date            date NOT NULL,
  to_date              date NOT NULL,
  half_day             text CHECK (half_day IN ('first_half', 'second_half')),
  days_requested       numeric(6,2) NOT NULL,
  reason               text,
  attachment_key       text,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  workflow_instance_id uuid,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  decided_at           timestamptz,
  CHECK (to_date >= from_date)
);
CREATE INDEX leave_request_emp_idx ON leave_request (employee_id, from_date, to_date);

-- ---------------------------------------------------------------- regularisation, WFH, on-duty (plan §5.4)
CREATE TABLE regularisation_request (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  local_date           date NOT NULL,
  kind                 text NOT NULL CHECK (kind IN ('missed_punch', 'wfh', 'on_duty', 'half_day', 'shift_change')),
  punch_time           timestamptz,
  punch_direction      text CHECK (punch_direction IN ('in', 'out')),
  shift_id             uuid REFERENCES shift(id),
  reason               text NOT NULL,
  attachment_key       text,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  workflow_instance_id uuid,
  resulting_punch_id   uuid,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  decided_at           timestamptz
);
CREATE INDEX regularisation_emp_idx ON regularisation_request (employee_id, local_date);

-- ---------------------------------------------------------------- overtime (plan §5.5, §10.4)
CREATE TABLE ot_request (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  local_date           date NOT NULL,
  requested_min        integer NOT NULL CHECK (requested_min > 0),
  approved_min         integer,
  reason               text,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  workflow_instance_id uuid,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  decided_at           timestamptz
);
CREATE INDEX ot_request_emp_idx ON ot_request (employee_id, local_date);

CREATE TABLE ot_slip (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id       uuid NOT NULL REFERENCES employee(id),
  local_date        date NOT NULL,
  attendance_day_id uuid REFERENCES attendance_day(id),
  detected_min      integer NOT NULL,
  approved_min      integer,
  multiplier        numeric(6,3) NOT NULL DEFAULT 1.5,
  day_type          text NOT NULL CHECK (day_type IN ('working', 'weekly_off', 'holiday')),
  status            text NOT NULL DEFAULT 'detected' CHECK (status IN ('detected', 'approved', 'rejected', 'posted', 'comp_off')),
  posted_payslip_id uuid,
  comp_off_ledger_id uuid REFERENCES leave_ledger(id),
  reviewed_by       uuid REFERENCES app_user(id),
  reviewed_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (employee_id, local_date)
);

-- ---------------------------------------------------------------- generic approval workflow (plan §5.8)
CREATE TABLE workflow_definition (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id       uuid NOT NULL REFERENCES company(id),
  object_type      text NOT NULL,                     -- leave_request, regularisation_request, ot_request, payroll_run
  steps            jsonb NOT NULL,                    -- [{"role":"manager"},{"role":"hr"}]
  sla_hours        integer NOT NULL DEFAULT 24,
  escalation_hours integer NOT NULL DEFAULT 48,
  UNIQUE (company_id, object_type)
);

CREATE TABLE workflow_instance (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  object_type   text NOT NULL,
  object_id     uuid NOT NULL,
  employee_id   uuid REFERENCES employee(id),         -- subject of the request (for manager resolution)
  steps         jsonb NOT NULL,
  current_step  integer NOT NULL DEFAULT 0,
  state         text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'rejected', 'cancelled')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  UNIQUE (object_type, object_id)
);
CREATE INDEX workflow_instance_pending_idx ON workflow_instance (company_id, state) WHERE state = 'pending';

CREATE TABLE approval_action (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  instance_id uuid NOT NULL REFERENCES workflow_instance(id),
  step        integer NOT NULL,
  actor_id    uuid NOT NULL REFERENCES app_user(id),
  decision    text NOT NULL CHECK (decision IN ('approve', 'reject')),
  reason      text,
  on_behalf_of uuid REFERENCES app_user(id),
  signature   text,
  acted_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- notifications (transactional outbox, plan §13.10)
CREATE TABLE notification_outbox (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid REFERENCES company(id),
  user_id     uuid REFERENCES app_user(id),
  employee_id uuid REFERENCES employee(id),
  channel     text NOT NULL CHECK (channel IN ('in_app', 'email', 'sms', 'push', 'webhook')),
  template    text NOT NULL,
  title       text NOT NULL,
  body        text NOT NULL,
  payload     jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
  attempts    integer NOT NULL DEFAULT 0,
  last_error  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  sent_at     timestamptz,
  read_at     timestamptz
);
CREATE INDEX notification_user_idx ON notification_outbox (user_id, created_at DESC);
CREATE INDEX notification_queue_idx ON notification_outbox (status, created_at) WHERE status = 'queued';

-- ---------------------------------------------------------------- recompute queue (worker; no external broker needed)
CREATE TABLE recompute_queue (
  employee_id uuid NOT NULL REFERENCES employee(id),
  local_date  date NOT NULL,
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  reason      text,
  PRIMARY KEY (employee_id, local_date)
);

-- ---------------------------------------------------------------- locations for mobile punch geofence (plan §5.2)
CREATE TABLE location (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  branch_id  uuid REFERENCES branch(id),
  name       text NOT NULL,
  lat        numeric(9,6) NOT NULL,
  lng        numeric(9,6) NOT NULL,
  radius_m   integer NOT NULL DEFAULT 150
);

-- ---------------------------------------------------------------- consent and biometric templates (plan §9.1 decisions 5-6)
CREATE TABLE consent_record (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  purpose        text NOT NULL,
  modality       text NOT NULL,
  notice_version text NOT NULL,
  signature      text,
  granted_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  recorded_by    uuid REFERENCES app_user(id)
);

CREATE TABLE biometric_template (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id          uuid REFERENCES employee(id),
  device_user_pin      text,
  modality             text NOT NULL,
  vendor_format        text NOT NULL,
  ciphertext           bytea NOT NULL,          -- AES-256-GCM envelope, AAD = employee/modality/purpose
  key_version          integer NOT NULL DEFAULT 1,
  aad_hash             text NOT NULL,
  enrolled_on_device_id uuid REFERENCES device(id),
  consent_id           uuid REFERENCES consent_record(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  shredded_at          timestamptz
);
CREATE INDEX biometric_template_emp_idx ON biometric_template (employee_id, modality);

-- ---------------------------------------------------------------- statutory rule versions (plan §6.13)
CREATE TABLE rule_version (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  rule_key      text NOT NULL,
  jurisdiction  text NOT NULL DEFAULT 'NP',
  valid_from_ad date NOT NULL,
  valid_to_ad   date,                                -- NULL = open-ended
  valid_from_bs text NOT NULL,
  valid_to_bs   text,
  value         jsonb NOT NULL,
  source_url    text,
  note          text,
  approved_by   uuid REFERENCES app_user(id),
  approved_at   timestamptz,
  supersedes_id uuid REFERENCES rule_version(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rule_version_key_idx ON rule_version (rule_key, jurisdiction, valid_from_ad);

-- ---------------------------------------------------------------- salary structures and components (plan §9.2, §11.1)
CREATE TABLE salary_component (
  code                    text NOT NULL,
  company_id              uuid NOT NULL REFERENCES company(id),
  name_en                 text NOT NULL,
  name_ne                 text,
  type                    text NOT NULL CHECK (type IN ('earning', 'deduction', 'employer_contribution', 'statistical')),
  formula                 text,                     -- DSL (packages/payroll-engine); NULL = amount from assignment/input
  condition               text,
  depends_on_payment_days boolean NOT NULL DEFAULT false,
  taxable                 boolean NOT NULL DEFAULT true,
  statutory               boolean NOT NULL DEFAULT false,
  rounding                text NOT NULL DEFAULT 'paisa' CHECK (rounding IN ('paisa', 'rupee')),
  sort_order              integer NOT NULL DEFAULT 0,
  active                  boolean NOT NULL DEFAULT true,
  PRIMARY KEY (company_id, code)
);

CREATE TABLE salary_structure (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  code       text NOT NULL,
  name_en    text NOT NULL,
  UNIQUE (company_id, code)
);

CREATE TABLE salary_structure_version (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  structure_id uuid NOT NULL REFERENCES salary_structure(id),
  version      integer NOT NULL,
  components   jsonb NOT NULL,                     -- [{"code":"BASIC"},{"code":"DA"},...] in evaluation order hints
  frozen_at    timestamptz,                        -- set once any assignment uses it (immutable afterwards)
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (structure_id, version)
);

CREATE TABLE salary_assignment (
  employee_id          uuid NOT NULL REFERENCES employee(id),
  structure_version_id uuid NOT NULL REFERENCES salary_structure_version(id),
  valid                daterange NOT NULL,
  base_amounts         jsonb NOT NULL,             -- {"BASIC":"40000.00","DA":"7380.00"} as decimal strings
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS)
);

CREATE TABLE tax_declaration (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id        uuid NOT NULL REFERENCES employee(id),
  fiscal_year_start  integer NOT NULL,             -- BS year the FY starts (2083 for 2083/84)
  life_premium       numeric(14,2) NOT NULL DEFAULT 0,
  health_premium     numeric(14,2) NOT NULL DEFAULT 0,
  cit_monthly        numeric(14,2) NOT NULL DEFAULT 0,
  medical_cost       numeric(14,2) NOT NULL DEFAULT 0,
  other_retirement_annual numeric(14,2) NOT NULL DEFAULT 0,
  status             text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'verified', 'rejected')),
  verified_by        uuid REFERENCES app_user(id),
  verified_at        timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (employee_id, fiscal_year_start)
);

CREATE TABLE loan (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id   uuid NOT NULL REFERENCES employee(id),
  kind          text NOT NULL DEFAULT 'loan' CHECK (kind IN ('loan', 'advance')),
  principal     numeric(14,2) NOT NULL,
  emi           numeric(14,2) NOT NULL,
  start_bs_year smallint NOT NULL,
  start_bs_month smallint NOT NULL,
  installments  integer NOT NULL,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed', 'paused')),
  note          text,
  created_by    uuid REFERENCES app_user(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- payroll periods, runs, payslips (plan §9.5, §11.8)
CREATE TABLE payroll_period (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  bs_year    smallint NOT NULL,
  bs_month   smallint NOT NULL,
  start_ad   date NOT NULL,
  end_ad     date NOT NULL,
  days       smallint NOT NULL,
  attendance_locked_at timestamptz,
  UNIQUE (company_id, bs_year, bs_month)
);

CREATE TABLE payroll_run (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  period_id          uuid NOT NULL REFERENCES payroll_period(id),
  run_type           text NOT NULL DEFAULT 'regular' CHECK (run_type IN ('regular', 'off_cycle', 'arrears', 'supplementary', 'final_settlement', 'bonus')),
  status             text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'preflight_failed', 'computed', 'reviewed', 'approved', 'locked', 'disbursed', 'filed')),
  rounding_policy    text NOT NULL DEFAULT 'half_up_rupee',
  references_run_id  uuid REFERENCES payroll_run(id),
  preflight          jsonb,
  variance           jsonb,
  maker_id           uuid REFERENCES app_user(id),
  checker_id         uuid REFERENCES app_user(id),
  approval_signature text,
  computed_at        timestamptz,
  reviewed_at        timestamptz,
  approved_at        timestamptz,
  locked_at          timestamptz,
  disbursed_at       timestamptz,
  filed_at           timestamptz,
  notes              text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payroll_run_period_idx ON payroll_run (period_id, created_at DESC);

CREATE TABLE payroll_input (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  run_id         uuid NOT NULL REFERENCES payroll_run(id),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  component_code text NOT NULL,
  amount         numeric(14,2) NOT NULL,
  note           text,
  created_by     uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payslip (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  run_id           uuid NOT NULL REFERENCES payroll_run(id),
  employee_id      uuid NOT NULL REFERENCES employee(id),
  inputs_snapshot  jsonb NOT NULL,
  rule_version_ids uuid[] NOT NULL DEFAULT '{}',
  bs_days_in_month smallint NOT NULL,
  paid_days        numeric(6,2) NOT NULL,
  lop_days         numeric(6,2) NOT NULL DEFAULT 0,
  ot_hours         numeric(8,2) NOT NULL DEFAULT 0,
  gross            numeric(14,2) NOT NULL,
  taxable          numeric(14,2) NOT NULL,
  tds              numeric(14,2) NOT NULL,
  employee_statutory numeric(14,2) NOT NULL DEFAULT 0,   -- SSF/PF employee share
  employer_statutory numeric(14,2) NOT NULL DEFAULT 0,   -- SSF/PF+gratuity employer share
  total_deductions numeric(14,2) NOT NULL,
  net              numeric(14,2) NOT NULL,
  ytd              jsonb NOT NULL DEFAULT '{}',
  explain          jsonb NOT NULL DEFAULT '[]',
  flags            jsonb NOT NULL DEFAULT '[]',
  published_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, employee_id)
);

CREATE TABLE payslip_line (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  payslip_id     uuid NOT NULL REFERENCES payslip(id),
  component_code text NOT NULL,
  type           text NOT NULL CHECK (type IN ('earning', 'deduction', 'employer_contribution', 'statistical')),
  name_en        text NOT NULL,
  name_ne        text,
  amount         numeric(14,2) NOT NULL,
  basis          jsonb,
  sort_order     integer NOT NULL DEFAULT 0
);
CREATE INDEX payslip_line_payslip_idx ON payslip_line (payslip_id);

CREATE TABLE loan_installment (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  loan_id    uuid NOT NULL REFERENCES loan(id),
  payslip_id uuid NOT NULL REFERENCES payslip(id),
  amount     numeric(14,2) NOT NULL,
  UNIQUE (loan_id, payslip_id)
);

CREATE TABLE bank_file (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  run_id     uuid NOT NULL REFERENCES payroll_run(id),
  bank_code  text NOT NULL,
  format     text NOT NULL,
  filename   text NOT NULL,
  content    text NOT NULL,
  checksum   text NOT NULL,
  created_by uuid REFERENCES app_user(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Locked runs are immutable (plan §9.1 decision 4): block changes to the run's payslips and lines.
CREATE FUNCTION payslip_locked_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_locked timestamptz; pid uuid;
BEGIN
  pid := CASE WHEN TG_TABLE_NAME = 'payslip_line' THEN coalesce(OLD.payslip_id, NEW.payslip_id) ELSE NULL END;
  IF TG_TABLE_NAME = 'payslip' THEN
    SELECT r.locked_at INTO run_locked FROM payroll_run r WHERE r.id = coalesce(OLD.run_id, NEW.run_id);
  ELSE
    SELECT r.locked_at INTO run_locked FROM payslip p JOIN payroll_run r ON r.id = p.run_id WHERE p.id = pid;
  END IF;
  IF run_locked IS NOT NULL AND NOT (TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'payslip' AND OLD.published_at IS DISTINCT FROM NEW.published_at
       AND row(OLD.*) IS DISTINCT FROM row(NEW.*) AND OLD.net = NEW.net AND OLD.gross = NEW.gross AND OLD.tds = NEW.tds) THEN
    RAISE EXCEPTION 'payroll run is locked; % on % is not allowed (create a supplementary run)', TG_OP, TG_TABLE_NAME;
  END IF;
  RETURN coalesce(NEW, OLD);
END $$;
CREATE TRIGGER payslip_locked_trg BEFORE UPDATE OR DELETE ON payslip FOR EACH ROW EXECUTE FUNCTION payslip_locked_guard();
CREATE TRIGGER payslip_line_locked_trg BEFORE UPDATE OR DELETE ON payslip_line FOR EACH ROW EXECUTE FUNCTION payslip_locked_guard();

CREATE FUNCTION payroll_run_locked_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND OLD.locked_at IS NOT NULL THEN RAISE EXCEPTION 'locked payroll run cannot be deleted'; END IF;
  IF TG_OP = 'UPDATE' AND OLD.locked_at IS NOT NULL THEN
    -- Only forward lifecycle fields may change after lock.
    IF NEW.status NOT IN ('locked', 'disbursed', 'filed') OR NEW.locked_at IS DISTINCT FROM OLD.locked_at
       OR NEW.period_id <> OLD.period_id OR NEW.approval_signature IS DISTINCT FROM OLD.approval_signature THEN
      RAISE EXCEPTION 'locked payroll run is immutable';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER payroll_run_locked_trg BEFORE UPDATE OR DELETE ON payroll_run FOR EACH ROW EXECUTE FUNCTION payroll_run_locked_guard();

-- ---------------------------------------------------------------- attendance lock and OT link
ALTER TABLE attendance_day ADD COLUMN ot_slip_id uuid REFERENCES ot_slip(id);

-- Latest attendance version per employee/day (reports, payroll).
CREATE VIEW v_attendance_latest AS
  SELECT DISTINCT ON (employee_id, local_date) *
    FROM attendance_day ORDER BY employee_id, local_date, version DESC;

-- ---------------------------------------------------------------- punch partitions through 2030
DO $$
DECLARE d date := '2028-01-01';
BEGIN
  WHILE d < '2031-01-01' LOOP
    EXECUTE format('CREATE TABLE punch_event_%s PARTITION OF punch_event FOR VALUES FROM (%L) TO (%L)',
                   to_char(d, 'YYYY_MM'), d, d + interval '1 month');
    d := d + interval '1 month';
  END LOOP;
END $$;
