-- Documents & letters: employee documents with verification, versions and expiry alerts; letter templates, issued
-- letters (immutable snapshots with a yearly reference sequence) and employee letter requests; company policies with
-- acknowledgements. File bytes live encrypted in stored_file (0015).

CREATE TABLE IF NOT EXISTS document_category (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  code                text NOT NULL CHECK (code ~ '^[a-z0-9_]{2,40}$'),
  name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  employee_can_upload boolean NOT NULL DEFAULT true,
  expiry_required     boolean NOT NULL DEFAULT false,
  visible_to_employee boolean NOT NULL DEFAULT true,
  sort_order          smallint NOT NULL DEFAULT 0,
  active              boolean NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

CREATE TABLE IF NOT EXISTS employee_document (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  employee_id         uuid NOT NULL REFERENCES employee(id),
  category_id         uuid NOT NULL REFERENCES document_category(id),
  title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  file_id             uuid NOT NULL REFERENCES stored_file(id),
  version             integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  replaces_id         uuid REFERENCES employee_document(id),
  is_current          boolean NOT NULL DEFAULT true,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'rejected')),
  reject_reason       text,
  expiry_date         date,
  visible_to_employee boolean NOT NULL DEFAULT true,
  notes               text,
  uploaded_by         uuid REFERENCES app_user(id),
  verified_by         uuid REFERENCES app_user(id),
  verified_at         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  deleted_by          uuid REFERENCES app_user(id)
);
CREATE INDEX IF NOT EXISTS employee_document_emp_idx ON employee_document (employee_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS employee_document_expiry_idx ON employee_document (company_id, expiry_date) WHERE deleted_at IS NULL AND is_current AND expiry_date IS NOT NULL;

-- One row per document and threshold (30, 7, 0 days) so an expiry alert is sent once.
CREATE TABLE IF NOT EXISTS document_expiry_alert (
  document_id uuid NOT NULL REFERENCES employee_document(id),
  threshold   smallint NOT NULL,
  sent_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, threshold)
);

CREATE TABLE IF NOT EXISTS letter_template (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  code                text NOT NULL CHECK (code ~ '^[a-z0-9_]{2,40}$'),
  name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  subject             text NOT NULL CHECK (length(subject) BETWEEN 1 AND 160),
  body                text NOT NULL CHECK (length(body) BETWEEN 1 AND 20000),
  requestable         boolean NOT NULL DEFAULT false,
  visible_to_employee boolean NOT NULL DEFAULT true,
  is_builtin          boolean NOT NULL DEFAULT false,
  active              boolean NOT NULL DEFAULT true,
  updated_by          uuid REFERENCES app_user(id),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

CREATE TABLE IF NOT EXISTS letter_sequence (
  company_id uuid NOT NULL REFERENCES company(id),
  bs_year    integer NOT NULL,
  last_no    integer NOT NULL DEFAULT 0,
  PRIMARY KEY (company_id, bs_year)
);

CREATE TABLE IF NOT EXISTS letter_request (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  template_id          uuid NOT NULL REFERENCES letter_template(id),
  purpose              text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 300),
  addressed_to         text CHECK (length(addressed_to) <= 200),
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'issued', 'cancelled')),
  decision_reason      text,
  workflow_instance_id uuid,
  issued_letter_id     uuid,
  created_by           uuid REFERENCES app_user(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  decided_at           timestamptz
);
CREATE INDEX IF NOT EXISTS letter_request_company_idx ON letter_request (company_id, status, created_at);

CREATE TABLE IF NOT EXISTS issued_letter (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id          uuid NOT NULL REFERENCES company(id),
  employee_id         uuid NOT NULL REFERENCES employee(id),
  template_id         uuid REFERENCES letter_template(id),
  template_code       text NOT NULL,
  ref_no              text NOT NULL,
  subject             text NOT NULL,
  html                text NOT NULL,
  visible_to_employee boolean NOT NULL DEFAULT true,
  request_id          uuid REFERENCES letter_request(id),
  issued_by           uuid REFERENCES app_user(id),
  issued_at           timestamptz NOT NULL DEFAULT now(),
  voided_at           timestamptz,
  voided_by           uuid REFERENCES app_user(id),
  void_reason         text,
  UNIQUE (company_id, ref_no)
);
CREATE INDEX IF NOT EXISTS issued_letter_emp_idx ON issued_letter (employee_id, issued_at);

-- An issued letter is a record: its reference, subject and text never change. Only voiding (and visibility) may.
CREATE OR REPLACE FUNCTION issued_letter_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.ref_no IS DISTINCT FROM OLD.ref_no OR NEW.subject IS DISTINCT FROM OLD.subject OR NEW.html IS DISTINCT FROM OLD.html
     OR NEW.employee_id IS DISTINCT FROM OLD.employee_id OR NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
    RAISE EXCEPTION 'an issued letter cannot be changed; void it and issue a new one';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS issued_letter_immutable ON issued_letter;
CREATE TRIGGER issued_letter_immutable BEFORE UPDATE ON issued_letter FOR EACH ROW EXECUTE FUNCTION issued_letter_immutable();
CREATE OR REPLACE FUNCTION issued_letter_no_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'an issued letter cannot be deleted; void it instead'; END $$;
DROP TRIGGER IF EXISTS issued_letter_no_delete ON issued_letter;
CREATE TRIGGER issued_letter_no_delete BEFORE DELETE ON issued_letter FOR EACH ROW EXECUTE FUNCTION issued_letter_no_delete();

CREATE TABLE IF NOT EXISTS company_policy (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  title         text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  description   text CHECK (length(description) <= 2000),
  file_id       uuid NOT NULL REFERENCES stored_file(id),
  audience      text NOT NULL DEFAULT 'all' CHECK (audience IN ('all', 'branch', 'department')),
  audience_ids  uuid[] NOT NULL DEFAULT '{}',
  requires_ack  boolean NOT NULL DEFAULT true,
  published_at  timestamptz NOT NULL DEFAULT now(),
  archived_at   timestamptz,
  created_by    uuid REFERENCES app_user(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_ack (
  policy_id       uuid NOT NULL REFERENCES company_policy(id),
  employee_id     uuid NOT NULL REFERENCES employee(id),
  acknowledged_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (policy_id, employee_id)
);

-- Last reminder sent to an employee who has not acknowledged a policy (weekly nudge).
CREATE TABLE IF NOT EXISTS policy_nudge (
  policy_id    uuid NOT NULL REFERENCES company_policy(id),
  employee_id  uuid NOT NULL REFERENCES employee(id),
  last_sent_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (policy_id, employee_id)
);

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'letter_request', '[{"role": "hr"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;
