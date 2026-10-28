-- Hajiri Phase 1 schema (plan.md §9). PostgreSQL 18: uuidv7(), WITHOUT OVERLAPS, range partitioning.
-- Conventions: all instants timestamptz (UTC); local_date/bs_date stored explicitly (NPT = UTC+05:45, no DST);
-- company_id/branch_id on every business table from day one (RLS policies come with the auth module).

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------- organisation
CREATE TABLE company (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  code       text NOT NULL UNIQUE,
  name_en    text NOT NULL,
  name_ne    text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE branch (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  code          text NOT NULL,
  name_en       text NOT NULL,
  name_ne       text,
  tz_offset_min smallint NOT NULL DEFAULT 345,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

CREATE TABLE org_unit (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  branch_id  uuid REFERENCES branch(id),
  parent_id  uuid REFERENCES org_unit(id),
  kind       text NOT NULL CHECK (kind IN ('department', 'team')),
  code       text NOT NULL,
  name_en    text NOT NULL,
  name_ne    text,
  UNIQUE (company_id, code)
);

-- ---------------------------------------------------------------- employee master
CREATE TABLE employee (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  emp_code        text NOT NULL,
  name_en         text NOT NULL,
  name_ne         text,
  gender          text CHECK (gender IN ('male', 'female', 'other')),
  dob             date,
  ot_eligible     boolean NOT NULL DEFAULT true,
  -- ISO weekday numbers with Sunday = 0; Saturday (6) is the statutory default weekly off.
  weekly_off_days smallint[] NOT NULL DEFAULT '{6}',
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'exited')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, emp_code)
);

-- Effective-dated assignment: one row per employee per non-overlapping period (plan §9.1 decision 2).
CREATE TABLE employee_assignment (
  employee_id     uuid NOT NULL REFERENCES employee(id),
  valid           daterange NOT NULL,
  org_unit_id     uuid REFERENCES org_unit(id),
  branch_id       uuid REFERENCES branch(id),
  manager_id      uuid REFERENCES employee(id),
  employment_type text NOT NULL CHECK (employment_type IN ('regular', 'work_based', 'time_based', 'casual', 'part_time')),
  probation_end   date,
  PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS)
);

-- ---------------------------------------------------------------- shifts
CREATE TABLE shift (
  id                    uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id            uuid NOT NULL REFERENCES company(id),
  code                  text NOT NULL,
  name_en               text NOT NULL,
  name_ne               text,
  start_time            time NOT NULL,
  end_time              time NOT NULL,                      -- end <= start means the shift crosses midnight
  grace_in_min          smallint NOT NULL DEFAULT 0,
  grace_out_min         smallint NOT NULL DEFAULT 0,
  early_in_window_min   smallint NOT NULL DEFAULT 60,
  late_out_window_min   smallint NOT NULL DEFAULT 120,
  break_minutes         smallint NOT NULL DEFAULT 0,
  half_day_threshold_min smallint NOT NULL DEFAULT 240,
  absent_threshold_min  smallint NOT NULL DEFAULT 120,
  pairing_mode          text NOT NULL DEFAULT 'alternating' CHECK (pairing_mode IN ('alternating', 'strict_log_type', 'first_last')),
  rounding_min          smallint NOT NULL DEFAULT 0,
  rounding_direction    text NOT NULL DEFAULT 'nearest' CHECK (rounding_direction IN ('nearest', 'down', 'up')),
  UNIQUE (company_id, code)
);

CREATE TABLE shift_assignment (
  employee_id uuid NOT NULL REFERENCES employee(id),
  shift_id    uuid NOT NULL REFERENCES shift(id),
  valid       daterange NOT NULL,
  PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS)
);

-- Per-day roster overrides the shift assignment (plan §10.1: roster row > shift_assignment > none).
CREATE TABLE roster (
  employee_id uuid NOT NULL REFERENCES employee(id),
  local_date  date NOT NULL,
  shift_id    uuid REFERENCES shift(id),                     -- NULL = explicitly off that day
  PRIMARY KEY (employee_id, local_date)
);

-- ---------------------------------------------------------------- calendars
CREATE TABLE bs_calendar (
  bs_year  smallint NOT NULL,
  bs_month smallint NOT NULL CHECK (bs_month BETWEEN 1 AND 12),
  bs_day   smallint NOT NULL CHECK (bs_day BETWEEN 1 AND 32),
  bs_date  text NOT NULL,
  ad_date  date NOT NULL UNIQUE,
  PRIMARY KEY (bs_year, bs_month, bs_day)
);

CREATE TABLE holiday (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  branch_id    uuid REFERENCES branch(id),                   -- NULL = all branches
  local_date   date NOT NULL,
  bs_date      text,
  name_en      text NOT NULL,
  name_ne      text,
  gender_scope text NOT NULL DEFAULT 'all' CHECK (gender_scope IN ('all', 'female')),
  optional     boolean NOT NULL DEFAULT false,
  UNIQUE (company_id, branch_id, local_date, name_en)
);

-- ---------------------------------------------------------------- devices
CREATE TABLE device (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid REFERENCES company(id),             -- NULL until an admin claims a pending device
  branch_id         uuid REFERENCES branch(id),
  serial_no         text NOT NULL UNIQUE,
  vendor            text NOT NULL CHECK (vendor IN ('zkteco', 'hikvision', 'suprema', 'anviz', 'mobile', 'kiosk', 'manual')),
  protocol          text NOT NULL CHECK (protocol IN ('adms', 'isapi', 'gsdk', 'crosschex', 'app', 'manual')),
  name              text,
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'disabled')),
  comm_key_hash     text,
  fw_version        text,
  tz_offset_min     smallint NOT NULL DEFAULT 345,
  push_version      text,
  attlog_stamp      text NOT NULL DEFAULT '0',
  oplog_stamp       text NOT NULL DEFAULT '0',
  last_handshake_at timestamptz,
  last_seen_at      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE device_user_map (
  device_id       uuid NOT NULL REFERENCES device(id),
  device_user_pin text NOT NULL,
  employee_id     uuid NOT NULL REFERENCES employee(id),
  valid           daterange NOT NULL,
  PRIMARY KEY (device_id, device_user_pin, valid WITHOUT OVERLAPS)
);

CREATE TABLE device_command (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id   uuid NOT NULL REFERENCES device(id),
  command     text NOT NULL,
  status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'acked', 'failed')),
  return_code integer,
  queued_at   timestamptz NOT NULL DEFAULT now(),
  sent_at     timestamptz,
  acked_at    timestamptz,
  -- The ADMS SHELL command is remote code execution on the terminal; it is never allowed (plan Appendix B).
  CONSTRAINT device_command_no_shell CHECK (command !~* '^\s*SHELL')
);
CREATE INDEX device_command_queue_idx ON device_command (device_id, status) WHERE status = 'queued';

-- ---------------------------------------------------------------- punches (immutable, append-only)
CREATE TABLE punch_event (
  id                uuid NOT NULL DEFAULT uuidv7(),
  device_id         uuid REFERENCES device(id),
  device_sn         text NOT NULL,
  vendor            text NOT NULL,
  device_user_pin   text NOT NULL,
  employee_id       uuid REFERENCES employee(id),            -- NULL = unmapped punch (queue for HR)
  punched_at        timestamptz NOT NULL,
  device_local_time text NOT NULL,                           -- as sent by the device, e.g. '2026-09-21 10:17:03'
  local_date        date NOT NULL,                           -- NPT calendar date of punched_at
  direction         text NOT NULL CHECK (direction IN ('in', 'out', 'unknown')),
  verify_mode       text NOT NULL,
  source            text NOT NULL,
  raw               jsonb,
  gateway_signature text,
  idempotency_key   text NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now(),
  supersedes_id     uuid,
  PRIMARY KEY (id, punched_at),
  UNIQUE (idempotency_key, punched_at)
) PARTITION BY RANGE (punched_at);

CREATE TABLE punch_event_default PARTITION OF punch_event DEFAULT;
DO $$
DECLARE d date := '2026-01-01';
BEGIN
  WHILE d < '2028-01-01' LOOP
    EXECUTE format('CREATE TABLE punch_event_%s PARTITION OF punch_event FOR VALUES FROM (%L) TO (%L)',
                   to_char(d, 'YYYY_MM'), d, d + interval '1 month');
    d := d + interval '1 month';
  END LOOP;
END $$;

CREATE INDEX punch_event_employee_idx ON punch_event (employee_id, punched_at);
CREATE INDEX punch_event_device_idx   ON punch_event (device_id, punched_at);
CREATE INDEX punch_event_unmapped_idx ON punch_event (ingested_at) WHERE employee_id IS NULL;
CREATE INDEX punch_event_brin         ON punch_event USING brin (punched_at);

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; % is not allowed (create a superseding row instead)', TG_TABLE_NAME, TG_OP;
END $$;

CREATE TRIGGER punch_event_immutable BEFORE UPDATE OR DELETE ON punch_event
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- derived attendance (versioned)
CREATE TABLE attendance_day (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  employee_id   uuid NOT NULL REFERENCES employee(id),
  local_date    date NOT NULL,
  bs_date       text,
  version       integer NOT NULL,
  shift_id      uuid REFERENCES shift(id),
  status        text NOT NULL CHECK (status IN ('OffShift','Holiday','WeeklyOff','OnLeave','Present','HalfDay','HalfDayLeave','Absent','Incomplete','WFH')),
  first_in      timestamptz,
  last_out      timestamptz,
  worked_min    integer NOT NULL DEFAULT 0,
  break_min     integer NOT NULL DEFAULT 0,
  late_min      integer NOT NULL DEFAULT 0,
  early_min     integer NOT NULL DEFAULT 0,
  ot_min        integer NOT NULL DEFAULT 0,
  ot            jsonb NOT NULL DEFAULT '{}',
  flags         jsonb NOT NULL DEFAULT '[]',
  punch_ids     uuid[] NOT NULL DEFAULT '{}',
  inputs_hash   text NOT NULL,
  computed_at   timestamptz NOT NULL DEFAULT now(),
  locked_at     timestamptz,
  UNIQUE (employee_id, local_date, version)
);
CREATE INDEX attendance_day_latest_idx ON attendance_day (employee_id, local_date, version DESC);

-- ---------------------------------------------------------------- audit log (hash-chained, append-only)
CREATE TABLE audit_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  actor_id    uuid,
  action      text NOT NULL,
  table_name  text NOT NULL,
  row_id      text,
  before      jsonb,
  after       jsonb,
  prev_hash   text NOT NULL,
  row_hash    text NOT NULL
);

CREATE FUNCTION audit_log_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE last_hash text;
BEGIN
  -- Serialise inserts so the chain is linear.
  PERFORM pg_advisory_xact_lock(hashtext('audit_log_chain'));
  SELECT row_hash INTO last_hash FROM audit_log ORDER BY id DESC LIMIT 1;
  NEW.prev_hash := coalesce(last_hash, repeat('0', 64));
  NEW.row_hash := encode(sha256(convert_to(
    NEW.prev_hash || '|' || NEW.occurred_at::text || '|' || coalesce(NEW.actor_id::text, '') || '|' || NEW.action || '|' ||
    NEW.table_name || '|' || coalesce(NEW.row_id, '') || '|' || coalesce(NEW.before::text, '') || '|' || coalesce(NEW.after::text, ''),
    'UTF8')), 'hex');
  RETURN NEW;
END $$;

CREATE TRIGGER audit_log_chain_trg BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_chain();
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
