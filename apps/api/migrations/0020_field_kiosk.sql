-- Field force, kiosks, geofenced locations, breaks and shift swaps.

-- Employees who work in the field (sales, site visits) may record visits away from the office.
ALTER TABLE employee ADD COLUMN IF NOT EXISTS field_work boolean NOT NULL DEFAULT false;

-- Locations gain an address and an active switch; employees can be tied to specific locations.
ALTER TABLE location ADD COLUMN IF NOT EXISTS address text;
ALTER TABLE location ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE location ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS location_assignment (
  location_id uuid NOT NULL REFERENCES location(id) ON DELETE CASCADE,
  employee_id uuid NOT NULL REFERENCES employee(id),
  company_id  uuid NOT NULL REFERENCES company(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (location_id, employee_id)
);
CREATE INDEX IF NOT EXISTS location_assignment_employee_idx ON location_assignment (employee_id);

-- Breaks during the working day. One open break per person; minutes are filled in when the break ends.
CREATE TABLE IF NOT EXISTS work_break (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  employee_id uuid NOT NULL REFERENCES employee(id),
  local_date  date NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('lunch', 'tea', 'prayer', 'personal')),
  paid        boolean NOT NULL,
  max_min     integer NOT NULL CHECK (max_min > 0),
  started_at  timestamptz NOT NULL,
  ended_at    timestamptz,
  minutes     integer CHECK (minutes >= 0),
  over_limit  boolean NOT NULL DEFAULT false,
  source      text NOT NULL DEFAULT 'web' CHECK (source IN ('web', 'kiosk', 'auto')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS work_break_open_idx ON work_break (employee_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS work_break_day_idx ON work_break (company_id, local_date);

-- Field visits: where someone went, why, and when they arrived and left.
CREATE TABLE IF NOT EXISTS field_visit (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id       uuid NOT NULL REFERENCES company(id),
  employee_id      uuid NOT NULL REFERENCES employee(id),
  local_date       date NOT NULL,
  place            text NOT NULL CHECK (length(place) BETWEEN 1 AND 160),
  purpose          text CHECK (length(purpose) <= 500),
  started_at       timestamptz NOT NULL,
  start_lat        numeric(9,6),
  start_lng        numeric(9,6),
  start_accuracy_m integer,
  ended_at         timestamptz,
  end_lat          numeric(9,6),
  end_lng          numeric(9,6),
  end_accuracy_m   integer,
  outcome          text CHECK (length(outcome) <= 1000),
  photo_file_id    uuid REFERENCES stored_file(id),
  punch_id         uuid,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS field_visit_open_idx ON field_visit (employee_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS field_visit_day_idx ON field_visit (company_id, local_date);

-- Shared clock-in tablets. The pairing code and the kiosk key are stored only as hashes.
CREATE TABLE IF NOT EXISTS kiosk (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  branch_id          uuid REFERENCES branch(id),
  location_id        uuid REFERENCES location(id) ON DELETE SET NULL,
  selfie_required    boolean NOT NULL DEFAULT true,
  status             text NOT NULL DEFAULT 'pairing' CHECK (status IN ('pairing', 'active', 'revoked')),
  pairing_code_hash  text,
  pairing_expires_at timestamptz,
  key_hash           text UNIQUE,
  paired_at          timestamptz,
  last_seen_at       timestamptz,
  failed_attempts    integer NOT NULL DEFAULT 0,
  locked_until       timestamptz,
  created_by         uuid REFERENCES app_user(id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  revoked_at         timestamptz
);
CREATE INDEX IF NOT EXISTS kiosk_company_idx ON kiosk (company_id);
CREATE UNIQUE INDEX IF NOT EXISTS kiosk_pairing_idx ON kiosk (pairing_code_hash) WHERE pairing_code_hash IS NOT NULL;

-- Each employee's kiosk PIN (hashed) and the version of their QR badge secret (bumped to invalidate old badges).
CREATE TABLE IF NOT EXISTS kiosk_credential (
  employee_id  uuid PRIMARY KEY REFERENCES employee(id),
  company_id   uuid NOT NULL REFERENCES company(id),
  pin_hash     text,
  failed_count integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  qr_version   integer NOT NULL DEFAULT 1,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- A QR badge code works once: replaying a photographed code is refused.
CREATE TABLE IF NOT EXISTS kiosk_qr_use (
  employee_id uuid NOT NULL REFERENCES employee(id),
  time_window bigint NOT NULL,
  kiosk_id    uuid REFERENCES kiosk(id),
  used_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (employee_id, time_window)
);

-- Shift swaps: the colleague accepts first, then the manager approves through the workflow.
CREATE TABLE IF NOT EXISTS shift_swap (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  requester_id         uuid NOT NULL REFERENCES employee(id),
  colleague_id         uuid NOT NULL REFERENCES employee(id),
  local_date           date NOT NULL,
  requester_shift_id   uuid REFERENCES shift(id),
  colleague_shift_id   uuid REFERENCES shift(id),
  reason               text CHECK (length(reason) <= 500),
  status               text NOT NULL DEFAULT 'awaiting_colleague'
                       CHECK (status IN ('awaiting_colleague', 'pending', 'approved', 'rejected', 'declined', 'cancelled')),
  colleague_note       text CHECK (length(colleague_note) <= 500),
  workflow_instance_id uuid,
  created_at           timestamptz NOT NULL DEFAULT now(),
  decided_at           timestamptz,
  CHECK (requester_id <> colleague_id)
);
CREATE INDEX IF NOT EXISTS shift_swap_company_idx ON shift_swap (company_id, local_date);
CREATE UNIQUE INDEX IF NOT EXISTS shift_swap_open_idx ON shift_swap (requester_id, local_date) WHERE status IN ('awaiting_colleague', 'pending');

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'shift_swap', '[{"role": "manager"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;
