-- Shared encrypted file store for documents, receipts, CVs and attachments. Each row is encrypted with the field key
-- and bound to its company and id. A file is uploaded first (owned by the uploader, purpose recorded) and then
-- attached by the module that uses it; unattached uploads are removed by the nightly worker after a day.
CREATE TABLE IF NOT EXISTS stored_file (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  purpose      text NOT NULL,
  filename     text NOT NULL CHECK (length(filename) BETWEEN 1 AND 200),
  mime         text NOT NULL,
  bytes        integer NOT NULL CHECK (bytes > 0),
  sha256       text NOT NULL,
  data_enc     bytea,
  uploaded_by  uuid REFERENCES app_user(id),
  attached_to  text,
  attached_at  timestamptz,
  deleted_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stored_file_company_idx ON stored_file (company_id, created_at);
CREATE INDEX IF NOT EXISTS stored_file_unattached_idx ON stored_file (created_at) WHERE attached_to IS NULL AND deleted_at IS NULL;
