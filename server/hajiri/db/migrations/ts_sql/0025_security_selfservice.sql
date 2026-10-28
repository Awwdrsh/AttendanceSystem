-- Two-step sign-in (TOTP), active sessions, hourly leave and employee tax-savings requests.

-- ---------------------------------------------------------------- two-step sign-in
-- The authenticator secret is encrypted with the field key (AAD totp:<user id>); totp_last_step blocks code replay.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS totp_secret_enc  bytea;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS totp_pending_enc bytea;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS totp_enabled_at  timestamptz;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS totp_last_step   bigint;

CREATE TABLE IF NOT EXISTS user_recovery_code (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id    uuid NOT NULL REFERENCES app_user(id),
  code_hash  text NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_recovery_code_user_idx ON user_recovery_code (user_id) WHERE used_at IS NULL;

-- Password accepted, second step pending: short-lived, single-use, bound to the user.
CREATE TABLE IF NOT EXISTS login_challenge (
  token_hash text PRIMARY KEY,
  user_id    uuid NOT NULL REFERENCES app_user(id),
  expires_at timestamptz NOT NULL,
  attempts   integer NOT NULL DEFAULT 0,
  used_at    timestamptz,
  ip         text,
  user_agent text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_challenge_user_idx ON login_challenge (user_id);

-- Sessions get a public id so people can see and sign out their own sessions without exposing the token hash.
ALTER TABLE session ADD COLUMN IF NOT EXISTS id uuid DEFAULT uuidv7();
UPDATE session SET id = uuidv7() WHERE id IS NULL;
ALTER TABLE session ALTER COLUMN id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS session_id_idx ON session (id);
ALTER TABLE session ADD COLUMN IF NOT EXISTS two_factor boolean NOT NULL DEFAULT false;

-- ---------------------------------------------------------------- hourly (short) leave
-- A short leave covers part of one working day; it is deducted as hours / scheduled working hours of the shift.
ALTER TABLE leave_type ADD COLUMN IF NOT EXISTS allow_hourly boolean NOT NULL DEFAULT false;
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS from_time time;
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS to_time   time;
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS hours     numeric(5,2);
DO $$ BEGIN
  ALTER TABLE leave_request ADD CONSTRAINT leave_request_hourly_chk CHECK (
    (from_time IS NULL AND to_time IS NULL AND hours IS NULL)
    OR (from_time IS NOT NULL AND to_time IS NOT NULL AND to_time > from_time AND hours > 0 AND half_day IS NULL AND from_date = to_date));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- Home and sick leave can be taken by the hour by default (HR can switch it off per leave type).
UPDATE leave_type SET allow_hourly = true WHERE code IN ('HOME', 'SICK') AND paid AND NOT allow_hourly;

-- ---------------------------------------------------------------- tax savings requests
-- What an employee asks HR to accept. Only an approved request changes tax_declaration (as 'verified'), and payroll
-- reads only verified declarations, so nothing unapproved can change anyone's pay.
CREATE TABLE IF NOT EXISTS tax_declaration_request (
  id                      uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id              uuid NOT NULL REFERENCES company(id),
  employee_id             uuid NOT NULL REFERENCES employee(id),
  fiscal_year_start       integer NOT NULL,
  life_premium            numeric(14,2) NOT NULL DEFAULT 0 CHECK (life_premium >= 0),
  health_premium          numeric(14,2) NOT NULL DEFAULT 0 CHECK (health_premium >= 0),
  cit_monthly             numeric(14,2) NOT NULL DEFAULT 0 CHECK (cit_monthly >= 0),
  medical_cost            numeric(14,2) NOT NULL DEFAULT 0 CHECK (medical_cost >= 0),
  other_retirement_annual numeric(14,2) NOT NULL DEFAULT 0 CHECK (other_retirement_annual >= 0),
  note                    text,
  proof_file_ids          uuid[] NOT NULL DEFAULT '{}',
  status                  text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  workflow_instance_id    uuid,
  created_by              uuid REFERENCES app_user(id),
  created_at              timestamptz NOT NULL DEFAULT now(),
  decided_by              uuid REFERENCES app_user(id),
  decided_at              timestamptz,
  decision_note           text
);
CREATE INDEX IF NOT EXISTS tax_declaration_request_emp_idx ON tax_declaration_request (employee_id, fiscal_year_start, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS tax_declaration_request_one_pending ON tax_declaration_request (employee_id, fiscal_year_start) WHERE status = 'pending';

-- Declarations already submitted but never verified become pending requests, so HR still sees and decides them.
INSERT INTO tax_declaration_request (company_id, employee_id, fiscal_year_start, life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, note)
SELECT e.company_id, d.employee_id, d.fiscal_year_start, d.life_premium, d.health_premium, d.cit_monthly, d.medical_cost, d.other_retirement_annual,
       'Submitted before the tax savings page existed'
  FROM tax_declaration d JOIN employee e ON e.id = d.employee_id
 WHERE d.status = 'submitted'
   AND NOT EXISTS (SELECT 1 FROM tax_declaration_request r WHERE r.employee_id = d.employee_id AND r.fiscal_year_start = d.fiscal_year_start AND r.status = 'pending');

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'tax_declaration', '[{"role": "hr"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;
