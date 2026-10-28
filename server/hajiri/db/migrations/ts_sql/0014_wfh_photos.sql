-- Photo taken when someone clocks in from home. The image is encrypted with the field key and bound to the employee
-- and punch; it is wiped after the company's retention period (attendance.policy.wfhPhotoRetentionDays) and the row
-- is kept as a record that a photo existed.
CREATE TABLE IF NOT EXISTS punch_photo (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  employee_id uuid NOT NULL REFERENCES employee(id),
  punch_id    uuid NOT NULL,
  punched_at  timestamptz NOT NULL,
  local_date  date NOT NULL,
  direction   text NOT NULL,
  mime        text NOT NULL CHECK (mime IN ('image/jpeg', 'image/png', 'image/webp')),
  bytes       integer NOT NULL CHECK (bytes > 0),
  image_enc   bytea,
  purged_at   timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS punch_photo_employee_idx ON punch_photo (employee_id, local_date);
CREATE INDEX IF NOT EXISTS punch_photo_company_idx ON punch_photo (company_id, local_date);
CREATE UNIQUE INDEX IF NOT EXISTS punch_photo_punch_idx ON punch_photo (punch_id);

UPDATE company_setting SET value = value || '{"wfhPhotoRequired": true, "wfhPhotoRetentionDays": 90}'::jsonb
 WHERE key = 'attendance.policy' AND NOT value ? 'wfhPhotoRequired';
