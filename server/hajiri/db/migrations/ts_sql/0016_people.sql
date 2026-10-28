-- People and core HR: org structure (unit heads, designations), the full employee master (personal, contact,
-- addresses, statutory IDs, emergency contacts, family and nominees, education, previous jobs, skills), effective-dated
-- career events, employee profile change requests (approved by HR) and field-level change history.
-- ID numbers (citizenship, passport, driving licence) are encrypted with the field key like PAN and bank accounts.

ALTER TABLE org_unit ADD COLUMN IF NOT EXISTS head_employee_id uuid REFERENCES employee(id);
ALTER TABLE org_unit ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE org_unit ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE branch ADD COLUMN IF NOT EXISTS address text;
ALTER TABLE branch ADD COLUMN IF NOT EXISTS phone text;
ALTER TABLE branch ADD COLUMN IF NOT EXISTS head_employee_id uuid REFERENCES employee(id);

CREATE TABLE IF NOT EXISTS designation (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  code        text NOT NULL CHECK (length(code) BETWEEN 1 AND 20),
  name_en     text NOT NULL CHECK (length(name_en) BETWEEN 1 AND 120),
  grade       text,
  job_level   smallint CHECK (job_level BETWEEN 1 AND 20),
  org_unit_id uuid REFERENCES org_unit(id),
  description text,
  active      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

ALTER TABLE employee
  ADD COLUMN IF NOT EXISTS designation_id        uuid REFERENCES designation(id),
  ADD COLUMN IF NOT EXISTS blood_group           text CHECK (blood_group IN ('A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-')),
  ADD COLUMN IF NOT EXISTS nationality           text,
  ADD COLUMN IF NOT EXISTS personal_email        text,
  ADD COLUMN IF NOT EXISTS personal_phone        text,
  ADD COLUMN IF NOT EXISTS work_phone            text,
  ADD COLUMN IF NOT EXISTS permanent_address     jsonb,
  ADD COLUMN IF NOT EXISTS temporary_address     jsonb,
  ADD COLUMN IF NOT EXISTS skills                text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS languages             text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS citizenship_no_enc    bytea,
  ADD COLUMN IF NOT EXISTS citizenship_district  text,
  ADD COLUMN IF NOT EXISTS citizenship_issued_on date,
  ADD COLUMN IF NOT EXISTS passport_no_enc       bytea,
  ADD COLUMN IF NOT EXISTS passport_expiry       date,
  ADD COLUMN IF NOT EXISTS licence_no_enc        bytea,
  ADD COLUMN IF NOT EXISTS licence_expiry        date,
  ADD COLUMN IF NOT EXISTS confirmed_on          date,
  ADD COLUMN IF NOT EXISTS avatar_file_id        uuid REFERENCES stored_file(id);

CREATE TABLE IF NOT EXISTS employee_emergency_contact (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id uuid NOT NULL REFERENCES employee(id),
  name        text NOT NULL,
  relation    text NOT NULL,
  phone       text NOT NULL,
  alt_phone   text,
  address     text,
  sort        smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS employee_emergency_contact_emp_idx ON employee_emergency_contact (employee_id);

CREATE TABLE IF NOT EXISTS employee_family (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id   uuid NOT NULL REFERENCES employee(id),
  name          text NOT NULL,
  relation      text NOT NULL,
  dob           date,
  occupation    text,
  phone         text,
  is_dependent  boolean NOT NULL DEFAULT false,
  is_nominee    boolean NOT NULL DEFAULT false,
  nominee_share numeric(5,2) CHECK (nominee_share IS NULL OR (nominee_share > 0 AND nominee_share <= 100)),
  sort          smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS employee_family_emp_idx ON employee_family (employee_id);

CREATE TABLE IF NOT EXISTS employee_education (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id uuid NOT NULL REFERENCES employee(id),
  level       text NOT NULL,
  degree      text,
  field       text,
  institution text NOT NULL,
  board       text,
  start_year  smallint,
  end_year    smallint,
  result      text,
  sort        smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS employee_education_emp_idx ON employee_education (employee_id);

CREATE TABLE IF NOT EXISTS employee_experience (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id        uuid NOT NULL REFERENCES employee(id),
  organisation       text NOT NULL,
  designation        text,
  from_date          date,
  to_date            date,
  responsibilities   text,
  reason_for_leaving text,
  sort               smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS employee_experience_emp_idx ON employee_experience (employee_id);

-- Promotions, transfers, confirmations, separations... An event dated today or earlier is applied when recorded;
-- a future event stays 'scheduled' until the daily job applies it on its effective date.
CREATE TABLE IF NOT EXISTS career_event (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  kind           text NOT NULL CHECK (kind IN ('hire', 'confirmation', 'promotion', 'transfer', 'designation_change', 'grade_change',
                                              'manager_change', 'employment_type_change', 'salary_revision', 'separation', 'rehire', 'note')),
  effective_date date NOT NULL,
  details        jsonb NOT NULL DEFAULT '{}',
  before         jsonb,
  note           text,
  status         text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'applied', 'cancelled')),
  applied_at     timestamptz,
  created_by     uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS career_event_emp_idx ON career_event (employee_id, effective_date);
CREATE INDEX IF NOT EXISTS career_event_due_idx ON career_event (effective_date) WHERE status = 'scheduled';

-- A proposed change to one section of a profile. The proposed values are encrypted (they can hold bank and ID
-- numbers); `summary` is a masked copy for lists and the approval inbox.
CREATE TABLE IF NOT EXISTS profile_change_request (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  employee_id     uuid NOT NULL REFERENCES employee(id),
  requested_by    uuid NOT NULL REFERENCES app_user(id),
  section         text NOT NULL,
  payload_enc     bytea NOT NULL,
  summary         jsonb NOT NULL DEFAULT '{}',
  reason          text,
  proof_file_id   uuid REFERENCES stored_file(id),
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  decided_by      uuid REFERENCES app_user(id),
  decided_at      timestamptz,
  decision_reason text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS profile_change_request_emp_idx ON profile_change_request (employee_id, created_at DESC);
CREATE INDEX IF NOT EXISTS profile_change_request_pending_idx ON profile_change_request (company_id) WHERE status = 'pending';

-- Field-level history: who changed what, from which value to which. Sensitive values are stored masked.
CREATE TABLE IF NOT EXISTS employee_field_history (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id  uuid NOT NULL REFERENCES company(id),
  employee_id uuid NOT NULL REFERENCES employee(id),
  field       text NOT NULL,
  old_value   text,
  new_value   text,
  source      text NOT NULL CHECK (source IN ('hr_edit', 'change_request', 'career_event', 'import', 'system')),
  ref_id      uuid,
  changed_by  uuid REFERENCES app_user(id),
  changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS employee_field_history_emp_idx ON employee_field_history (employee_id, changed_at DESC);

-- Every existing employee gets a hire event from their join date, so the timeline starts somewhere.
INSERT INTO career_event (company_id, employee_id, kind, effective_date, details, status, applied_at)
SELECT e.company_id, e.id, 'hire', coalesce(e.join_date, e.created_at::date),
       jsonb_build_object('designation', e.designation, 'grade', e.grade), 'applied', now()
  FROM employee e
 WHERE NOT EXISTS (SELECT 1 FROM career_event c WHERE c.employee_id = e.id AND c.kind = 'hire');

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'profile_change', '[{"role": "hr"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;

INSERT INTO company_setting (company_id, key, value)
SELECT id, 'people.policy', '{"makerChecker": false}'::jsonb FROM company
ON CONFLICT DO NOTHING;
