-- Hiring and applicant tracking: requisitions approved by HR, job openings published to the public careers page,
-- candidates and their applications moving through stages, interviews with scorecards, offers, and the hire that turns
-- a candidate into an employee. Rejected candidates' personal data is purged after hiring.policy.retentionDays.

CREATE TABLE IF NOT EXISTS hiring_requisition (
  id                    uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id            uuid NOT NULL REFERENCES company(id),
  raised_by             uuid NOT NULL REFERENCES app_user(id),
  raised_by_employee_id uuid REFERENCES employee(id),
  title                 text NOT NULL CHECK (length(title) BETWEEN 2 AND 120),
  designation           text,
  org_unit_id           uuid REFERENCES org_unit(id),
  branch_id             uuid REFERENCES branch(id),
  headcount             smallint NOT NULL DEFAULT 1 CHECK (headcount BETWEEN 1 AND 500),
  filled                smallint NOT NULL DEFAULT 0 CHECK (filled >= 0),
  salary_min            numeric(12,2) CHECK (salary_min >= 0),
  salary_max            numeric(12,2) CHECK (salary_max >= 0),
  kind                  text NOT NULL DEFAULT 'new' CHECK (kind IN ('new', 'replacement')),
  replacing_employee_id uuid REFERENCES employee(id),
  justification         text NOT NULL,
  status                text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'filled')),
  decided_at            timestamptz,
  decision_reason       text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min)
);
CREATE INDEX IF NOT EXISTS hiring_requisition_company_idx ON hiring_requisition (company_id, status);

CREATE TABLE IF NOT EXISTS hiring_job (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  requisition_id      uuid REFERENCES hiring_requisition(id),
  title               text NOT NULL CHECK (length(title) BETWEEN 2 AND 120),
  description         text NOT NULL DEFAULT '',
  requirements        text NOT NULL DEFAULT '',
  category            text,
  location            text,
  work_mode           text NOT NULL DEFAULT 'onsite' CHECK (work_mode IN ('onsite', 'remote', 'hybrid')),
  employment_type     text NOT NULL DEFAULT 'full_time' CHECK (employment_type IN ('full_time', 'part_time', 'contract', 'internship', 'temporary')),
  experience_min      numeric(4,1) CHECK (experience_min >= 0),
  experience_max      numeric(4,1) CHECK (experience_max >= 0),
  salary_min          numeric(12,2) CHECK (salary_min >= 0),
  salary_max          numeric(12,2) CHECK (salary_max >= 0),
  show_salary         boolean NOT NULL DEFAULT false,
  openings            smallint NOT NULL DEFAULT 1 CHECK (openings BETWEEN 1 AND 500),
  deadline            date,
  org_unit_id         uuid REFERENCES org_unit(id),
  branch_id           uuid REFERENCES branch(id),
  hiring_manager_id   uuid REFERENCES employee(id),
  stages              jsonb NOT NULL DEFAULT '["Applied","Screening","Interview","Offer","Hired"]',
  questions           jsonb NOT NULL DEFAULT '[]',
  status              text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed')),
  published_at        timestamptz,
  closed_at           timestamptz,
  created_by          uuid REFERENCES app_user(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (experience_min IS NULL OR experience_max IS NULL OR experience_max >= experience_min),
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min)
);
CREATE INDEX IF NOT EXISTS hiring_job_company_idx ON hiring_job (company_id, status);

-- One person per company, matched by e-mail so repeat applicants show up as duplicates.
CREATE TABLE IF NOT EXISTS hiring_candidate (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  name        text NOT NULL,
  email       text,
  phone       text,
  purged_at   timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS hiring_candidate_email_idx ON hiring_candidate (company_id, lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS hiring_application (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid NOT NULL REFERENCES company(id),
  job_id            uuid NOT NULL REFERENCES hiring_job(id),
  candidate_id      uuid NOT NULL REFERENCES hiring_candidate(id),
  stage             text NOT NULL DEFAULT 'Applied',
  furthest_stage    smallint NOT NULL DEFAULT 0,
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'rejected', 'withdrawn', 'hired')),
  source            text NOT NULL DEFAULT 'careers' CHECK (source IN ('careers', 'referral', 'manual', 'job_board', 'other')),
  cv_file_id        uuid REFERENCES stored_file(id),
  cover_note        text,
  answers           jsonb NOT NULL DEFAULT '{}',
  experience_years  numeric(4,1),
  current_employer  text,
  score             smallint,
  score_detail      jsonb NOT NULL DEFAULT '[]',
  knocked_out       boolean NOT NULL DEFAULT false,
  rating            smallint CHECK (rating BETWEEN 1 AND 5),
  tags              text[] NOT NULL DEFAULT '{}',
  owner_user_id     uuid REFERENCES app_user(id),
  rejected_reason   text,
  closed_at         timestamptz,
  hired_employee_id uuid REFERENCES employee(id),
  consent_at        timestamptz,
  stage_changed_at  timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS hiring_application_job_idx ON hiring_application (job_id, status);
CREATE INDEX IF NOT EXISTS hiring_application_company_idx ON hiring_application (company_id, created_at);

CREATE TABLE IF NOT EXISTS hiring_event (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  application_id  uuid NOT NULL REFERENCES hiring_application(id),
  actor_id        uuid REFERENCES app_user(id),
  kind            text NOT NULL,
  body            text,
  data            jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hiring_event_app_idx ON hiring_event (application_id, created_at);

CREATE TABLE IF NOT EXISTS hiring_interview (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  application_id  uuid NOT NULL REFERENCES hiring_application(id),
  title           text NOT NULL DEFAULT 'Interview',
  starts_at       timestamptz NOT NULL,
  duration_min    smallint NOT NULL DEFAULT 45 CHECK (duration_min BETWEEN 10 AND 480),
  mode            text NOT NULL CHECK (mode IN ('in_person', 'phone', 'video')),
  location        text,
  panel           uuid[] NOT NULL CHECK (cardinality(panel) BETWEEN 1 AND 10),
  status          text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'completed', 'cancelled')),
  created_by      uuid REFERENCES app_user(id),
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hiring_interview_app_idx ON hiring_interview (application_id);
CREATE INDEX IF NOT EXISTS hiring_interview_company_idx ON hiring_interview (company_id, starts_at);

CREATE TABLE IF NOT EXISTS hiring_scorecard (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  interview_id    uuid NOT NULL REFERENCES hiring_interview(id),
  reviewer_id     uuid NOT NULL REFERENCES app_user(id),
  ratings         jsonb NOT NULL DEFAULT '{}',
  recommendation  text NOT NULL CHECK (recommendation IN ('strong_no', 'no', 'yes', 'strong_yes')),
  notes           text,
  submitted_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (interview_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS hiring_offer (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id             uuid NOT NULL REFERENCES company(id),
  application_id         uuid NOT NULL REFERENCES hiring_application(id),
  designation            text NOT NULL,
  salary                 numeric(12,2) NOT NULL CHECK (salary > 0),
  join_date              date NOT NULL,
  expires_on             date,
  notes                  text,
  status                 text NOT NULL DEFAULT 'pending_approval'
                         CHECK (status IN ('pending_approval', 'approved', 'rejected', 'accepted', 'declined', 'withdrawn')),
  created_by             uuid REFERENCES app_user(id),
  created_by_employee_id uuid REFERENCES employee(id),
  decided_at             timestamptz,
  responded_at           timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hiring_offer_app_idx ON hiring_offer (application_id);

-- Candidates are not users: e-mails to them carry the address on the outbox row itself.
ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS to_email text;

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT c.id, v.t, '[{"role":"hr"}]'::jsonb FROM company c CROSS JOIN (VALUES ('job_requisition'), ('job_offer')) AS v(t)
ON CONFLICT (company_id, object_type) DO NOTHING;

INSERT INTO company_setting (company_id, key, value)
SELECT c.id, 'hiring.policy', '{"retentionDays": 180, "careersEnabled": true}'::jsonb FROM company c
ON CONFLICT DO NOTHING;
