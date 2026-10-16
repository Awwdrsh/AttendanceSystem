-- Saved ad-hoc reports (Reports → Build a report). The definition is a dataset, columns, filters and a sort chosen from
-- the server's whitelist; it is validated again every time it runs. Shared reports are visible to every HR user of the
-- company; only the author changes or deletes one. Feature switches live in company_setting under the key 'features'.
CREATE TABLE IF NOT EXISTS saved_report (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  description text,
  shared      boolean NOT NULL DEFAULT false,
  definition  jsonb NOT NULL,
  created_by  uuid REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS saved_report_company_idx ON saved_report (company_id, created_by);
