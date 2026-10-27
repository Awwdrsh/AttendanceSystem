-- Hajiri schema as of TS migration 0030_features_reports, dumped with pg_dump 18 --schema-only.
-- Applied by 0001_initial; never edit, add a new Django migration instead.
--
-- PostgreSQL database dump
--

-- Dumped from database version 18.6 (Homebrew)
-- Dumped by pg_dump version 18.6 (Homebrew)

SET LOCAL check_function_bodies = false;

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

--
-- Name: audit_log_chain(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.audit_log_chain() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

--
-- Name: forbid_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; % is not allowed (create a superseding row instead)', TG_TABLE_NAME, TG_OP;
END $$;

--
-- Name: hj_is_head(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.hj_is_head(emp uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
  SELECT emp IS NOT NULL AND (EXISTS (SELECT 1 FROM org_unit WHERE head_employee_id = emp) OR EXISTS (SELECT 1 FROM branch WHERE head_employee_id = emp))
$$;

--
-- Name: hj_manages(uuid, uuid, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.hj_manages(viewer uuid, target uuid, on_date date) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
  SELECT viewer IS NOT NULL AND target IS NOT NULL AND viewer <> target AND EXISTS (
    SELECT 1 FROM employee_assignment a
     WHERE a.employee_id = target AND a.valid @> on_date
       AND (a.manager_id = viewer
            OR EXISTS (SELECT 1 FROM branch b WHERE b.id = a.branch_id AND b.head_employee_id = viewer)
            OR EXISTS (
              WITH RECURSIVE up(id, parent_id, head, depth) AS (
                SELECT o.id, o.parent_id, o.head_employee_id, 1 FROM org_unit o WHERE o.id = a.org_unit_id
                UNION ALL
                SELECT p.id, p.parent_id, p.head_employee_id, up.depth + 1 FROM org_unit p JOIN up ON p.id = up.parent_id WHERE up.depth < 20)
              SELECT 1 FROM up WHERE up.head = viewer)))
$$;

--
-- Name: issued_letter_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.issued_letter_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.ref_no IS DISTINCT FROM OLD.ref_no OR NEW.subject IS DISTINCT FROM OLD.subject OR NEW.html IS DISTINCT FROM OLD.html
     OR NEW.employee_id IS DISTINCT FROM OLD.employee_id OR NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
    RAISE EXCEPTION 'an issued letter cannot be changed; void it and issue a new one';
  END IF;
  RETURN NEW;
END $$;

--
-- Name: issued_letter_no_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.issued_letter_no_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN RAISE EXCEPTION 'an issued letter cannot be deleted; void it instead'; END $$;

--
-- Name: payroll_run_locked_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.payroll_run_locked_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

--
-- Name: payslip_locked_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.payslip_locked_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE run_locked timestamptz; target uuid;
BEGIN
  IF TG_TABLE_NAME = 'payslip' THEN
    IF TG_OP = 'DELETE' THEN target := OLD.run_id; ELSE target := NEW.run_id; END IF;
    SELECT r.locked_at INTO run_locked FROM payroll_run r WHERE r.id = target;
    IF run_locked IS NOT NULL THEN
      -- Publishing (published_at) is the only change allowed after lock; every amount must stay identical.
      IF TG_OP = 'UPDATE' AND OLD.published_at IS DISTINCT FROM NEW.published_at AND OLD.net = NEW.net AND OLD.gross = NEW.gross
         AND OLD.tds = NEW.tds AND OLD.total_deductions = NEW.total_deductions AND OLD.taxable = NEW.taxable THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'payroll run is locked; % on payslip is not allowed (create a supplementary run)', TG_OP;
    END IF;
  ELSE
    IF TG_OP = 'DELETE' THEN target := OLD.payslip_id; ELSE target := NEW.payslip_id; END IF;
    SELECT r.locked_at INTO run_locked FROM payslip p JOIN payroll_run r ON r.id = p.run_id WHERE p.id = target;
    IF run_locked IS NOT NULL THEN
      RAISE EXCEPTION 'payroll run is locked; % on payslip_line is not allowed (create a supplementary run)', TG_OP;
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

SET LOCAL default_table_access_method = heap;

--
-- Name: announcement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.announcement (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    author_id uuid NOT NULL,
    author_employee_id uuid,
    title text NOT NULL,
    body text NOT NULL,
    audience_kind text DEFAULT 'all'::text NOT NULL,
    audience_id uuid,
    pinned boolean DEFAULT false NOT NULL,
    requires_ack boolean DEFAULT false NOT NULL,
    expires_on date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    popup boolean DEFAULT false NOT NULL,
    popup_until date,
    CONSTRAINT announcement_audience_kind_check CHECK ((audience_kind = ANY (ARRAY['all'::text, 'branch'::text, 'department'::text, 'reports'::text]))),
    CONSTRAINT announcement_body_check CHECK (((length(body) >= 1) AND (length(body) <= 8000))),
    CONSTRAINT announcement_title_check CHECK (((length(title) >= 1) AND (length(title) <= 160)))
);

--
-- Name: announcement_ack; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.announcement_ack (
    announcement_id uuid NOT NULL,
    user_id uuid NOT NULL,
    acked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: announcement_popup_seen; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.announcement_popup_seen (
    announcement_id uuid NOT NULL,
    user_id uuid NOT NULL,
    seen_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: app_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid,
    employee_id uuid,
    email text NOT NULL,
    name text NOT NULL,
    password_hash text NOT NULL,
    roles text[] DEFAULT '{employee}'::text[] NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    must_change_password boolean DEFAULT false NOT NULL,
    failed_logins integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    totp_secret_enc bytea,
    totp_pending_enc bytea,
    totp_enabled_at timestamp with time zone,
    totp_last_step bigint,
    CONSTRAINT app_user_status_check CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text])))
);

--
-- Name: approval_action; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_action (
    id uuid DEFAULT uuidv7() NOT NULL,
    instance_id uuid NOT NULL,
    step integer NOT NULL,
    actor_id uuid NOT NULL,
    decision text NOT NULL,
    reason text,
    on_behalf_of uuid,
    signature text,
    acted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_action_decision_check CHECK ((decision = ANY (ARRAY['approve'::text, 'reject'::text])))
);

--
-- Name: approval_flow; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_flow (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    object_type text NOT NULL,
    name text NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    steps jsonb NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_flow_name_check CHECK (((length(name) >= 1) AND (length(name) <= 100)))
);

--
-- Name: approver_delegate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approver_delegate (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    delegate_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    note text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    cancelled_at timestamp with time zone,
    CONSTRAINT approver_delegate_check CHECK ((to_date >= from_date)),
    CONSTRAINT approver_delegate_check1 CHECK ((user_id <> delegate_id))
);

--
-- Name: asset; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    type_id uuid,
    tag text NOT NULL,
    name text NOT NULL,
    serial text,
    condition text DEFAULT 'good'::text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    purchase_date date,
    cost numeric(14,2),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT asset_condition_check CHECK ((condition = ANY (ARRAY['new'::text, 'good'::text, 'fair'::text, 'poor'::text, 'damaged'::text, 'lost'::text]))),
    CONSTRAINT asset_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120))),
    CONSTRAINT asset_status_check CHECK ((status = ANY (ARRAY['available'::text, 'assigned'::text, 'retired'::text]))),
    CONSTRAINT asset_tag_check CHECK (((length(tag) >= 1) AND (length(tag) <= 40)))
);

--
-- Name: asset_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_assignment (
    id uuid DEFAULT uuidv7() NOT NULL,
    asset_id uuid NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    assigned_on date NOT NULL,
    returned_on date,
    condition_out text,
    condition_in text,
    notes text,
    assigned_by uuid,
    returned_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT asset_assignment_check CHECK (((returned_on IS NULL) OR (returned_on >= assigned_on)))
);

--
-- Name: asset_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_type (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    CONSTRAINT asset_type_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: attendance_day; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance_day (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    bs_date text,
    version integer NOT NULL,
    shift_id uuid,
    status text NOT NULL,
    first_in timestamp with time zone,
    last_out timestamp with time zone,
    worked_min integer DEFAULT 0 NOT NULL,
    break_min integer DEFAULT 0 NOT NULL,
    late_min integer DEFAULT 0 NOT NULL,
    early_min integer DEFAULT 0 NOT NULL,
    ot_min integer DEFAULT 0 NOT NULL,
    ot jsonb DEFAULT '{}'::jsonb NOT NULL,
    flags jsonb DEFAULT '[]'::jsonb NOT NULL,
    punch_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    inputs_hash text NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone,
    ot_slip_id uuid,
    CONSTRAINT attendance_day_status_check CHECK ((status = ANY (ARRAY['OffShift'::text, 'Holiday'::text, 'WeeklyOff'::text, 'OnLeave'::text, 'Present'::text, 'HalfDay'::text, 'HalfDayLeave'::text, 'Absent'::text, 'Incomplete'::text, 'WFH'::text])))
);

--
-- Name: attendance_nudge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance_nudge (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    kind text NOT NULL,
    ref text NOT NULL,
    local_date date NOT NULL,
    nudged_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attendance_nudge_kind_check CHECK ((kind = ANY (ARRAY['employee'::text, 'approver'::text])))
);

--
-- Name: attendance_upload; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance_upload (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    filename text,
    reason text NOT NULL,
    rows_total integer NOT NULL,
    punches integer NOT NULL,
    duplicates integer DEFAULT 0 NOT NULL,
    uploaded_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id uuid,
    action text NOT NULL,
    table_name text NOT NULL,
    row_id text,
    before jsonb,
    after jsonb,
    prev_hash text NOT NULL,
    row_hash text NOT NULL
);

--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.audit_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: bank_file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_file (
    id uuid DEFAULT uuidv7() NOT NULL,
    run_id uuid NOT NULL,
    bank_code text NOT NULL,
    format text NOT NULL,
    filename text NOT NULL,
    content text NOT NULL,
    checksum text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: biometric_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.biometric_template (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid,
    device_user_pin text,
    modality text NOT NULL,
    vendor_format text NOT NULL,
    ciphertext bytea NOT NULL,
    key_version integer DEFAULT 1 NOT NULL,
    aad_hash text NOT NULL,
    enrolled_on_device_id uuid,
    consent_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    shredded_at timestamp with time zone
);

--
-- Name: branch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.branch (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    tz_offset_min smallint DEFAULT 345 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    address text,
    phone text,
    head_employee_id uuid
);

--
-- Name: bs_calendar; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bs_calendar (
    bs_year smallint NOT NULL,
    bs_month smallint NOT NULL,
    bs_day smallint NOT NULL,
    bs_date text NOT NULL,
    ad_date date NOT NULL,
    CONSTRAINT bs_calendar_bs_day_check CHECK (((bs_day >= 1) AND (bs_day <= 32))),
    CONSTRAINT bs_calendar_bs_month_check CHECK (((bs_month >= 1) AND (bs_month <= 12)))
);

--
-- Name: career_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.career_event (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    kind text NOT NULL,
    effective_date date NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    before jsonb,
    note text,
    status text DEFAULT 'scheduled'::text NOT NULL,
    applied_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT career_event_kind_check CHECK ((kind = ANY (ARRAY['hire'::text, 'confirmation'::text, 'promotion'::text, 'transfer'::text, 'designation_change'::text, 'grade_change'::text, 'manager_change'::text, 'employment_type_change'::text, 'salary_revision'::text, 'separation'::text, 'rehire'::text, 'note'::text, 'suspension'::text, 'leave_of_absence'::text, 'acting'::text, 'deputation'::text]))),
    CONSTRAINT career_event_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'applied'::text, 'cancelled'::text])))
);

--
-- Name: company; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company (
    id uuid DEFAULT uuidv7() NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: company_policy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_policy (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    file_id uuid NOT NULL,
    audience text DEFAULT 'all'::text NOT NULL,
    audience_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    requires_ack boolean DEFAULT true NOT NULL,
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT company_policy_audience_check CHECK ((audience = ANY (ARRAY['all'::text, 'branch'::text, 'department'::text]))),
    CONSTRAINT company_policy_description_check CHECK ((length(description) <= 2000)),
    CONSTRAINT company_policy_title_check CHECK (((length(title) >= 1) AND (length(title) <= 160)))
);

--
-- Name: company_setting; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_setting (
    company_id uuid NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: consent_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.consent_record (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    purpose text NOT NULL,
    modality text NOT NULL,
    notice_version text NOT NULL,
    signature text,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    recorded_by uuid
);

--
-- Name: designation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.designation (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    grade text,
    job_level smallint,
    org_unit_id uuid,
    description text,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT designation_code_check CHECK (((length(code) >= 1) AND (length(code) <= 20))),
    CONSTRAINT designation_job_level_check CHECK (((job_level >= 1) AND (job_level <= 20))),
    CONSTRAINT designation_name_en_check CHECK (((length(name_en) >= 1) AND (length(name_en) <= 120)))
);

--
-- Name: device; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid,
    branch_id uuid,
    serial_no text NOT NULL,
    vendor text NOT NULL,
    protocol text NOT NULL,
    name text,
    status text DEFAULT 'pending'::text NOT NULL,
    comm_key_hash text,
    fw_version text,
    tz_offset_min smallint DEFAULT 345 NOT NULL,
    push_version text,
    attlog_stamp text DEFAULT '0'::text NOT NULL,
    oplog_stamp text DEFAULT '0'::text NOT NULL,
    last_handshake_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    clock_drift_sec integer,
    CONSTRAINT device_protocol_check CHECK ((protocol = ANY (ARRAY['adms'::text, 'isapi'::text, 'gsdk'::text, 'crosschex'::text, 'app'::text, 'manual'::text]))),
    CONSTRAINT device_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'active'::text, 'disabled'::text]))),
    CONSTRAINT device_vendor_check CHECK ((vendor = ANY (ARRAY['zkteco'::text, 'hikvision'::text, 'suprema'::text, 'anviz'::text, 'mobile'::text, 'kiosk'::text, 'manual'::text])))
);

--
-- Name: device_command; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_command (
    id bigint NOT NULL,
    device_id uuid NOT NULL,
    command text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    return_code integer,
    queued_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    acked_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT device_command_no_shell CHECK ((command !~* '^\s*SHELL'::text)),
    CONSTRAINT device_command_single_line CHECK ((command !~ '[
]'::text)),
    CONSTRAINT device_command_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'sent'::text, 'acked'::text, 'failed'::text])))
);

--
-- Name: device_command_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.device_command ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.device_command_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: device_user_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_user_map (
    device_id uuid NOT NULL,
    device_user_pin text NOT NULL,
    employee_id uuid NOT NULL,
    valid daterange NOT NULL
);

--
-- Name: disciplinary_case; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.disciplinary_case (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    number integer NOT NULL,
    employee_id uuid NOT NULL,
    category text NOT NULL,
    incident_date date NOT NULL,
    incident_enc bytea NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    show_cause_enc bytea,
    show_cause_at timestamp with time zone,
    response_due date,
    response_enc bytea,
    responded_at timestamp with time zone,
    stage_id uuid,
    decision_note_enc bytea,
    suspension_from date,
    suspension_to date,
    exit_id uuid,
    letter_id uuid,
    opened_by uuid,
    decided_by uuid,
    decided_at timestamp with time zone,
    overdue_notified boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT disciplinary_case_category_check CHECK ((category = ANY (ARRAY['attendance'::text, 'conduct'::text, 'performance'::text, 'policy'::text, 'safety'::text, 'misconduct'::text, 'other'::text]))),
    CONSTRAINT disciplinary_case_check CHECK (((suspension_to IS NULL) OR (suspension_from IS NULL) OR (suspension_to >= suspension_from))),
    CONSTRAINT disciplinary_case_status_check CHECK ((status = ANY (ARRAY['open'::text, 'show_cause'::text, 'responded'::text, 'decided'::text, 'withdrawn'::text])))
);

--
-- Name: disciplinary_stage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.disciplinary_stage (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    severity smallint DEFAULT 1 NOT NULL,
    effect text DEFAULT 'none'::text NOT NULL,
    letter_template_code text,
    sort smallint DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT disciplinary_stage_code_check CHECK ((code ~ '^[a-z0-9_]{2,40}$'::text)),
    CONSTRAINT disciplinary_stage_effect_check CHECK ((effect = ANY (ARRAY['none'::text, 'suspension'::text, 'termination'::text]))),
    CONSTRAINT disciplinary_stage_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80))),
    CONSTRAINT disciplinary_stage_severity_check CHECK (((severity >= 1) AND (severity <= 9)))
);

--
-- Name: document_category; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_category (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    employee_can_upload boolean DEFAULT true NOT NULL,
    expiry_required boolean DEFAULT false NOT NULL,
    visible_to_employee boolean DEFAULT true NOT NULL,
    sort_order smallint DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_category_code_check CHECK ((code ~ '^[a-z0-9_]{2,40}$'::text)),
    CONSTRAINT document_category_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: document_expiry_alert; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_expiry_alert (
    document_id uuid NOT NULL,
    threshold smallint NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: employee; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    emp_code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    gender text,
    dob date,
    ot_eligible boolean DEFAULT true NOT NULL,
    weekly_off_days smallint[] DEFAULT '{6}'::smallint[] NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    marital_status text,
    join_date date,
    exit_date date,
    designation text,
    grade text,
    phone text,
    email text,
    regime text DEFAULT 'ssf'::text NOT NULL,
    ssf_id text,
    pf_id text,
    cit_id text,
    pan_enc bytea,
    pan_bidx text,
    bank_code text,
    bank_branch text,
    bank_account_enc bytea,
    bank_account_last4 text,
    is_resident boolean DEFAULT true NOT NULL,
    remote_area_class text,
    disability boolean DEFAULT false NOT NULL,
    festival_month smallint,
    probation_end date,
    work_from_home boolean DEFAULT false NOT NULL,
    designation_id uuid,
    blood_group text,
    nationality text,
    personal_email text,
    personal_phone text,
    work_phone text,
    permanent_address jsonb,
    temporary_address jsonb,
    skills text[] DEFAULT '{}'::text[] NOT NULL,
    languages text[] DEFAULT '{}'::text[] NOT NULL,
    citizenship_no_enc bytea,
    citizenship_district text,
    citizenship_issued_on date,
    passport_no_enc bytea,
    passport_expiry date,
    licence_no_enc bytea,
    licence_expiry date,
    confirmed_on date,
    avatar_file_id uuid,
    field_work boolean DEFAULT false NOT NULL,
    contract_end date,
    CONSTRAINT employee_blood_group_check CHECK ((blood_group = ANY (ARRAY['A+'::text, 'A-'::text, 'B+'::text, 'B-'::text, 'AB+'::text, 'AB-'::text, 'O+'::text, 'O-'::text]))),
    CONSTRAINT employee_festival_month_check CHECK (((festival_month >= 1) AND (festival_month <= 12))),
    CONSTRAINT employee_gender_check CHECK ((gender = ANY (ARRAY['male'::text, 'female'::text, 'other'::text]))),
    CONSTRAINT employee_marital_status_check CHECK ((marital_status = ANY (ARRAY['single'::text, 'couple'::text]))),
    CONSTRAINT employee_regime_check CHECK ((regime = ANY (ARRAY['ssf'::text, 'pf'::text, 'none'::text]))),
    CONSTRAINT employee_remote_area_class_check CHECK ((remote_area_class = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'D'::text]))),
    CONSTRAINT employee_status_check CHECK ((status = ANY (ARRAY['active'::text, 'exited'::text])))
);

--
-- Name: employee_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_assignment (
    employee_id uuid NOT NULL,
    valid daterange NOT NULL,
    org_unit_id uuid,
    branch_id uuid,
    manager_id uuid,
    employment_type text NOT NULL,
    probation_end date,
    CONSTRAINT employee_assignment_employment_type_check CHECK ((employment_type = ANY (ARRAY['regular'::text, 'work_based'::text, 'time_based'::text, 'casual'::text, 'part_time'::text])))
);

--
-- Name: employee_confirmation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_confirmation (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    confirmed_on date NOT NULL,
    review_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: employee_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_document (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    category_id uuid NOT NULL,
    title text NOT NULL,
    file_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    replaces_id uuid,
    is_current boolean DEFAULT true NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    reject_reason text,
    expiry_date date,
    visible_to_employee boolean DEFAULT true NOT NULL,
    notes text,
    uploaded_by uuid,
    verified_by uuid,
    verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT employee_document_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'verified'::text, 'rejected'::text]))),
    CONSTRAINT employee_document_title_check CHECK (((length(title) >= 1) AND (length(title) <= 120))),
    CONSTRAINT employee_document_version_check CHECK ((version >= 1))
);

--
-- Name: employee_education; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_education (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    level text NOT NULL,
    degree text,
    field text,
    institution text NOT NULL,
    board text,
    start_year smallint,
    end_year smallint,
    result text,
    sort smallint DEFAULT 0 NOT NULL
);

--
-- Name: employee_emergency_contact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_emergency_contact (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    name text NOT NULL,
    relation text NOT NULL,
    phone text NOT NULL,
    alt_phone text,
    address text,
    sort smallint DEFAULT 0 NOT NULL
);

--
-- Name: employee_exit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_exit (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    kind text NOT NULL,
    voluntary boolean NOT NULL,
    reason_category text,
    reason text,
    submitted_on date NOT NULL,
    notice_days smallint DEFAULT 0 NOT NULL,
    proposed_lwd date NOT NULL,
    accepted_lwd date,
    status text DEFAULT 'pending'::text NOT NULL,
    initiated_by uuid,
    decided_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT employee_exit_kind_check CHECK ((kind = ANY (ARRAY['resignation'::text, 'termination'::text, 'retirement'::text, 'contract_end'::text, 'death'::text, 'other'::text]))),
    CONSTRAINT employee_exit_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text, 'withdrawn'::text, 'cancelled'::text, 'completed'::text])))
);

--
-- Name: employee_experience; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_experience (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    organisation text NOT NULL,
    designation text,
    from_date date,
    to_date date,
    responsibilities text,
    reason_for_leaving text,
    sort smallint DEFAULT 0 NOT NULL
);

--
-- Name: employee_family; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_family (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    name text NOT NULL,
    relation text NOT NULL,
    dob date,
    occupation text,
    phone text,
    is_dependent boolean DEFAULT false NOT NULL,
    is_nominee boolean DEFAULT false NOT NULL,
    nominee_share numeric(5,2),
    sort smallint DEFAULT 0 NOT NULL,
    CONSTRAINT employee_family_nominee_share_check CHECK (((nominee_share IS NULL) OR ((nominee_share > (0)::numeric) AND (nominee_share <= (100)::numeric))))
);

--
-- Name: employee_field_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_field_history (
    id bigint NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    field text NOT NULL,
    old_value text,
    new_value text,
    source text NOT NULL,
    ref_id uuid,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT employee_field_history_source_check CHECK ((source = ANY (ARRAY['hr_edit'::text, 'change_request'::text, 'career_event'::text, 'import'::text, 'system'::text])))
);

--
-- Name: employee_field_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.employee_field_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.employee_field_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: employee_temp_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_temp_assignment (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    kind text NOT NULL,
    designation text,
    designation_id uuid,
    branch_id uuid,
    org_unit_id uuid,
    start_date date NOT NULL,
    end_date date NOT NULL,
    note text,
    status text DEFAULT 'active'::text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    CONSTRAINT employee_temp_assignment_check CHECK ((end_date >= start_date)),
    CONSTRAINT employee_temp_assignment_kind_check CHECK ((kind = ANY (ARRAY['acting'::text, 'deputation'::text]))),
    CONSTRAINT employee_temp_assignment_status_check CHECK ((status = ANY (ARRAY['active'::text, 'ended'::text, 'cancelled'::text])))
);

--
-- Name: engage_comment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.engage_comment (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    user_id uuid NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT engage_comment_body_check CHECK (((length(body) >= 1) AND (length(body) <= 1000))),
    CONSTRAINT engage_comment_target_id_check CHECK ((length(target_id) <= 120)),
    CONSTRAINT engage_comment_target_type_check CHECK ((target_type = ANY (ARRAY['kudos'::text, 'announcement'::text, 'celebration'::text])))
);

--
-- Name: engage_pref; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.engage_pref (
    employee_id uuid NOT NULL,
    show_birthday boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: engage_reaction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.engage_reaction (
    company_id uuid NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    user_id uuid NOT NULL,
    emoji text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT engage_reaction_emoji_check CHECK ((emoji = ANY (ARRAY['like'::text, 'love'::text, 'clap'::text, 'celebrate'::text, 'laugh'::text]))),
    CONSTRAINT engage_reaction_target_id_check CHECK ((length(target_id) <= 120)),
    CONSTRAINT engage_reaction_target_type_check CHECK ((target_type = ANY (ARRAY['kudos'::text, 'announcement'::text, 'celebration'::text])))
);

--
-- Name: engage_setting; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.engage_setting (
    company_id uuid NOT NULL,
    kudos_monthly_limit smallint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT engage_setting_kudos_monthly_limit_check CHECK (((kudos_monthly_limit IS NULL) OR ((kudos_monthly_limit >= 1) AND (kudos_monthly_limit <= 100))))
);

--
-- Name: engage_value; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.engage_value (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    emoji text DEFAULT '⭐'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    sort_order smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT engage_value_description_check CHECK ((length(description) <= 200)),
    CONSTRAINT engage_value_emoji_check CHECK ((length(emoji) <= 8)),
    CONSTRAINT engage_value_name_check CHECK (((length(name) >= 1) AND (length(name) <= 40)))
);

--
-- Name: exit_interview; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exit_interview (
    exit_id uuid NOT NULL,
    company_id uuid NOT NULL,
    answers_enc bytea NOT NULL,
    by_employee boolean NOT NULL,
    recorded_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: exit_interview_question; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exit_interview_question (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    sort smallint DEFAULT 0 NOT NULL,
    question text NOT NULL,
    kind text NOT NULL,
    options jsonb DEFAULT '[]'::jsonb NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT exit_interview_question_kind_check CHECK ((kind = ANY (ARRAY['text'::text, 'rating'::text, 'choice'::text]))),
    CONSTRAINT exit_interview_question_question_check CHECK (((length(question) >= 1) AND (length(question) <= 300)))
);

--
-- Name: expense_category; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_category (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    kind text DEFAULT 'amount'::text NOT NULL,
    receipt_required_above numeric(14,2),
    active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT expense_category_code_check CHECK ((code ~ '^[A-Z][A-Z0-9_]{1,29}$'::text)),
    CONSTRAINT expense_category_kind_check CHECK ((kind = ANY (ARRAY['amount'::text, 'mileage'::text]))),
    CONSTRAINT expense_category_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80))),
    CONSTRAINT expense_category_receipt_required_above_check CHECK ((receipt_required_above >= (0)::numeric))
);

--
-- Name: expense_claim; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_claim (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    title text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    total_claimed numeric(14,2) DEFAULT 0 NOT NULL,
    total_approved numeric(14,2),
    workflow_instance_id uuid,
    submitted_at timestamp with time zone,
    decided_at timestamp with time zone,
    decision_note text,
    payment_method text,
    paid_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT expense_claim_payment_method_check CHECK ((payment_method = ANY (ARRAY['payroll'::text, 'cash'::text]))),
    CONSTRAINT expense_claim_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text, 'paid'::text]))),
    CONSTRAINT expense_claim_title_check CHECK (((length(title) >= 1) AND (length(title) <= 120)))
);

--
-- Name: expense_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_item (
    id uuid DEFAULT uuidv7() NOT NULL,
    claim_id uuid NOT NULL,
    expense_date date NOT NULL,
    category_id uuid NOT NULL,
    description text NOT NULL,
    amount numeric(14,2) NOT NULL,
    km numeric(8,1),
    receipt_file_id uuid,
    approved_amount numeric(14,2),
    adjust_reason text,
    sort_order integer DEFAULT 0 NOT NULL,
    CONSTRAINT expense_item_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT expense_item_approved_amount_check CHECK ((approved_amount >= (0)::numeric)),
    CONSTRAINT expense_item_description_check CHECK (((length(description) >= 1) AND (length(description) <= 300))),
    CONSTRAINT expense_item_km_check CHECK ((km > (0)::numeric))
);

--
-- Name: expense_limit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_limit (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    category_id uuid NOT NULL,
    grade text NOT NULL,
    per_claim numeric(14,2),
    per_month numeric(14,2),
    CONSTRAINT expense_limit_grade_check CHECK (((length(grade) >= 1) AND (length(grade) <= 40))),
    CONSTRAINT expense_limit_per_claim_check CHECK ((per_claim >= (0)::numeric)),
    CONSTRAINT expense_limit_per_month_check CHECK ((per_month >= (0)::numeric))
);

--
-- Name: expense_payout; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expense_payout (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    source_type text NOT NULL,
    source_id uuid NOT NULL,
    component_code text NOT NULL,
    amount numeric(14,2) NOT NULL,
    note text,
    status text DEFAULT 'pending'::text NOT NULL,
    payroll_run_id uuid,
    paid_amount numeric(14,2),
    paid_at timestamp with time zone,
    parent_payout_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT expense_payout_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT expense_payout_component_code_check CHECK ((component_code = ANY (ARRAY['EXP_REIMB'::text, 'TRAVEL_ADV'::text, 'EXP_RECOVERY'::text]))),
    CONSTRAINT expense_payout_source_type_check CHECK ((source_type = ANY (ARRAY['expense_claim'::text, 'travel_advance'::text, 'travel_settlement'::text]))),
    CONSTRAINT expense_payout_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'paid'::text, 'cash'::text, 'cancelled'::text])))
);

--
-- Name: field_visit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.field_visit (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    place text NOT NULL,
    purpose text,
    started_at timestamp with time zone NOT NULL,
    start_lat numeric(9,6),
    start_lng numeric(9,6),
    start_accuracy_m integer,
    ended_at timestamp with time zone,
    end_lat numeric(9,6),
    end_lng numeric(9,6),
    end_accuracy_m integer,
    outcome text,
    photo_file_id uuid,
    punch_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT field_visit_check CHECK (((ended_at IS NULL) OR (ended_at >= started_at))),
    CONSTRAINT field_visit_outcome_check CHECK ((length(outcome) <= 1000)),
    CONSTRAINT field_visit_place_check CHECK (((length(place) >= 1) AND (length(place) <= 160))),
    CONSTRAINT field_visit_purpose_check CHECK ((length(purpose) <= 500))
);

--
-- Name: grievance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grievance (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    number integer NOT NULL,
    employee_id uuid NOT NULL,
    user_id uuid NOT NULL,
    category text NOT NULL,
    subject_enc bytea NOT NULL,
    description_enc bytea NOT NULL,
    concerns_enc bytea,
    status text DEFAULT 'submitted'::text NOT NULL,
    excluded_user_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    hearing_at timestamp with time zone,
    hearing_place text,
    outcome text,
    findings_enc bytea,
    decided_by uuid,
    decided_at timestamp with time zone,
    appeal_reason_enc bytea,
    appealed_at timestamp with time zone,
    appeal_outcome text,
    appeal_note_enc bytea,
    appeal_decided_by uuid,
    appeal_decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT grievance_appeal_outcome_check CHECK ((appeal_outcome = ANY (ARRAY['decision_upheld'::text, 'decision_changed'::text]))),
    CONSTRAINT grievance_category_check CHECK ((category = ANY (ARRAY['harassment'::text, 'discrimination'::text, 'safety'::text, 'pay_benefits'::text, 'management'::text, 'policy'::text, 'other'::text]))),
    CONSTRAINT grievance_outcome_check CHECK ((outcome = ANY (ARRAY['upheld'::text, 'partly_upheld'::text, 'not_upheld'::text]))),
    CONSTRAINT grievance_status_check CHECK ((status = ANY (ARRAY['submitted'::text, 'under_review'::text, 'hearing_scheduled'::text, 'decided'::text, 'appealed'::text, 'closed'::text, 'withdrawn'::text])))
);

--
-- Name: grievance_committee_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grievance_committee_member (
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    added_by uuid,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: grievance_file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grievance_file (
    grievance_id uuid NOT NULL,
    file_id uuid NOT NULL
);

--
-- Name: grievance_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grievance_note (
    id uuid DEFAULT uuidv7() NOT NULL,
    grievance_id uuid NOT NULL,
    author_id uuid NOT NULL,
    shared boolean DEFAULT false NOT NULL,
    body_enc bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: helpdesk_category; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.helpdesk_category (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    confidential boolean DEFAULT false NOT NULL,
    owner_user_id uuid,
    sort smallint DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT helpdesk_category_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: helpdesk_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.helpdesk_message (
    id uuid DEFAULT uuidv7() NOT NULL,
    ticket_id uuid NOT NULL,
    company_id uuid NOT NULL,
    author_user_id uuid NOT NULL,
    body text NOT NULL,
    internal boolean DEFAULT false NOT NULL,
    file_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT helpdesk_message_body_check CHECK (((length(body) >= 1) AND (length(body) <= 5000)))
);

--
-- Name: helpdesk_ticket; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.helpdesk_ticket (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    number integer NOT NULL,
    requester_user_id uuid NOT NULL,
    employee_id uuid,
    category_id uuid NOT NULL,
    subject text NOT NULL,
    priority text DEFAULT 'normal'::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    assignee_user_id uuid,
    sla_due_at timestamp with time zone NOT NULL,
    first_response_at timestamp with time zone,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    breached_at timestamp with time zone,
    rating smallint,
    rating_comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT helpdesk_ticket_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'normal'::text, 'high'::text, 'urgent'::text]))),
    CONSTRAINT helpdesk_ticket_rating_check CHECK (((rating >= 1) AND (rating <= 5))),
    CONSTRAINT helpdesk_ticket_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'waiting'::text, 'resolved'::text, 'closed'::text]))),
    CONSTRAINT helpdesk_ticket_subject_check CHECK (((length(subject) >= 1) AND (length(subject) <= 160)))
);

--
-- Name: hiring_application; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_application (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    job_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    stage text DEFAULT 'Applied'::text NOT NULL,
    furthest_stage smallint DEFAULT 0 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    source text DEFAULT 'careers'::text NOT NULL,
    cv_file_id uuid,
    cover_note text,
    answers jsonb DEFAULT '{}'::jsonb NOT NULL,
    experience_years numeric(4,1),
    current_employer text,
    score smallint,
    score_detail jsonb DEFAULT '[]'::jsonb NOT NULL,
    knocked_out boolean DEFAULT false NOT NULL,
    rating smallint,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    owner_user_id uuid,
    rejected_reason text,
    closed_at timestamp with time zone,
    hired_employee_id uuid,
    consent_at timestamp with time zone,
    stage_changed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_application_rating_check CHECK (((rating >= 1) AND (rating <= 5))),
    CONSTRAINT hiring_application_source_check CHECK ((source = ANY (ARRAY['careers'::text, 'referral'::text, 'manual'::text, 'job_board'::text, 'other'::text]))),
    CONSTRAINT hiring_application_status_check CHECK ((status = ANY (ARRAY['active'::text, 'rejected'::text, 'withdrawn'::text, 'hired'::text])))
);

--
-- Name: hiring_candidate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_candidate (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    email text,
    phone text,
    purged_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: hiring_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_event (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    application_id uuid NOT NULL,
    actor_id uuid,
    kind text NOT NULL,
    body text,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: hiring_interview; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_interview (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    application_id uuid NOT NULL,
    title text DEFAULT 'Interview'::text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    duration_min smallint DEFAULT 45 NOT NULL,
    mode text NOT NULL,
    location text,
    panel uuid[] NOT NULL,
    status text DEFAULT 'scheduled'::text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_interview_duration_min_check CHECK (((duration_min >= 10) AND (duration_min <= 480))),
    CONSTRAINT hiring_interview_mode_check CHECK ((mode = ANY (ARRAY['in_person'::text, 'phone'::text, 'video'::text]))),
    CONSTRAINT hiring_interview_panel_check CHECK (((cardinality(panel) >= 1) AND (cardinality(panel) <= 10))),
    CONSTRAINT hiring_interview_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'completed'::text, 'cancelled'::text])))
);

--
-- Name: hiring_job; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_job (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    requisition_id uuid,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    requirements text DEFAULT ''::text NOT NULL,
    category text,
    location text,
    work_mode text DEFAULT 'onsite'::text NOT NULL,
    employment_type text DEFAULT 'full_time'::text NOT NULL,
    experience_min numeric(4,1),
    experience_max numeric(4,1),
    salary_min numeric(12,2),
    salary_max numeric(12,2),
    show_salary boolean DEFAULT false NOT NULL,
    openings smallint DEFAULT 1 NOT NULL,
    deadline date,
    org_unit_id uuid,
    branch_id uuid,
    hiring_manager_id uuid,
    stages jsonb DEFAULT '["Applied", "Screening", "Interview", "Offer", "Hired"]'::jsonb NOT NULL,
    questions jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    published_at timestamp with time zone,
    closed_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_job_check CHECK (((experience_min IS NULL) OR (experience_max IS NULL) OR (experience_max >= experience_min))),
    CONSTRAINT hiring_job_check1 CHECK (((salary_min IS NULL) OR (salary_max IS NULL) OR (salary_max >= salary_min))),
    CONSTRAINT hiring_job_employment_type_check CHECK ((employment_type = ANY (ARRAY['full_time'::text, 'part_time'::text, 'contract'::text, 'internship'::text, 'temporary'::text]))),
    CONSTRAINT hiring_job_experience_max_check CHECK ((experience_max >= (0)::numeric)),
    CONSTRAINT hiring_job_experience_min_check CHECK ((experience_min >= (0)::numeric)),
    CONSTRAINT hiring_job_openings_check CHECK (((openings >= 1) AND (openings <= 500))),
    CONSTRAINT hiring_job_salary_max_check CHECK ((salary_max >= (0)::numeric)),
    CONSTRAINT hiring_job_salary_min_check CHECK ((salary_min >= (0)::numeric)),
    CONSTRAINT hiring_job_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'open'::text, 'closed'::text]))),
    CONSTRAINT hiring_job_title_check CHECK (((length(title) >= 2) AND (length(title) <= 120))),
    CONSTRAINT hiring_job_work_mode_check CHECK ((work_mode = ANY (ARRAY['onsite'::text, 'remote'::text, 'hybrid'::text])))
);

--
-- Name: hiring_offer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_offer (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    application_id uuid NOT NULL,
    designation text NOT NULL,
    salary numeric(12,2) NOT NULL,
    join_date date NOT NULL,
    expires_on date,
    notes text,
    status text DEFAULT 'pending_approval'::text NOT NULL,
    created_by uuid,
    created_by_employee_id uuid,
    decided_at timestamp with time zone,
    responded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_offer_salary_check CHECK ((salary > (0)::numeric)),
    CONSTRAINT hiring_offer_status_check CHECK ((status = ANY (ARRAY['pending_approval'::text, 'approved'::text, 'rejected'::text, 'accepted'::text, 'declined'::text, 'withdrawn'::text])))
);

--
-- Name: hiring_requisition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_requisition (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    raised_by uuid NOT NULL,
    raised_by_employee_id uuid,
    title text NOT NULL,
    designation text,
    org_unit_id uuid,
    branch_id uuid,
    headcount smallint DEFAULT 1 NOT NULL,
    filled smallint DEFAULT 0 NOT NULL,
    salary_min numeric(12,2),
    salary_max numeric(12,2),
    kind text DEFAULT 'new'::text NOT NULL,
    replacing_employee_id uuid,
    justification text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_at timestamp with time zone,
    decision_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_requisition_check CHECK (((salary_min IS NULL) OR (salary_max IS NULL) OR (salary_max >= salary_min))),
    CONSTRAINT hiring_requisition_filled_check CHECK ((filled >= 0)),
    CONSTRAINT hiring_requisition_headcount_check CHECK (((headcount >= 1) AND (headcount <= 500))),
    CONSTRAINT hiring_requisition_kind_check CHECK ((kind = ANY (ARRAY['new'::text, 'replacement'::text]))),
    CONSTRAINT hiring_requisition_salary_max_check CHECK ((salary_max >= (0)::numeric)),
    CONSTRAINT hiring_requisition_salary_min_check CHECK ((salary_min >= (0)::numeric)),
    CONSTRAINT hiring_requisition_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text, 'filled'::text]))),
    CONSTRAINT hiring_requisition_title_check CHECK (((length(title) >= 2) AND (length(title) <= 120)))
);

--
-- Name: hiring_scorecard; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hiring_scorecard (
    id uuid DEFAULT uuidv7() NOT NULL,
    interview_id uuid NOT NULL,
    reviewer_id uuid NOT NULL,
    ratings jsonb DEFAULT '{}'::jsonb NOT NULL,
    recommendation text NOT NULL,
    notes text,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hiring_scorecard_recommendation_check CHECK ((recommendation = ANY (ARRAY['strong_no'::text, 'no'::text, 'yes'::text, 'strong_yes'::text])))
);

--
-- Name: holiday; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.holiday (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    local_date date NOT NULL,
    bs_date text,
    name_en text NOT NULL,
    name_ne text,
    gender_scope text DEFAULT 'all'::text NOT NULL,
    optional boolean DEFAULT false NOT NULL,
    CONSTRAINT holiday_gender_scope_check CHECK ((gender_scope = ANY (ARRAY['all'::text, 'female'::text])))
);

--
-- Name: issued_letter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.issued_letter (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    template_id uuid,
    template_code text NOT NULL,
    ref_no text NOT NULL,
    subject text NOT NULL,
    html text NOT NULL,
    visible_to_employee boolean DEFAULT true NOT NULL,
    request_id uuid,
    issued_by uuid,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    voided_at timestamp with time zone,
    voided_by uuid,
    void_reason text
);

--
-- Name: kiosk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kiosk (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    branch_id uuid,
    location_id uuid,
    selfie_required boolean DEFAULT true NOT NULL,
    status text DEFAULT 'pairing'::text NOT NULL,
    pairing_code_hash text,
    pairing_expires_at timestamp with time zone,
    key_hash text,
    paired_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    failed_attempts integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT kiosk_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80))),
    CONSTRAINT kiosk_status_check CHECK ((status = ANY (ARRAY['pairing'::text, 'active'::text, 'revoked'::text])))
);

--
-- Name: kiosk_credential; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kiosk_credential (
    employee_id uuid NOT NULL,
    company_id uuid NOT NULL,
    pin_hash text,
    failed_count integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    qr_version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: kiosk_qr_use; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kiosk_qr_use (
    employee_id uuid NOT NULL,
    time_window bigint NOT NULL,
    kiosk_id uuid,
    used_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: kudos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kudos (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    giver_id uuid NOT NULL,
    giver_employee_id uuid NOT NULL,
    value_id uuid,
    message text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT kudos_message_check CHECK (((length(message) >= 1) AND (length(message) <= 1000)))
);

--
-- Name: kudos_recipient; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kudos_recipient (
    kudos_id uuid NOT NULL,
    employee_id uuid NOT NULL
);

--
-- Name: kye_campaign; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kye_campaign (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    title text NOT NULL,
    message text,
    sections text[] NOT NULL,
    audience_kind text DEFAULT 'all'::text NOT NULL,
    audience_id uuid,
    due_date date NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    reminded boolean DEFAULT false NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    CONSTRAINT kye_campaign_audience_kind_check CHECK ((audience_kind = ANY (ARRAY['all'::text, 'branch'::text, 'department'::text]))),
    CONSTRAINT kye_campaign_status_check CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text]))),
    CONSTRAINT kye_campaign_title_check CHECK (((length(title) >= 1) AND (length(title) <= 160)))
);

--
-- Name: kye_response; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kye_response (
    campaign_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    note text,
    responded_at timestamp with time zone,
    last_nudged_at timestamp with time zone,
    CONSTRAINT kye_response_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'confirmed'::text, 'changes_requested'::text])))
);

--
-- Name: late_penalty; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.late_penalty (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    bs_year smallint NOT NULL,
    bs_month smallint NOT NULL,
    late_days integer NOT NULL,
    late_minutes integer DEFAULT 0 NOT NULL,
    penalty_days numeric(5,2) NOT NULL,
    leave_days numeric(5,2) DEFAULT 0 NOT NULL,
    leave_type_id uuid,
    unpaid_days numeric(5,2) DEFAULT 0 NOT NULL,
    rule jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'applied'::text NOT NULL,
    applied_by uuid,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    reversed_by uuid,
    reversed_at timestamp with time zone,
    reverse_reason text,
    CONSTRAINT late_penalty_bs_month_check CHECK (((bs_month >= 1) AND (bs_month <= 12))),
    CONSTRAINT late_penalty_late_days_check CHECK ((late_days >= 0)),
    CONSTRAINT late_penalty_penalty_days_check CHECK ((penalty_days > (0)::numeric)),
    CONSTRAINT late_penalty_status_check CHECK ((status = ANY (ARRAY['applied'::text, 'reversed'::text])))
);

--
-- Name: leave_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_ledger (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    posted_at timestamp with time zone DEFAULT now() NOT NULL,
    effective_date date NOT NULL,
    delta_days numeric(6,2) NOT NULL,
    reason text NOT NULL,
    ref_type text,
    ref_id uuid,
    expires_on date,
    note text,
    posted_by uuid,
    CONSTRAINT leave_ledger_reason_check CHECK ((reason = ANY (ARRAY['allocation'::text, 'accrual'::text, 'usage'::text, 'cancellation'::text, 'carry_forward'::text, 'expiry'::text, 'encashment'::text, 'adjustment'::text, 'comp_off'::text])))
);

--
-- Name: leave_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    half_day text,
    days_requested numeric(6,2) NOT NULL,
    reason text,
    attachment_key text,
    status text DEFAULT 'pending'::text NOT NULL,
    workflow_instance_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    from_time time without time zone,
    to_time time without time zone,
    hours numeric(5,2),
    bridge_days numeric(5,2) DEFAULT 0 NOT NULL,
    bridge_type_id uuid,
    bridge_from date,
    bridge_to date,
    CONSTRAINT leave_request_check CHECK ((to_date >= from_date)),
    CONSTRAINT leave_request_half_day_check CHECK ((half_day = ANY (ARRAY['first_half'::text, 'second_half'::text]))),
    CONSTRAINT leave_request_hourly_chk CHECK ((((from_time IS NULL) AND (to_time IS NULL) AND (hours IS NULL)) OR ((from_time IS NOT NULL) AND (to_time IS NOT NULL) AND (to_time > from_time) AND (hours > (0)::numeric) AND (half_day IS NULL) AND (from_date = to_date)))),
    CONSTRAINT leave_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: leave_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_type (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    paid boolean DEFAULT true NOT NULL,
    statutory boolean DEFAULT false NOT NULL,
    accrual_basis text DEFAULT 'none'::text NOT NULL,
    accrual_days numeric(6,2) DEFAULT 0 NOT NULL,
    accrual_per_worked_days integer,
    max_balance numeric(6,2),
    carry_forward boolean DEFAULT true NOT NULL,
    encashable boolean DEFAULT false NOT NULL,
    certificate_after_days integer,
    allow_half_day boolean DEFAULT true NOT NULL,
    max_consecutive_days integer,
    min_service_days integer DEFAULT 0 NOT NULL,
    negative_balance_limit numeric(6,2) DEFAULT 0 NOT NULL,
    gender_scope text DEFAULT 'all'::text NOT NULL,
    sandwich_rule boolean DEFAULT false NOT NULL,
    expiry_days integer,
    sort_order integer DEFAULT 0 NOT NULL,
    event_days numeric(6,2),
    allow_hourly boolean DEFAULT false NOT NULL,
    club_with_id uuid,
    CONSTRAINT leave_type_accrual_basis_check CHECK ((accrual_basis = ANY (ARRAY['none'::text, 'worked_days'::text, 'monthly'::text, 'annual'::text]))),
    CONSTRAINT leave_type_club_not_self CHECK (((club_with_id IS NULL) OR (club_with_id <> id))),
    CONSTRAINT leave_type_gender_scope_check CHECK ((gender_scope = ANY (ARRAY['all'::text, 'female'::text, 'male'::text])))
);

--
-- Name: letter_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.letter_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    template_id uuid NOT NULL,
    purpose text NOT NULL,
    addressed_to text,
    status text DEFAULT 'pending'::text NOT NULL,
    decision_reason text,
    workflow_instance_id uuid,
    issued_letter_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT letter_request_addressed_to_check CHECK ((length(addressed_to) <= 200)),
    CONSTRAINT letter_request_purpose_check CHECK (((length(purpose) >= 1) AND (length(purpose) <= 300))),
    CONSTRAINT letter_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'issued'::text, 'cancelled'::text])))
);

--
-- Name: letter_sequence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.letter_sequence (
    company_id uuid NOT NULL,
    bs_year integer NOT NULL,
    last_no integer DEFAULT 0 NOT NULL
);

--
-- Name: letter_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.letter_template (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    requestable boolean DEFAULT false NOT NULL,
    visible_to_employee boolean DEFAULT true NOT NULL,
    is_builtin boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT letter_template_body_check CHECK (((length(body) >= 1) AND (length(body) <= 20000))),
    CONSTRAINT letter_template_code_check CHECK ((code ~ '^[a-z0-9_]{2,40}$'::text)),
    CONSTRAINT letter_template_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80))),
    CONSTRAINT letter_template_subject_check CHECK (((length(subject) >= 1) AND (length(subject) <= 160)))
);

--
-- Name: lifecycle_case; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_case (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    kind text NOT NULL,
    template_id uuid,
    anchor_date date NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    exit_id uuid,
    started_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT lifecycle_case_kind_check CHECK ((kind = ANY (ARRAY['onboarding'::text, 'offboarding'::text]))),
    CONSTRAINT lifecycle_case_status_check CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'cancelled'::text])))
);

--
-- Name: lifecycle_reminder; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_reminder (
    company_id uuid NOT NULL,
    kind text NOT NULL,
    ref_id uuid NOT NULL,
    key text NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: lifecycle_task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_task (
    id uuid DEFAULT uuidv7() NOT NULL,
    case_id uuid NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    sort smallint DEFAULT 0 NOT NULL,
    title text NOT NULL,
    description text,
    department text,
    assignee_type text NOT NULL,
    assignee_user_id uuid,
    due_date date,
    link text,
    status text DEFAULT 'open'::text NOT NULL,
    note text,
    done_by uuid,
    done_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lifecycle_task_assignee_type_check CHECK ((assignee_type = ANY (ARRAY['hr'::text, 'manager'::text, 'employee'::text, 'user'::text]))),
    CONSTRAINT lifecycle_task_status_check CHECK ((status = ANY (ARRAY['open'::text, 'done'::text, 'skipped'::text])))
);

--
-- Name: lifecycle_task_comment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_task_comment (
    id uuid DEFAULT uuidv7() NOT NULL,
    task_id uuid NOT NULL,
    author_id uuid NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lifecycle_task_comment_body_check CHECK (((length(body) >= 1) AND (length(body) <= 2000)))
);

--
-- Name: lifecycle_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_template (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    description text,
    branch_id uuid,
    org_unit_id uuid,
    is_default boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lifecycle_template_kind_check CHECK ((kind = ANY (ARRAY['onboarding'::text, 'offboarding'::text]))),
    CONSTRAINT lifecycle_template_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120)))
);

--
-- Name: lifecycle_template_task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_template_task (
    id uuid DEFAULT uuidv7() NOT NULL,
    template_id uuid NOT NULL,
    sort smallint DEFAULT 0 NOT NULL,
    title text NOT NULL,
    description text,
    department text,
    assignee_type text NOT NULL,
    assignee_user_id uuid,
    due_offset_days smallint DEFAULT 0 NOT NULL,
    link text,
    CONSTRAINT lifecycle_template_task_assignee_type_check CHECK ((assignee_type = ANY (ARRAY['hr'::text, 'manager'::text, 'employee'::text, 'user'::text]))),
    CONSTRAINT lifecycle_template_task_due_offset_days_check CHECK (((due_offset_days >= '-90'::integer) AND (due_offset_days <= 365))),
    CONSTRAINT lifecycle_template_task_title_check CHECK (((length(title) >= 1) AND (length(title) <= 160)))
);

--
-- Name: loan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loan (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    kind text DEFAULT 'loan'::text NOT NULL,
    principal numeric(14,2) NOT NULL,
    emi numeric(14,2) NOT NULL,
    start_bs_year smallint NOT NULL,
    start_bs_month smallint NOT NULL,
    installments integer NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    note text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT loan_kind_check CHECK ((kind = ANY (ARRAY['loan'::text, 'advance'::text]))),
    CONSTRAINT loan_status_check CHECK ((status = ANY (ARRAY['active'::text, 'closed'::text, 'paused'::text])))
);

--
-- Name: loan_installment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loan_installment (
    id uuid DEFAULT uuidv7() NOT NULL,
    loan_id uuid NOT NULL,
    payslip_id uuid NOT NULL,
    amount numeric(14,2) NOT NULL
);

--
-- Name: location; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.location (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    name text NOT NULL,
    lat numeric(9,6) NOT NULL,
    lng numeric(9,6) NOT NULL,
    radius_m integer DEFAULT 150 NOT NULL,
    address text,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: location_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.location_assignment (
    location_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    company_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: login_challenge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.login_challenge (
    token_hash text NOT NULL,
    user_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    used_at timestamp with time zone,
    ip text,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: lrn_attempt; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lrn_attempt (
    id uuid DEFAULT uuidv7() NOT NULL,
    enrollment_id uuid NOT NULL,
    answers jsonb NOT NULL,
    score numeric(5,2) NOT NULL,
    passed boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: lrn_course; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lrn_course (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    title text NOT NULL,
    description text,
    category text,
    delivery text DEFAULT 'classroom'::text NOT NULL,
    source text DEFAULT 'internal'::text NOT NULL,
    provider text,
    duration_hours numeric(6,2),
    url text,
    cost numeric(12,2) DEFAULT 0 NOT NULL,
    mandatory boolean DEFAULT false NOT NULL,
    mandatory_for text[] DEFAULT '{}'::text[] NOT NULL,
    validity_months smallint,
    pass_mark smallint,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lrn_course_cost_check CHECK ((cost >= (0)::numeric)),
    CONSTRAINT lrn_course_delivery_check CHECK ((delivery = ANY (ARRAY['online'::text, 'classroom'::text, 'blended'::text]))),
    CONSTRAINT lrn_course_pass_mark_check CHECK (((pass_mark >= 1) AND (pass_mark <= 100))),
    CONSTRAINT lrn_course_source_check CHECK ((source = ANY (ARRAY['internal'::text, 'external'::text]))),
    CONSTRAINT lrn_course_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200))),
    CONSTRAINT lrn_course_validity_months_check CHECK (((validity_months >= 1) AND (validity_months <= 120)))
);

--
-- Name: lrn_enrollment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lrn_enrollment (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    course_id uuid NOT NULL,
    session_id uuid,
    employee_id uuid NOT NULL,
    source text NOT NULL,
    status text NOT NULL,
    workflow_instance_id uuid,
    review_id uuid,
    reason text,
    assigned_by uuid,
    attended boolean,
    completed_at timestamp with time zone,
    score numeric(5,2),
    passed boolean,
    feedback_rating smallint,
    feedback_comment text,
    certificate_file_id uuid,
    certificate_issued_on date,
    certificate_expires_on date,
    expiry_reminded_at timestamp with time zone,
    cost numeric(12,2) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lrn_enrollment_feedback_rating_check CHECK (((feedback_rating >= 1) AND (feedback_rating <= 5))),
    CONSTRAINT lrn_enrollment_source_check CHECK ((source = ANY (ARRAY['self'::text, 'assigned'::text, 'review'::text, 'mandatory'::text]))),
    CONSTRAINT lrn_enrollment_status_check CHECK ((status = ANY (ARRAY['pending_approval'::text, 'enrolled'::text, 'completed'::text, 'rejected'::text, 'cancelled'::text, 'no_show'::text])))
);

--
-- Name: lrn_question; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lrn_question (
    id uuid DEFAULT uuidv7() NOT NULL,
    course_id uuid NOT NULL,
    prompt text NOT NULL,
    options jsonb NOT NULL,
    correct_index smallint NOT NULL,
    sort smallint DEFAULT 0 NOT NULL,
    CONSTRAINT lrn_question_correct_index_check CHECK ((correct_index >= 0))
);

--
-- Name: lrn_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lrn_session (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    course_id uuid NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    venue text,
    trainer text,
    capacity integer,
    status text DEFAULT 'scheduled'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lrn_session_capacity_check CHECK ((capacity > 0)),
    CONSTRAINT lrn_session_check CHECK ((ends_at > starts_at)),
    CONSTRAINT lrn_session_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'completed'::text, 'cancelled'::text])))
);

--
-- Name: notification_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_outbox (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid,
    user_id uuid,
    employee_id uuid,
    channel text NOT NULL,
    template text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    read_at timestamp with time zone,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    to_email text,
    CONSTRAINT notification_outbox_channel_check CHECK ((channel = ANY (ARRAY['in_app'::text, 'email'::text, 'sms'::text, 'push'::text, 'webhook'::text]))),
    CONSTRAINT notification_outbox_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'sent'::text, 'failed'::text])))
);

--
-- Name: org_unit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_unit (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    parent_id uuid,
    kind text NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    head_employee_id uuid,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT org_unit_kind_check CHECK ((kind = ANY (ARRAY['department'::text, 'team'::text])))
);

--
-- Name: ot_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ot_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    requested_min integer NOT NULL,
    approved_min integer,
    reason text,
    status text DEFAULT 'pending'::text NOT NULL,
    workflow_instance_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT ot_request_requested_min_check CHECK ((requested_min > 0)),
    CONSTRAINT ot_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: ot_slip; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ot_slip (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    attendance_day_id uuid,
    detected_min integer NOT NULL,
    approved_min integer,
    multiplier numeric(6,3) DEFAULT 1.5 NOT NULL,
    day_type text NOT NULL,
    status text DEFAULT 'detected'::text NOT NULL,
    posted_payslip_id uuid,
    comp_off_ledger_id uuid,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ot_slip_day_type_check CHECK ((day_type = ANY (ARRAY['working'::text, 'weekly_off'::text, 'holiday'::text]))),
    CONSTRAINT ot_slip_status_check CHECK ((status = ANY (ARRAY['detected'::text, 'approved'::text, 'rejected'::text, 'posted'::text, 'comp_off'::text])))
);

--
-- Name: payroll_hold; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_hold (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    run_id uuid NOT NULL,
    amount numeric(14,2) DEFAULT 0 NOT NULL,
    reason text NOT NULL,
    status text DEFAULT 'held'::text NOT NULL,
    release_mode text,
    release_run_id uuid,
    release_note text,
    held_by uuid,
    held_at timestamp with time zone DEFAULT now() NOT NULL,
    released_by uuid,
    released_at timestamp with time zone,
    CONSTRAINT payroll_hold_amount_check CHECK ((amount >= (0)::numeric)),
    CONSTRAINT payroll_hold_reason_check CHECK (((length(reason) >= 3) AND (length(reason) <= 500))),
    CONSTRAINT payroll_hold_release_mode_check CHECK ((release_mode = ANY (ARRAY['payroll'::text, 'outside'::text]))),
    CONSTRAINT payroll_hold_release_note_check CHECK ((length(release_note) <= 500)),
    CONSTRAINT payroll_hold_status_check CHECK ((status = ANY (ARRAY['held'::text, 'releasing'::text, 'released'::text, 'cancelled'::text])))
);

--
-- Name: payroll_input; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_input (
    id uuid DEFAULT uuidv7() NOT NULL,
    run_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    component_code text NOT NULL,
    amount numeric(14,2) NOT NULL,
    note text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: payroll_opening_balance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_opening_balance (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    fiscal_year_start integer NOT NULL,
    gross numeric(14,2) DEFAULT 0 NOT NULL,
    taxable numeric(14,2) DEFAULT 0 NOT NULL,
    tds_paid numeric(14,2) DEFAULT 0 NOT NULL,
    retirement_employee numeric(14,2) DEFAULT 0 NOT NULL,
    retirement_employer numeric(14,2) DEFAULT 0 NOT NULL,
    festival_paid boolean DEFAULT false NOT NULL,
    months smallint DEFAULT 0 NOT NULL,
    source text DEFAULT 'previous_employer'::text NOT NULL,
    note text,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payroll_opening_balance_fiscal_year_start_check CHECK (((fiscal_year_start >= 2070) AND (fiscal_year_start <= 2200))),
    CONSTRAINT payroll_opening_balance_gross_check CHECK ((gross >= (0)::numeric)),
    CONSTRAINT payroll_opening_balance_months_check CHECK (((months >= 0) AND (months <= 12))),
    CONSTRAINT payroll_opening_balance_note_check CHECK ((length(note) <= 500)),
    CONSTRAINT payroll_opening_balance_retirement_employee_check CHECK ((retirement_employee >= (0)::numeric)),
    CONSTRAINT payroll_opening_balance_retirement_employer_check CHECK ((retirement_employer >= (0)::numeric)),
    CONSTRAINT payroll_opening_balance_source_check CHECK ((source = ANY (ARRAY['previous_employer'::text, 'go_live'::text, 'other'::text]))),
    CONSTRAINT payroll_opening_balance_taxable_check CHECK ((taxable >= (0)::numeric)),
    CONSTRAINT payroll_opening_balance_tds_paid_check CHECK ((tds_paid >= (0)::numeric))
);

--
-- Name: payroll_period; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_period (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    bs_year smallint NOT NULL,
    bs_month smallint NOT NULL,
    start_ad date NOT NULL,
    end_ad date NOT NULL,
    days smallint NOT NULL,
    attendance_locked_at timestamp with time zone
);

--
-- Name: payroll_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_run (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    period_id uuid NOT NULL,
    run_type text DEFAULT 'regular'::text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    rounding_policy text DEFAULT 'half_up_rupee'::text NOT NULL,
    references_run_id uuid,
    preflight jsonb,
    variance jsonb,
    maker_id uuid,
    checker_id uuid,
    approval_signature text,
    computed_at timestamp with time zone,
    reviewed_at timestamp with time zone,
    approved_at timestamp with time zone,
    locked_at timestamp with time zone,
    disbursed_at timestamp with time zone,
    filed_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    totals jsonb,
    CONSTRAINT payroll_run_run_type_check CHECK ((run_type = ANY (ARRAY['regular'::text, 'off_cycle'::text, 'arrears'::text, 'supplementary'::text, 'final_settlement'::text, 'bonus'::text]))),
    CONSTRAINT payroll_run_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'preflight_failed'::text, 'computed'::text, 'reviewed'::text, 'approved'::text, 'locked'::text, 'disbursed'::text, 'filed'::text])))
);

--
-- Name: payslip; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payslip (
    id uuid DEFAULT uuidv7() NOT NULL,
    run_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    inputs_snapshot jsonb NOT NULL,
    rule_version_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    bs_days_in_month smallint NOT NULL,
    paid_days numeric(6,2) NOT NULL,
    lop_days numeric(6,2) DEFAULT 0 NOT NULL,
    ot_hours numeric(8,2) DEFAULT 0 NOT NULL,
    gross numeric(14,2) NOT NULL,
    taxable numeric(14,2) NOT NULL,
    tds numeric(14,2) NOT NULL,
    employee_statutory numeric(14,2) DEFAULT 0 NOT NULL,
    employer_statutory numeric(14,2) DEFAULT 0 NOT NULL,
    total_deductions numeric(14,2) NOT NULL,
    net numeric(14,2) NOT NULL,
    ytd jsonb DEFAULT '{}'::jsonb NOT NULL,
    explain jsonb DEFAULT '[]'::jsonb NOT NULL,
    flags jsonb DEFAULT '[]'::jsonb NOT NULL,
    published_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: payslip_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payslip_line (
    id uuid DEFAULT uuidv7() NOT NULL,
    payslip_id uuid NOT NULL,
    component_code text NOT NULL,
    type text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    amount numeric(14,2) NOT NULL,
    basis jsonb,
    sort_order integer DEFAULT 0 NOT NULL,
    CONSTRAINT payslip_line_type_check CHECK ((type = ANY (ARRAY['earning'::text, 'deduction'::text, 'employer_contribution'::text, 'statistical'::text])))
);

--
-- Name: perf_checkin; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_checkin (
    id uuid DEFAULT uuidv7() NOT NULL,
    goal_id uuid NOT NULL,
    key_result_id uuid,
    author_id uuid,
    value numeric(16,2),
    progress numeric(5,2),
    confidence smallint,
    comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_checkin_confidence_check CHECK (((confidence >= 1) AND (confidence <= 3)))
);

--
-- Name: perf_competency; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_competency (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    description text,
    indicators jsonb DEFAULT '[]'::jsonb NOT NULL,
    designations text[] DEFAULT '{}'::text[] NOT NULL,
    grades text[] DEFAULT '{}'::text[] NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_competency_kind_check CHECK ((kind = ANY (ARRAY['knowledge'::text, 'skill'::text, 'attitude'::text]))),
    CONSTRAINT perf_competency_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120)))
);

--
-- Name: perf_cycle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_cycle (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    goals_weight numeric(5,2) DEFAULT 60 NOT NULL,
    competency_weight numeric(5,2) DEFAULT 40 NOT NULL,
    rating_labels jsonb DEFAULT '["Needs improvement", "Partly meets", "Meets expectations", "Exceeds expectations", "Outstanding"]'::jsonb NOT NULL,
    peer_enabled boolean DEFAULT false NOT NULL,
    peer_min smallint DEFAULT 3 NOT NULL,
    self_due date,
    manager_due date,
    peer_due date,
    calibration_due date,
    filter jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    launched_at timestamp with time zone,
    released_at timestamp with time zone,
    CONSTRAINT perf_cycle_check CHECK ((period_end >= period_start)),
    CONSTRAINT perf_cycle_check1 CHECK (((goals_weight + competency_weight) = (100)::numeric)),
    CONSTRAINT perf_cycle_competency_weight_check CHECK (((competency_weight >= (0)::numeric) AND (competency_weight <= (100)::numeric))),
    CONSTRAINT perf_cycle_goals_weight_check CHECK (((goals_weight >= (0)::numeric) AND (goals_weight <= (100)::numeric))),
    CONSTRAINT perf_cycle_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120))),
    CONSTRAINT perf_cycle_peer_min_check CHECK (((peer_min >= 1) AND (peer_min <= 10))),
    CONSTRAINT perf_cycle_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'self_review'::text, 'manager_review'::text, 'peer_review'::text, 'calibration'::text, 'released'::text, 'closed'::text])))
);

--
-- Name: perf_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_feedback (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    giver_employee_id uuid NOT NULL,
    subject_employee_id uuid NOT NULL,
    requester_employee_id uuid,
    status text DEFAULT 'given'::text NOT NULL,
    prompt text,
    body text,
    private_to_manager boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    given_at timestamp with time zone,
    CONSTRAINT perf_feedback_check CHECK ((giver_employee_id <> subject_employee_id)),
    CONSTRAINT perf_feedback_check1 CHECK (((status <> 'given'::text) OR (body IS NOT NULL))),
    CONSTRAINT perf_feedback_status_check CHECK ((status = ANY (ARRAY['requested'::text, 'given'::text, 'declined'::text])))
);

--
-- Name: perf_goal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_goal (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    scope text NOT NULL,
    org_unit_id uuid,
    employee_id uuid,
    parent_id uuid,
    title text NOT NULL,
    description text,
    weight numeric(5,2) DEFAULT 0 NOT NULL,
    start_date date,
    due_date date,
    status text DEFAULT 'active'::text NOT NULL,
    progress numeric(5,2) DEFAULT 0 NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_goal_check CHECK (((scope <> 'individual'::text) OR (employee_id IS NOT NULL))),
    CONSTRAINT perf_goal_check1 CHECK (((scope <> 'department'::text) OR (org_unit_id IS NOT NULL))),
    CONSTRAINT perf_goal_progress_check CHECK (((progress >= (0)::numeric) AND (progress <= (100)::numeric))),
    CONSTRAINT perf_goal_scope_check CHECK ((scope = ANY (ARRAY['company'::text, 'department'::text, 'individual'::text]))),
    CONSTRAINT perf_goal_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'active'::text, 'completed'::text, 'cancelled'::text]))),
    CONSTRAINT perf_goal_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200))),
    CONSTRAINT perf_goal_weight_check CHECK (((weight >= (0)::numeric) AND (weight <= (100)::numeric)))
);

--
-- Name: perf_journal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_journal (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    author_id uuid NOT NULL,
    note_date date NOT NULL,
    tag text DEFAULT 'neutral'::text NOT NULL,
    note text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_journal_note_check CHECK (((length(note) >= 1) AND (length(note) <= 4000))),
    CONSTRAINT perf_journal_tag_check CHECK ((tag = ANY (ARRAY['positive'::text, 'improvement'::text, 'neutral'::text])))
);

--
-- Name: perf_key_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_key_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    goal_id uuid NOT NULL,
    title text NOT NULL,
    measure text DEFAULT 'number'::text NOT NULL,
    start_value numeric(16,2) DEFAULT 0 NOT NULL,
    target_value numeric(16,2) DEFAULT 100 NOT NULL,
    current_value numeric(16,2) DEFAULT 0 NOT NULL,
    unit text,
    weight numeric(6,2) DEFAULT 1 NOT NULL,
    sort smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_key_result_measure_check CHECK ((measure = ANY (ARRAY['number'::text, 'percent'::text, 'currency'::text, 'boolean'::text]))),
    CONSTRAINT perf_key_result_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200))),
    CONSTRAINT perf_key_result_weight_check CHECK ((weight > (0)::numeric))
);

--
-- Name: perf_kpi; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_kpi (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    designation text,
    title text NOT NULL,
    description text,
    measure text DEFAULT 'number'::text NOT NULL,
    default_target numeric(16,2) DEFAULT 100 NOT NULL,
    unit text,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_kpi_measure_check CHECK ((measure = ANY (ARRAY['number'::text, 'percent'::text, 'currency'::text, 'boolean'::text]))),
    CONSTRAINT perf_kpi_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200)))
);

--
-- Name: perf_one_on_one; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_one_on_one (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    manager_employee_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    cadence text DEFAULT 'none'::text NOT NULL,
    status text DEFAULT 'scheduled'::text NOT NULL,
    shared_notes text,
    manager_private_notes text,
    employee_private_notes text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_one_on_one_cadence_check CHECK ((cadence = ANY (ARRAY['none'::text, 'weekly'::text, 'biweekly'::text, 'monthly'::text]))),
    CONSTRAINT perf_one_on_one_check CHECK ((manager_employee_id <> employee_id)),
    CONSTRAINT perf_one_on_one_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'done'::text, 'cancelled'::text])))
);

--
-- Name: perf_one_on_one_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_one_on_one_item (
    id uuid DEFAULT uuidv7() NOT NULL,
    meeting_id uuid NOT NULL,
    kind text NOT NULL,
    text text NOT NULL,
    owner_employee_id uuid,
    done boolean DEFAULT false NOT NULL,
    added_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_one_on_one_item_kind_check CHECK ((kind = ANY (ARRAY['agenda'::text, 'action'::text]))),
    CONSTRAINT perf_one_on_one_item_text_check CHECK (((length(text) >= 1) AND (length(text) <= 1000)))
);

--
-- Name: perf_peer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_peer (
    id uuid DEFAULT uuidv7() NOT NULL,
    review_id uuid NOT NULL,
    reviewer_employee_id uuid NOT NULL,
    status text DEFAULT 'nominated'::text NOT NULL,
    rating smallint,
    strengths text,
    improvements text,
    nominated_by uuid,
    submitted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_peer_rating_check CHECK (((rating >= 1) AND (rating <= 5))),
    CONSTRAINT perf_peer_status_check CHECK ((status = ANY (ARRAY['nominated'::text, 'approved'::text, 'declined'::text, 'submitted'::text])))
);

--
-- Name: perf_reminder_sent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_reminder_sent (
    key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: perf_review; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_review (
    id uuid DEFAULT uuidv7() NOT NULL,
    cycle_id uuid NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    manager_employee_id uuid,
    self_summary text,
    self_submitted_at timestamp with time zone,
    self_score numeric(4,2),
    manager_summary text,
    manager_submitted_at timestamp with time zone,
    manager_score numeric(4,2),
    recommendation jsonb DEFAULT '{}'::jsonb NOT NULL,
    final_rating numeric(4,2),
    calibrated_by uuid,
    calibration_reason text,
    released_at timestamp with time zone,
    acknowledged_at timestamp with time zone,
    employee_comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: perf_review_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perf_review_item (
    id uuid DEFAULT uuidv7() NOT NULL,
    review_id uuid NOT NULL,
    item_type text NOT NULL,
    ref_id uuid NOT NULL,
    title text NOT NULL,
    detail text,
    weight numeric(6,2) DEFAULT 1 NOT NULL,
    self_rating smallint,
    self_comment text,
    manager_rating smallint,
    manager_comment text,
    CONSTRAINT perf_review_item_item_type_check CHECK ((item_type = ANY (ARRAY['goal'::text, 'competency'::text]))),
    CONSTRAINT perf_review_item_manager_rating_check CHECK (((manager_rating >= 1) AND (manager_rating <= 5))),
    CONSTRAINT perf_review_item_self_rating_check CHECK (((self_rating >= 1) AND (self_rating <= 5)))
);

--
-- Name: policy_ack; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_ack (
    policy_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    acknowledged_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: policy_nudge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_nudge (
    policy_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    last_sent_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: probation_review; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.probation_review (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    probation_end date,
    rating smallint NOT NULL,
    recommendation text NOT NULL,
    extend_to date,
    last_working_day date,
    comments text NOT NULL,
    submitted_by uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT probation_review_check CHECK (((recommendation <> 'extend'::text) OR (extend_to IS NOT NULL))),
    CONSTRAINT probation_review_check1 CHECK (((recommendation <> 'terminate'::text) OR (last_working_day IS NOT NULL))),
    CONSTRAINT probation_review_comments_check CHECK (((length(comments) >= 1) AND (length(comments) <= 4000))),
    CONSTRAINT probation_review_rating_check CHECK (((rating >= 1) AND (rating <= 5))),
    CONSTRAINT probation_review_recommendation_check CHECK ((recommendation = ANY (ARRAY['confirm'::text, 'extend'::text, 'terminate'::text]))),
    CONSTRAINT probation_review_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: profile_change_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.profile_change_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    requested_by uuid NOT NULL,
    section text NOT NULL,
    payload_enc bytea NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    reason text,
    proof_file_id uuid,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_by uuid,
    decided_at timestamp with time zone,
    decision_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT profile_change_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: punch_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event (
    id uuid DEFAULT uuidv7() NOT NULL,
    device_id uuid,
    device_sn text NOT NULL,
    vendor text NOT NULL,
    device_user_pin text NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone NOT NULL,
    device_local_time text NOT NULL,
    local_date date NOT NULL,
    direction text NOT NULL,
    verify_mode text NOT NULL,
    source text NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
)
PARTITION BY RANGE (punched_at);

--
-- Name: punch_event_2026_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_01 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_02 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_03 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_04 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_05 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_06 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_07 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_08 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_09 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_10 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_11 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2026_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2026_12 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_01 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_02 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_03 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_04 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_05 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_06 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_07 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_08 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_09 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_10 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_11 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2027_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2027_12 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_01 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_02 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_03 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_04 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_05 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_06 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_07 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_08 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_09 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_10 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_11 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2028_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2028_12 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_01 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_02 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_03 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_04 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_05 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_06 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_07 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_08 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_09 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_10 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_11 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2029_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2029_12 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_01 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_02 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_03 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_04 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_05 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_06 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_07 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_08 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_09 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_10 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_11 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_2030_12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_2030_12 (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_event_default; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_event_default (
    id uuid DEFAULT uuidv7() CONSTRAINT punch_event_id_not_null NOT NULL,
    device_id uuid,
    device_sn text CONSTRAINT punch_event_device_sn_not_null NOT NULL,
    vendor text CONSTRAINT punch_event_vendor_not_null NOT NULL,
    device_user_pin text CONSTRAINT punch_event_device_user_pin_not_null NOT NULL,
    employee_id uuid,
    punched_at timestamp with time zone CONSTRAINT punch_event_punched_at_not_null NOT NULL,
    device_local_time text CONSTRAINT punch_event_device_local_time_not_null NOT NULL,
    local_date date CONSTRAINT punch_event_local_date_not_null NOT NULL,
    direction text CONSTRAINT punch_event_direction_not_null NOT NULL,
    verify_mode text CONSTRAINT punch_event_verify_mode_not_null NOT NULL,
    source text CONSTRAINT punch_event_source_not_null NOT NULL,
    raw jsonb,
    gateway_signature text,
    idempotency_key text CONSTRAINT punch_event_idempotency_key_not_null NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() CONSTRAINT punch_event_ingested_at_not_null NOT NULL,
    supersedes_id uuid,
    CONSTRAINT punch_event_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text, 'unknown'::text])))
);

--
-- Name: punch_photo; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.punch_photo (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    punch_id uuid NOT NULL,
    punched_at timestamp with time zone NOT NULL,
    local_date date NOT NULL,
    direction text NOT NULL,
    mime text NOT NULL,
    bytes integer NOT NULL,
    image_enc bytea,
    purged_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT punch_photo_bytes_check CHECK ((bytes > 0)),
    CONSTRAINT punch_photo_mime_check CHECK ((mime = ANY (ARRAY['image/jpeg'::text, 'image/png'::text, 'image/webp'::text])))
);

--
-- Name: recompute_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recompute_queue (
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text
);

--
-- Name: registered_device; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registered_device (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    label text NOT NULL,
    user_agent text,
    status text DEFAULT 'pending'::text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by uuid,
    decided_at timestamp with time zone,
    last_used_at timestamp with time zone,
    CONSTRAINT registered_device_label_check CHECK (((length(label) >= 1) AND (length(label) <= 80))),
    CONSTRAINT registered_device_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'revoked'::text])))
);

--
-- Name: regularisation_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regularisation_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    kind text NOT NULL,
    punch_time timestamp with time zone,
    punch_direction text,
    shift_id uuid,
    reason text NOT NULL,
    attachment_key text,
    status text DEFAULT 'pending'::text NOT NULL,
    workflow_instance_id uuid,
    resulting_punch_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT regularisation_request_kind_check CHECK ((kind = ANY (ARRAY['missed_punch'::text, 'wfh'::text, 'on_duty'::text, 'half_day'::text, 'shift_change'::text]))),
    CONSTRAINT regularisation_request_punch_direction_check CHECK ((punch_direction = ANY (ARRAY['in'::text, 'out'::text]))),
    CONSTRAINT regularisation_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: relations_reminder_sent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.relations_reminder_sent (
    company_id uuid NOT NULL,
    kind text NOT NULL,
    ref text NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: roster; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roster (
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    shift_id uuid
);

--
-- Name: rule_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rule_version (
    id uuid DEFAULT uuidv7() NOT NULL,
    rule_key text NOT NULL,
    jurisdiction text DEFAULT 'NP'::text NOT NULL,
    valid_from_ad date NOT NULL,
    valid_to_ad date,
    valid_from_bs text NOT NULL,
    valid_to_bs text,
    value jsonb NOT NULL,
    source_url text,
    note text,
    approved_by uuid,
    approved_at timestamp with time zone,
    supersedes_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: salary_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.salary_assignment (
    employee_id uuid NOT NULL,
    structure_version_id uuid NOT NULL,
    valid daterange NOT NULL,
    base_amounts jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: salary_component; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.salary_component (
    code text NOT NULL,
    company_id uuid NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    type text NOT NULL,
    formula text,
    condition text,
    depends_on_payment_days boolean DEFAULT false NOT NULL,
    taxable boolean DEFAULT true NOT NULL,
    statutory boolean DEFAULT false NOT NULL,
    rounding text DEFAULT 'paisa'::text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT salary_component_rounding_check CHECK ((rounding = ANY (ARRAY['paisa'::text, 'rupee'::text]))),
    CONSTRAINT salary_component_type_check CHECK ((type = ANY (ARRAY['earning'::text, 'deduction'::text, 'employer_contribution'::text, 'statistical'::text])))
);

--
-- Name: salary_structure; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.salary_structure (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL
);

--
-- Name: salary_structure_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.salary_structure_version (
    id uuid DEFAULT uuidv7() NOT NULL,
    structure_id uuid NOT NULL,
    version integer NOT NULL,
    components jsonb NOT NULL,
    frozen_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: saved_report; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_report (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    shared boolean DEFAULT false NOT NULL,
    definition jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT saved_report_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120)))
);

--
-- Name: session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session (
    token_hash text NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    ip text,
    user_agent text,
    id uuid DEFAULT uuidv7() NOT NULL,
    two_factor boolean DEFAULT false NOT NULL
);

--
-- Name: shift; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shift (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_ne text,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    grace_in_min smallint DEFAULT 0 NOT NULL,
    grace_out_min smallint DEFAULT 0 NOT NULL,
    early_in_window_min smallint DEFAULT 60 NOT NULL,
    late_out_window_min smallint DEFAULT 360 NOT NULL,
    break_minutes smallint DEFAULT 0 NOT NULL,
    half_day_threshold_min smallint DEFAULT 240 NOT NULL,
    absent_threshold_min smallint DEFAULT 120 NOT NULL,
    pairing_mode text DEFAULT 'alternating'::text NOT NULL,
    rounding_min smallint DEFAULT 0 NOT NULL,
    rounding_direction text DEFAULT 'nearest'::text NOT NULL,
    CONSTRAINT shift_pairing_mode_check CHECK ((pairing_mode = ANY (ARRAY['alternating'::text, 'strict_log_type'::text, 'first_last'::text]))),
    CONSTRAINT shift_rounding_direction_check CHECK ((rounding_direction = ANY (ARRAY['nearest'::text, 'down'::text, 'up'::text])))
);

--
-- Name: shift_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shift_assignment (
    employee_id uuid NOT NULL,
    shift_id uuid NOT NULL,
    valid daterange NOT NULL
);

--
-- Name: shift_swap; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shift_swap (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    requester_id uuid NOT NULL,
    colleague_id uuid NOT NULL,
    local_date date NOT NULL,
    requester_shift_id uuid,
    colleague_shift_id uuid,
    reason text,
    status text DEFAULT 'awaiting_colleague'::text NOT NULL,
    colleague_note text,
    workflow_instance_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT shift_swap_check CHECK ((requester_id <> colleague_id)),
    CONSTRAINT shift_swap_colleague_note_check CHECK ((length(colleague_note) <= 500)),
    CONSTRAINT shift_swap_reason_check CHECK ((length(reason) <= 500)),
    CONSTRAINT shift_swap_status_check CHECK ((status = ANY (ARRAY['awaiting_colleague'::text, 'pending'::text, 'approved'::text, 'rejected'::text, 'declined'::text, 'cancelled'::text])))
);

--
-- Name: stored_file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stored_file (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    purpose text NOT NULL,
    filename text NOT NULL,
    mime text NOT NULL,
    bytes integer NOT NULL,
    sha256 text NOT NULL,
    data_enc bytea,
    uploaded_by uuid,
    attached_to text,
    attached_at timestamp with time zone,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT stored_file_bytes_check CHECK ((bytes > 0)),
    CONSTRAINT stored_file_filename_check CHECK (((length(filename) >= 1) AND (length(filename) <= 200)))
);

--
-- Name: suggestion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.suggestion (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    category text DEFAULT 'general'::text NOT NULL,
    body text NOT NULL,
    received_on date NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    reply text,
    is_public boolean DEFAULT false NOT NULL,
    replied_by uuid,
    replied_at timestamp with time zone,
    CONSTRAINT suggestion_body_check CHECK (((length(body) >= 5) AND (length(body) <= 3000))),
    CONSTRAINT suggestion_category_check CHECK ((category = ANY (ARRAY['general'::text, 'workplace'::text, 'process'::text, 'benefits'::text, 'culture'::text, 'other'::text]))),
    CONSTRAINT suggestion_reply_check CHECK ((length(reply) <= 3000)),
    CONSTRAINT suggestion_status_check CHECK ((status = ANY (ARRAY['new'::text, 'reviewed'::text, 'answered'::text, 'archived'::text])))
);

--
-- Name: survey; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    kind text DEFAULT 'survey'::text NOT NULL,
    title text NOT NULL,
    description text,
    anonymous boolean DEFAULT true NOT NULL,
    min_group smallint DEFAULT 5 NOT NULL,
    audience_kind text DEFAULT 'all'::text NOT NULL,
    audience_id uuid,
    status text DEFAULT 'draft'::text NOT NULL,
    opens_on date NOT NULL,
    closes_on date NOT NULL,
    recurrence text DEFAULT 'none'::text NOT NULL,
    series_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    opened_notified_at timestamp with time zone,
    reminded_at timestamp with time zone,
    closed_at timestamp with time zone,
    CONSTRAINT survey_audience_kind_check CHECK ((audience_kind = ANY (ARRAY['all'::text, 'branch'::text, 'department'::text]))),
    CONSTRAINT survey_check CHECK ((closes_on >= opens_on)),
    CONSTRAINT survey_description_check CHECK ((length(description) <= 2000)),
    CONSTRAINT survey_kind_check CHECK ((kind = ANY (ARRAY['survey'::text, 'poll'::text]))),
    CONSTRAINT survey_min_group_check CHECK (((min_group >= 3) AND (min_group <= 50))),
    CONSTRAINT survey_recurrence_check CHECK ((recurrence = ANY (ARRAY['none'::text, 'weekly'::text, 'monthly'::text]))),
    CONSTRAINT survey_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'published'::text, 'closed'::text]))),
    CONSTRAINT survey_title_check CHECK (((length(title) >= 1) AND (length(title) <= 160)))
);

--
-- Name: survey_answer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_answer (
    response_id uuid NOT NULL,
    question_id uuid NOT NULL,
    value_num smallint,
    choices text[],
    text_value text,
    CONSTRAINT survey_answer_text_value_check CHECK ((length(text_value) <= 2000))
);

--
-- Name: survey_question; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_question (
    id uuid DEFAULT uuidv7() NOT NULL,
    survey_id uuid NOT NULL,
    "position" smallint NOT NULL,
    kind text NOT NULL,
    prompt text NOT NULL,
    options jsonb DEFAULT '[]'::jsonb NOT NULL,
    required boolean DEFAULT true NOT NULL,
    CONSTRAINT survey_question_kind_check CHECK ((kind = ANY (ARRAY['rating'::text, 'enps'::text, 'single'::text, 'multi'::text, 'text'::text]))),
    CONSTRAINT survey_question_prompt_check CHECK (((length(prompt) >= 1) AND (length(prompt) <= 300)))
);

--
-- Name: survey_response; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_response (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    survey_id uuid NOT NULL,
    respondent_key text NOT NULL,
    employee_id uuid,
    org_unit_id uuid,
    branch_id uuid,
    submitted_on date NOT NULL
);

--
-- Name: tada_rate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tada_rate (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    grade text NOT NULL,
    destination text NOT NULL,
    da_rate numeric(14,2) NOT NULL,
    lodging_rate numeric(14,2) NOT NULL,
    CONSTRAINT tada_rate_da_rate_check CHECK ((da_rate >= (0)::numeric)),
    CONSTRAINT tada_rate_destination_check CHECK ((destination = ANY (ARRAY['valley'::text, 'city'::text, 'rural'::text, 'international'::text]))),
    CONSTRAINT tada_rate_grade_check CHECK (((length(grade) >= 1) AND (length(grade) <= 40))),
    CONSTRAINT tada_rate_lodging_rate_check CHECK ((lodging_rate >= (0)::numeric))
);

--
-- Name: task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    board_id uuid CONSTRAINT task_board_id_not_null1 NOT NULL,
    column_id text NOT NULL,
    title text NOT NULL,
    description text,
    priority text DEFAULT 'normal'::text NOT NULL,
    due_date date,
    labels text[] DEFAULT '{}'::text[] NOT NULL,
    "position" double precision DEFAULT 0 NOT NULL,
    created_by uuid,
    completed_at timestamp with time zone,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_description_check CHECK ((length(description) <= 5000)),
    CONSTRAINT task_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'normal'::text, 'high'::text, 'urgent'::text]))),
    CONSTRAINT task_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200)))
);

--
-- Name: task_assignee; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_assignee (
    task_id uuid NOT NULL,
    user_id uuid NOT NULL
);

--
-- Name: task_attachment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_attachment (
    id uuid DEFAULT uuidv7() NOT NULL,
    task_id uuid NOT NULL,
    file_id uuid NOT NULL,
    added_by uuid,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: task_board; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_board (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    owner_user_id uuid NOT NULL,
    columns jsonb NOT NULL,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_board_kind_check CHECK ((kind = ANY (ARRAY['personal'::text, 'team'::text]))),
    CONSTRAINT task_board_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: task_board_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_board_member (
    board_id uuid NOT NULL,
    user_id uuid NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: task_checklist_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_checklist_item (
    id uuid DEFAULT uuidv7() NOT NULL,
    task_id uuid NOT NULL,
    text text NOT NULL,
    done boolean DEFAULT false NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    CONSTRAINT task_checklist_item_text_check CHECK (((length(text) >= 1) AND (length(text) <= 300)))
);

--
-- Name: task_comment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_comment (
    id uuid DEFAULT uuidv7() NOT NULL,
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_comment_body_check CHECK (((length(body) >= 1) AND (length(body) <= 2000)))
);

--
-- Name: task_reminder_sent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_reminder_sent (
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    local_date date NOT NULL
);

--
-- Name: tax_declaration; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_declaration (
    id uuid DEFAULT uuidv7() NOT NULL,
    employee_id uuid NOT NULL,
    fiscal_year_start integer NOT NULL,
    life_premium numeric(14,2) DEFAULT 0 NOT NULL,
    health_premium numeric(14,2) DEFAULT 0 NOT NULL,
    cit_monthly numeric(14,2) DEFAULT 0 NOT NULL,
    medical_cost numeric(14,2) DEFAULT 0 NOT NULL,
    other_retirement_annual numeric(14,2) DEFAULT 0 NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    verified_by uuid,
    verified_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    donation numeric(14,2) DEFAULT 0 NOT NULL,
    cit_from_month smallint,
    cit_prev_monthly numeric(14,2) DEFAULT 0 NOT NULL,
    CONSTRAINT tax_declaration_cit_from_month_check CHECK (((cit_from_month >= 1) AND (cit_from_month <= 12))),
    CONSTRAINT tax_declaration_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'submitted'::text, 'verified'::text, 'rejected'::text])))
);

--
-- Name: tax_declaration_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_declaration_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    fiscal_year_start integer NOT NULL,
    life_premium numeric(14,2) DEFAULT 0 NOT NULL,
    health_premium numeric(14,2) DEFAULT 0 NOT NULL,
    cit_monthly numeric(14,2) DEFAULT 0 NOT NULL,
    medical_cost numeric(14,2) DEFAULT 0 NOT NULL,
    other_retirement_annual numeric(14,2) DEFAULT 0 NOT NULL,
    note text,
    proof_file_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    workflow_instance_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by uuid,
    decided_at timestamp with time zone,
    decision_note text,
    donation numeric(14,2) DEFAULT 0 NOT NULL,
    CONSTRAINT tax_declaration_request_cit_monthly_check CHECK ((cit_monthly >= (0)::numeric)),
    CONSTRAINT tax_declaration_request_health_premium_check CHECK ((health_premium >= (0)::numeric)),
    CONSTRAINT tax_declaration_request_life_premium_check CHECK ((life_premium >= (0)::numeric)),
    CONSTRAINT tax_declaration_request_medical_cost_check CHECK ((medical_cost >= (0)::numeric)),
    CONSTRAINT tax_declaration_request_other_retirement_annual_check CHECK ((other_retirement_annual >= (0)::numeric)),
    CONSTRAINT tax_declaration_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: time_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_activity (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    archived boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT time_activity_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: time_client; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_client (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    notes text,
    archived boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT time_client_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120)))
);

--
-- Name: time_entry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_entry (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    duration_min integer,
    project_id uuid,
    activity_id uuid,
    note text,
    billable boolean DEFAULT false NOT NULL,
    source text NOT NULL,
    auto_stopped boolean DEFAULT false NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT time_entry_check CHECK ((((ended_at IS NULL) AND (duration_min IS NULL) AND (started_at IS NOT NULL)) OR (duration_min IS NOT NULL))),
    CONSTRAINT time_entry_check1 CHECK (((ended_at IS NULL) OR (started_at IS NULL) OR (ended_at > started_at))),
    CONSTRAINT time_entry_duration_min_check CHECK (((duration_min IS NULL) OR ((duration_min >= 1) AND (duration_min <= 1440)))),
    CONSTRAINT time_entry_note_check CHECK (((note IS NULL) OR (length(note) <= 500))),
    CONSTRAINT time_entry_source_check CHECK ((source = ANY (ARRAY['timer'::text, 'manual'::text])))
);

--
-- Name: time_project; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_project (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    client_id uuid,
    code text NOT NULL,
    name text NOT NULL,
    color text DEFAULT '#12773a'::text NOT NULL,
    billable boolean DEFAULT true NOT NULL,
    bill_rate numeric(12,2),
    budget_hours numeric(10,2),
    all_members boolean DEFAULT true NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT time_project_bill_rate_check CHECK (((bill_rate IS NULL) OR (bill_rate >= (0)::numeric))),
    CONSTRAINT time_project_budget_hours_check CHECK (((budget_hours IS NULL) OR (budget_hours > (0)::numeric))),
    CONSTRAINT time_project_code_check CHECK ((code ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$'::text)),
    CONSTRAINT time_project_color_check CHECK ((color ~ '^#[0-9a-fA-F]{6}$'::text)),
    CONSTRAINT time_project_name_check CHECK (((length(name) >= 1) AND (length(name) <= 120))),
    CONSTRAINT time_project_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);

--
-- Name: time_project_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_project_member (
    project_id uuid NOT NULL,
    employee_id uuid NOT NULL
);

--
-- Name: timesheet; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.timesheet (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    week_start date NOT NULL,
    status text DEFAULT 'submitted'::text NOT NULL,
    total_min integer DEFAULT 0 NOT NULL,
    note text,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_by uuid,
    decided_at timestamp with time zone,
    decided_by uuid,
    decision_comment text,
    workflow_instance_id uuid,
    CONSTRAINT timesheet_note_check CHECK (((note IS NULL) OR (length(note) <= 500))),
    CONSTRAINT timesheet_status_check CHECK ((status = ANY (ARRAY['submitted'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text]))),
    CONSTRAINT timesheet_week_start_check CHECK ((EXTRACT(dow FROM week_start) = (0)::numeric))
);

--
-- Name: travel_bill; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.travel_bill (
    id uuid DEFAULT uuidv7() NOT NULL,
    settlement_id uuid NOT NULL,
    kind text NOT NULL,
    bill_date date NOT NULL,
    description text NOT NULL,
    amount numeric(14,2) NOT NULL,
    file_id uuid,
    CONSTRAINT travel_bill_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT travel_bill_description_check CHECK (((length(description) >= 1) AND (length(description) <= 300))),
    CONSTRAINT travel_bill_kind_check CHECK ((kind = ANY (ARRAY['transport'::text, 'lodging'::text, 'food'::text, 'other'::text])))
);

--
-- Name: travel_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.travel_request (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    destination text NOT NULL,
    destination_category text NOT NULL,
    purpose text NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    mode text NOT NULL,
    estimated_cost numeric(14,2) DEFAULT 0 NOT NULL,
    advance_requested numeric(14,2) DEFAULT 0 NOT NULL,
    advance_approved numeric(14,2),
    advance_method text,
    advance_status text DEFAULT 'none'::text NOT NULL,
    fx_rate numeric(10,4),
    days integer NOT NULL,
    nights integer NOT NULL,
    da_rate numeric(14,2) NOT NULL,
    lodging_rate numeric(14,2) NOT NULL,
    da_amount numeric(14,2) NOT NULL,
    lodging_limit numeric(14,2) NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    workflow_instance_id uuid,
    decided_at timestamp with time zone,
    decision_note text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT travel_request_advance_method_check CHECK ((advance_method = ANY (ARRAY['payroll'::text, 'cash'::text]))),
    CONSTRAINT travel_request_advance_requested_check CHECK ((advance_requested >= (0)::numeric)),
    CONSTRAINT travel_request_advance_status_check CHECK ((advance_status = ANY (ARRAY['none'::text, 'pending'::text, 'paid'::text]))),
    CONSTRAINT travel_request_check CHECK ((to_date >= from_date)),
    CONSTRAINT travel_request_days_check CHECK ((days > 0)),
    CONSTRAINT travel_request_destination_category_check CHECK ((destination_category = ANY (ARRAY['valley'::text, 'city'::text, 'rural'::text, 'international'::text]))),
    CONSTRAINT travel_request_destination_check CHECK (((length(destination) >= 1) AND (length(destination) <= 120))),
    CONSTRAINT travel_request_estimated_cost_check CHECK ((estimated_cost >= (0)::numeric)),
    CONSTRAINT travel_request_fx_rate_check CHECK ((fx_rate > (0)::numeric)),
    CONSTRAINT travel_request_mode_check CHECK ((mode = ANY (ARRAY['bus'::text, 'air'::text, 'office_vehicle'::text, 'own_vehicle'::text, 'other'::text]))),
    CONSTRAINT travel_request_nights_check CHECK ((nights >= 0)),
    CONSTRAINT travel_request_purpose_check CHECK (((length(purpose) >= 3) AND (length(purpose) <= 1000))),
    CONSTRAINT travel_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text, 'settling'::text, 'settled'::text])))
);

--
-- Name: travel_settlement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.travel_settlement (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    travel_request_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    actual_from date NOT NULL,
    actual_to date NOT NULL,
    days integer NOT NULL,
    nights integer NOT NULL,
    da_amount numeric(14,2) NOT NULL,
    lodging_claimed numeric(14,2) NOT NULL,
    lodging_allowed numeric(14,2) NOT NULL,
    other_bills numeric(14,2) NOT NULL,
    entitlement numeric(14,2) NOT NULL,
    advance numeric(14,2) NOT NULL,
    net numeric(14,2) NOT NULL,
    note text,
    workflow_instance_id uuid,
    decided_at timestamp with time zone,
    decision_note text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT travel_settlement_check CHECK ((actual_to >= actual_from)),
    CONSTRAINT travel_settlement_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: user_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_group (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_group_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80)))
);

--
-- Name: user_group_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_group_member (
    group_id uuid NOT NULL,
    user_id uuid NOT NULL
);

--
-- Name: user_recovery_code; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_recovery_code (
    id uuid DEFAULT uuidv7() NOT NULL,
    user_id uuid NOT NULL,
    code_hash text NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: v_attendance_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_attendance_latest AS
 SELECT DISTINCT ON (employee_id, local_date) id,
    employee_id,
    local_date,
    bs_date,
    version,
    shift_id,
    status,
    first_in,
    last_out,
    worked_min,
    break_min,
    late_min,
    early_min,
    ot_min,
    ot,
    flags,
    punch_ids,
    inputs_hash,
    computed_at,
    locked_at,
    ot_slip_id
   FROM public.attendance_day
  ORDER BY employee_id, local_date, version DESC;

--
-- Name: work_break; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_break (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    local_date date NOT NULL,
    kind text NOT NULL,
    paid boolean NOT NULL,
    max_min integer NOT NULL,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    minutes integer,
    over_limit boolean DEFAULT false NOT NULL,
    source text DEFAULT 'web'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT work_break_check CHECK (((ended_at IS NULL) OR (ended_at >= started_at))),
    CONSTRAINT work_break_kind_check CHECK ((kind = ANY (ARRAY['lunch'::text, 'tea'::text, 'prayer'::text, 'personal'::text]))),
    CONSTRAINT work_break_max_min_check CHECK ((max_min > 0)),
    CONSTRAINT work_break_minutes_check CHECK ((minutes >= 0)),
    CONSTRAINT work_break_source_check CHECK ((source = ANY (ARRAY['web'::text, 'kiosk'::text, 'auto'::text])))
);

--
-- Name: workflow_definition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_definition (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    object_type text NOT NULL,
    steps jsonb NOT NULL,
    sla_hours integer DEFAULT 24 NOT NULL,
    escalation_hours integer DEFAULT 48 NOT NULL
);

--
-- Name: workflow_instance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_instance (
    id uuid DEFAULT uuidv7() NOT NULL,
    company_id uuid NOT NULL,
    object_type text NOT NULL,
    object_id uuid NOT NULL,
    employee_id uuid,
    steps jsonb NOT NULL,
    current_step integer DEFAULT 0 NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    routing jsonb DEFAULT '[]'::jsonb NOT NULL,
    amount numeric(14,2),
    flow_id uuid,
    CONSTRAINT workflow_instance_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);

--
-- Name: punch_event_2026_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_01 FOR VALUES FROM ('2026-01-01 00:00:00+05:45') TO ('2026-02-01 00:00:00+05:45');

--
-- Name: punch_event_2026_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_02 FOR VALUES FROM ('2026-02-01 00:00:00+05:45') TO ('2026-03-01 00:00:00+05:45');

--
-- Name: punch_event_2026_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_03 FOR VALUES FROM ('2026-03-01 00:00:00+05:45') TO ('2026-04-01 00:00:00+05:45');

--
-- Name: punch_event_2026_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_04 FOR VALUES FROM ('2026-04-01 00:00:00+05:45') TO ('2026-05-01 00:00:00+05:45');

--
-- Name: punch_event_2026_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_05 FOR VALUES FROM ('2026-05-01 00:00:00+05:45') TO ('2026-06-01 00:00:00+05:45');

--
-- Name: punch_event_2026_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_06 FOR VALUES FROM ('2026-06-01 00:00:00+05:45') TO ('2026-07-01 00:00:00+05:45');

--
-- Name: punch_event_2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+05:45') TO ('2026-08-01 00:00:00+05:45');

--
-- Name: punch_event_2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+05:45') TO ('2026-09-01 00:00:00+05:45');

--
-- Name: punch_event_2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+05:45') TO ('2026-10-01 00:00:00+05:45');

--
-- Name: punch_event_2026_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_10 FOR VALUES FROM ('2026-10-01 00:00:00+05:45') TO ('2026-11-01 00:00:00+05:45');

--
-- Name: punch_event_2026_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_11 FOR VALUES FROM ('2026-11-01 00:00:00+05:45') TO ('2026-12-01 00:00:00+05:45');

--
-- Name: punch_event_2026_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2026_12 FOR VALUES FROM ('2026-12-01 00:00:00+05:45') TO ('2027-01-01 00:00:00+05:45');

--
-- Name: punch_event_2027_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_01 FOR VALUES FROM ('2027-01-01 00:00:00+05:45') TO ('2027-02-01 00:00:00+05:45');

--
-- Name: punch_event_2027_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_02 FOR VALUES FROM ('2027-02-01 00:00:00+05:45') TO ('2027-03-01 00:00:00+05:45');

--
-- Name: punch_event_2027_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_03 FOR VALUES FROM ('2027-03-01 00:00:00+05:45') TO ('2027-04-01 00:00:00+05:45');

--
-- Name: punch_event_2027_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_04 FOR VALUES FROM ('2027-04-01 00:00:00+05:45') TO ('2027-05-01 00:00:00+05:45');

--
-- Name: punch_event_2027_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_05 FOR VALUES FROM ('2027-05-01 00:00:00+05:45') TO ('2027-06-01 00:00:00+05:45');

--
-- Name: punch_event_2027_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_06 FOR VALUES FROM ('2027-06-01 00:00:00+05:45') TO ('2027-07-01 00:00:00+05:45');

--
-- Name: punch_event_2027_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_07 FOR VALUES FROM ('2027-07-01 00:00:00+05:45') TO ('2027-08-01 00:00:00+05:45');

--
-- Name: punch_event_2027_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_08 FOR VALUES FROM ('2027-08-01 00:00:00+05:45') TO ('2027-09-01 00:00:00+05:45');

--
-- Name: punch_event_2027_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_09 FOR VALUES FROM ('2027-09-01 00:00:00+05:45') TO ('2027-10-01 00:00:00+05:45');

--
-- Name: punch_event_2027_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_10 FOR VALUES FROM ('2027-10-01 00:00:00+05:45') TO ('2027-11-01 00:00:00+05:45');

--
-- Name: punch_event_2027_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_11 FOR VALUES FROM ('2027-11-01 00:00:00+05:45') TO ('2027-12-01 00:00:00+05:45');

--
-- Name: punch_event_2027_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2027_12 FOR VALUES FROM ('2027-12-01 00:00:00+05:45') TO ('2028-01-01 00:00:00+05:45');

--
-- Name: punch_event_2028_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_01 FOR VALUES FROM ('2028-01-01 00:00:00+05:45') TO ('2028-02-01 00:00:00+05:45');

--
-- Name: punch_event_2028_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_02 FOR VALUES FROM ('2028-02-01 00:00:00+05:45') TO ('2028-03-01 00:00:00+05:45');

--
-- Name: punch_event_2028_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_03 FOR VALUES FROM ('2028-03-01 00:00:00+05:45') TO ('2028-04-01 00:00:00+05:45');

--
-- Name: punch_event_2028_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_04 FOR VALUES FROM ('2028-04-01 00:00:00+05:45') TO ('2028-05-01 00:00:00+05:45');

--
-- Name: punch_event_2028_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_05 FOR VALUES FROM ('2028-05-01 00:00:00+05:45') TO ('2028-06-01 00:00:00+05:45');

--
-- Name: punch_event_2028_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_06 FOR VALUES FROM ('2028-06-01 00:00:00+05:45') TO ('2028-07-01 00:00:00+05:45');

--
-- Name: punch_event_2028_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_07 FOR VALUES FROM ('2028-07-01 00:00:00+05:45') TO ('2028-08-01 00:00:00+05:45');

--
-- Name: punch_event_2028_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_08 FOR VALUES FROM ('2028-08-01 00:00:00+05:45') TO ('2028-09-01 00:00:00+05:45');

--
-- Name: punch_event_2028_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_09 FOR VALUES FROM ('2028-09-01 00:00:00+05:45') TO ('2028-10-01 00:00:00+05:45');

--
-- Name: punch_event_2028_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_10 FOR VALUES FROM ('2028-10-01 00:00:00+05:45') TO ('2028-11-01 00:00:00+05:45');

--
-- Name: punch_event_2028_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_11 FOR VALUES FROM ('2028-11-01 00:00:00+05:45') TO ('2028-12-01 00:00:00+05:45');

--
-- Name: punch_event_2028_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2028_12 FOR VALUES FROM ('2028-12-01 00:00:00+05:45') TO ('2029-01-01 00:00:00+05:45');

--
-- Name: punch_event_2029_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_01 FOR VALUES FROM ('2029-01-01 00:00:00+05:45') TO ('2029-02-01 00:00:00+05:45');

--
-- Name: punch_event_2029_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_02 FOR VALUES FROM ('2029-02-01 00:00:00+05:45') TO ('2029-03-01 00:00:00+05:45');

--
-- Name: punch_event_2029_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_03 FOR VALUES FROM ('2029-03-01 00:00:00+05:45') TO ('2029-04-01 00:00:00+05:45');

--
-- Name: punch_event_2029_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_04 FOR VALUES FROM ('2029-04-01 00:00:00+05:45') TO ('2029-05-01 00:00:00+05:45');

--
-- Name: punch_event_2029_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_05 FOR VALUES FROM ('2029-05-01 00:00:00+05:45') TO ('2029-06-01 00:00:00+05:45');

--
-- Name: punch_event_2029_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_06 FOR VALUES FROM ('2029-06-01 00:00:00+05:45') TO ('2029-07-01 00:00:00+05:45');

--
-- Name: punch_event_2029_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_07 FOR VALUES FROM ('2029-07-01 00:00:00+05:45') TO ('2029-08-01 00:00:00+05:45');

--
-- Name: punch_event_2029_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_08 FOR VALUES FROM ('2029-08-01 00:00:00+05:45') TO ('2029-09-01 00:00:00+05:45');

--
-- Name: punch_event_2029_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_09 FOR VALUES FROM ('2029-09-01 00:00:00+05:45') TO ('2029-10-01 00:00:00+05:45');

--
-- Name: punch_event_2029_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_10 FOR VALUES FROM ('2029-10-01 00:00:00+05:45') TO ('2029-11-01 00:00:00+05:45');

--
-- Name: punch_event_2029_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_11 FOR VALUES FROM ('2029-11-01 00:00:00+05:45') TO ('2029-12-01 00:00:00+05:45');

--
-- Name: punch_event_2029_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2029_12 FOR VALUES FROM ('2029-12-01 00:00:00+05:45') TO ('2030-01-01 00:00:00+05:45');

--
-- Name: punch_event_2030_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_01 FOR VALUES FROM ('2030-01-01 00:00:00+05:45') TO ('2030-02-01 00:00:00+05:45');

--
-- Name: punch_event_2030_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_02 FOR VALUES FROM ('2030-02-01 00:00:00+05:45') TO ('2030-03-01 00:00:00+05:45');

--
-- Name: punch_event_2030_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_03 FOR VALUES FROM ('2030-03-01 00:00:00+05:45') TO ('2030-04-01 00:00:00+05:45');

--
-- Name: punch_event_2030_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_04 FOR VALUES FROM ('2030-04-01 00:00:00+05:45') TO ('2030-05-01 00:00:00+05:45');

--
-- Name: punch_event_2030_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_05 FOR VALUES FROM ('2030-05-01 00:00:00+05:45') TO ('2030-06-01 00:00:00+05:45');

--
-- Name: punch_event_2030_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_06 FOR VALUES FROM ('2030-06-01 00:00:00+05:45') TO ('2030-07-01 00:00:00+05:45');

--
-- Name: punch_event_2030_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_07 FOR VALUES FROM ('2030-07-01 00:00:00+05:45') TO ('2030-08-01 00:00:00+05:45');

--
-- Name: punch_event_2030_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_08 FOR VALUES FROM ('2030-08-01 00:00:00+05:45') TO ('2030-09-01 00:00:00+05:45');

--
-- Name: punch_event_2030_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_09 FOR VALUES FROM ('2030-09-01 00:00:00+05:45') TO ('2030-10-01 00:00:00+05:45');

--
-- Name: punch_event_2030_10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_10 FOR VALUES FROM ('2030-10-01 00:00:00+05:45') TO ('2030-11-01 00:00:00+05:45');

--
-- Name: punch_event_2030_11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_11 FOR VALUES FROM ('2030-11-01 00:00:00+05:45') TO ('2030-12-01 00:00:00+05:45');

--
-- Name: punch_event_2030_12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_2030_12 FOR VALUES FROM ('2030-12-01 00:00:00+05:45') TO ('2031-01-01 00:00:00+05:45');

--
-- Name: punch_event_default; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event ATTACH PARTITION public.punch_event_default DEFAULT;

--
-- Name: announcement_ack announcement_ack_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_ack
    ADD CONSTRAINT announcement_ack_pkey PRIMARY KEY (announcement_id, user_id);

--
-- Name: announcement announcement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement
    ADD CONSTRAINT announcement_pkey PRIMARY KEY (id);

--
-- Name: announcement_popup_seen announcement_popup_seen_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_popup_seen
    ADD CONSTRAINT announcement_popup_seen_pkey PRIMARY KEY (announcement_id, user_id);

--
-- Name: app_user app_user_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_email_key UNIQUE (email);

--
-- Name: app_user app_user_employee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_employee_id_key UNIQUE (employee_id);

--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);

--
-- Name: approval_action approval_action_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_action
    ADD CONSTRAINT approval_action_pkey PRIMARY KEY (id);

--
-- Name: approval_flow approval_flow_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_flow
    ADD CONSTRAINT approval_flow_pkey PRIMARY KEY (id);

--
-- Name: approver_delegate approver_delegate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approver_delegate
    ADD CONSTRAINT approver_delegate_pkey PRIMARY KEY (id);

--
-- Name: asset_assignment asset_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_pkey PRIMARY KEY (id);

--
-- Name: asset asset_company_id_tag_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT asset_company_id_tag_key UNIQUE (company_id, tag);

--
-- Name: asset asset_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT asset_pkey PRIMARY KEY (id);

--
-- Name: asset_type asset_type_company_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_type
    ADD CONSTRAINT asset_type_company_id_name_key UNIQUE (company_id, name);

--
-- Name: asset_type asset_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_type
    ADD CONSTRAINT asset_type_pkey PRIMARY KEY (id);

--
-- Name: attendance_day attendance_day_employee_id_local_date_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_day
    ADD CONSTRAINT attendance_day_employee_id_local_date_version_key UNIQUE (employee_id, local_date, version);

--
-- Name: attendance_day attendance_day_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_day
    ADD CONSTRAINT attendance_day_pkey PRIMARY KEY (id);

--
-- Name: attendance_nudge attendance_nudge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_nudge
    ADD CONSTRAINT attendance_nudge_pkey PRIMARY KEY (id);

--
-- Name: attendance_nudge attendance_nudge_target_user_id_kind_ref_local_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_nudge
    ADD CONSTRAINT attendance_nudge_target_user_id_kind_ref_local_date_key UNIQUE (target_user_id, kind, ref, local_date);

--
-- Name: attendance_upload attendance_upload_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_upload
    ADD CONSTRAINT attendance_upload_pkey PRIMARY KEY (id);

--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

--
-- Name: bank_file bank_file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_file
    ADD CONSTRAINT bank_file_pkey PRIMARY KEY (id);

--
-- Name: biometric_template biometric_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.biometric_template
    ADD CONSTRAINT biometric_template_pkey PRIMARY KEY (id);

--
-- Name: branch branch_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch
    ADD CONSTRAINT branch_company_id_code_key UNIQUE (company_id, code);

--
-- Name: branch branch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch
    ADD CONSTRAINT branch_pkey PRIMARY KEY (id);

--
-- Name: bs_calendar bs_calendar_ad_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bs_calendar
    ADD CONSTRAINT bs_calendar_ad_date_key UNIQUE (ad_date);

--
-- Name: bs_calendar bs_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bs_calendar
    ADD CONSTRAINT bs_calendar_pkey PRIMARY KEY (bs_year, bs_month, bs_day);

--
-- Name: career_event career_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.career_event
    ADD CONSTRAINT career_event_pkey PRIMARY KEY (id);

--
-- Name: company company_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_code_key UNIQUE (code);

--
-- Name: company company_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_pkey PRIMARY KEY (id);

--
-- Name: company_policy company_policy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_policy
    ADD CONSTRAINT company_policy_pkey PRIMARY KEY (id);

--
-- Name: company_setting company_setting_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_setting
    ADD CONSTRAINT company_setting_pkey PRIMARY KEY (company_id, key);

--
-- Name: consent_record consent_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_record
    ADD CONSTRAINT consent_record_pkey PRIMARY KEY (id);

--
-- Name: designation designation_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designation
    ADD CONSTRAINT designation_company_id_code_key UNIQUE (company_id, code);

--
-- Name: designation designation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designation
    ADD CONSTRAINT designation_pkey PRIMARY KEY (id);

--
-- Name: device_command device_command_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_command
    ADD CONSTRAINT device_command_pkey PRIMARY KEY (id);

--
-- Name: device device_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device
    ADD CONSTRAINT device_pkey PRIMARY KEY (id);

--
-- Name: device device_serial_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device
    ADD CONSTRAINT device_serial_no_key UNIQUE (serial_no);

--
-- Name: device_user_map device_user_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_user_map
    ADD CONSTRAINT device_user_map_pkey PRIMARY KEY (device_id, device_user_pin, valid WITHOUT OVERLAPS);

--
-- Name: disciplinary_case disciplinary_case_company_id_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_company_id_number_key UNIQUE (company_id, number);

--
-- Name: disciplinary_case disciplinary_case_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_pkey PRIMARY KEY (id);

--
-- Name: disciplinary_stage disciplinary_stage_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_stage
    ADD CONSTRAINT disciplinary_stage_company_id_code_key UNIQUE (company_id, code);

--
-- Name: disciplinary_stage disciplinary_stage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_stage
    ADD CONSTRAINT disciplinary_stage_pkey PRIMARY KEY (id);

--
-- Name: document_category document_category_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_category
    ADD CONSTRAINT document_category_company_id_code_key UNIQUE (company_id, code);

--
-- Name: document_category document_category_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_category
    ADD CONSTRAINT document_category_pkey PRIMARY KEY (id);

--
-- Name: document_expiry_alert document_expiry_alert_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_expiry_alert
    ADD CONSTRAINT document_expiry_alert_pkey PRIMARY KEY (document_id, threshold);

--
-- Name: employee_assignment employee_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignment
    ADD CONSTRAINT employee_assignment_pkey PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS);

--
-- Name: employee employee_company_id_emp_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_company_id_emp_code_key UNIQUE (company_id, emp_code);

--
-- Name: employee_confirmation employee_confirmation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_confirmation
    ADD CONSTRAINT employee_confirmation_pkey PRIMARY KEY (id);

--
-- Name: employee_document employee_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_pkey PRIMARY KEY (id);

--
-- Name: employee_education employee_education_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_education
    ADD CONSTRAINT employee_education_pkey PRIMARY KEY (id);

--
-- Name: employee_emergency_contact employee_emergency_contact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_emergency_contact
    ADD CONSTRAINT employee_emergency_contact_pkey PRIMARY KEY (id);

--
-- Name: employee_exit employee_exit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exit
    ADD CONSTRAINT employee_exit_pkey PRIMARY KEY (id);

--
-- Name: employee_experience employee_experience_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_experience
    ADD CONSTRAINT employee_experience_pkey PRIMARY KEY (id);

--
-- Name: employee_family employee_family_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_family
    ADD CONSTRAINT employee_family_pkey PRIMARY KEY (id);

--
-- Name: employee_field_history employee_field_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_field_history
    ADD CONSTRAINT employee_field_history_pkey PRIMARY KEY (id);

--
-- Name: employee employee_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_pkey PRIMARY KEY (id);

--
-- Name: employee_temp_assignment employee_temp_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_pkey PRIMARY KEY (id);

--
-- Name: engage_comment engage_comment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_comment
    ADD CONSTRAINT engage_comment_pkey PRIMARY KEY (id);

--
-- Name: engage_pref engage_pref_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_pref
    ADD CONSTRAINT engage_pref_pkey PRIMARY KEY (employee_id);

--
-- Name: engage_reaction engage_reaction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_reaction
    ADD CONSTRAINT engage_reaction_pkey PRIMARY KEY (company_id, target_type, target_id, user_id, emoji);

--
-- Name: engage_setting engage_setting_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_setting
    ADD CONSTRAINT engage_setting_pkey PRIMARY KEY (company_id);

--
-- Name: engage_value engage_value_company_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_value
    ADD CONSTRAINT engage_value_company_id_name_key UNIQUE (company_id, name);

--
-- Name: engage_value engage_value_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_value
    ADD CONSTRAINT engage_value_pkey PRIMARY KEY (id);

--
-- Name: exit_interview exit_interview_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview
    ADD CONSTRAINT exit_interview_pkey PRIMARY KEY (exit_id);

--
-- Name: exit_interview_question exit_interview_question_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview_question
    ADD CONSTRAINT exit_interview_question_pkey PRIMARY KEY (id);

--
-- Name: expense_category expense_category_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_category
    ADD CONSTRAINT expense_category_company_id_code_key UNIQUE (company_id, code);

--
-- Name: expense_category expense_category_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_category
    ADD CONSTRAINT expense_category_pkey PRIMARY KEY (id);

--
-- Name: expense_claim expense_claim_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_claim
    ADD CONSTRAINT expense_claim_pkey PRIMARY KEY (id);

--
-- Name: expense_item expense_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_item
    ADD CONSTRAINT expense_item_pkey PRIMARY KEY (id);

--
-- Name: expense_limit expense_limit_category_id_grade_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_limit
    ADD CONSTRAINT expense_limit_category_id_grade_key UNIQUE (category_id, grade);

--
-- Name: expense_limit expense_limit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_limit
    ADD CONSTRAINT expense_limit_pkey PRIMARY KEY (id);

--
-- Name: expense_payout expense_payout_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_payout
    ADD CONSTRAINT expense_payout_pkey PRIMARY KEY (id);

--
-- Name: field_visit field_visit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_visit
    ADD CONSTRAINT field_visit_pkey PRIMARY KEY (id);

--
-- Name: grievance_committee_member grievance_committee_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_committee_member
    ADD CONSTRAINT grievance_committee_member_pkey PRIMARY KEY (company_id, user_id);

--
-- Name: grievance grievance_company_id_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_company_id_number_key UNIQUE (company_id, number);

--
-- Name: grievance_file grievance_file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_file
    ADD CONSTRAINT grievance_file_pkey PRIMARY KEY (grievance_id, file_id);

--
-- Name: grievance_note grievance_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_note
    ADD CONSTRAINT grievance_note_pkey PRIMARY KEY (id);

--
-- Name: grievance grievance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_pkey PRIMARY KEY (id);

--
-- Name: helpdesk_category helpdesk_category_company_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_category
    ADD CONSTRAINT helpdesk_category_company_id_name_key UNIQUE (company_id, name);

--
-- Name: helpdesk_category helpdesk_category_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_category
    ADD CONSTRAINT helpdesk_category_pkey PRIMARY KEY (id);

--
-- Name: helpdesk_message helpdesk_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_message
    ADD CONSTRAINT helpdesk_message_pkey PRIMARY KEY (id);

--
-- Name: helpdesk_ticket helpdesk_ticket_company_id_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_company_id_number_key UNIQUE (company_id, number);

--
-- Name: helpdesk_ticket helpdesk_ticket_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_pkey PRIMARY KEY (id);

--
-- Name: hiring_application hiring_application_job_id_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_job_id_candidate_id_key UNIQUE (job_id, candidate_id);

--
-- Name: hiring_application hiring_application_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_pkey PRIMARY KEY (id);

--
-- Name: hiring_candidate hiring_candidate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_candidate
    ADD CONSTRAINT hiring_candidate_pkey PRIMARY KEY (id);

--
-- Name: hiring_event hiring_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_event
    ADD CONSTRAINT hiring_event_pkey PRIMARY KEY (id);

--
-- Name: hiring_interview hiring_interview_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_interview
    ADD CONSTRAINT hiring_interview_pkey PRIMARY KEY (id);

--
-- Name: hiring_job hiring_job_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_pkey PRIMARY KEY (id);

--
-- Name: hiring_offer hiring_offer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_offer
    ADD CONSTRAINT hiring_offer_pkey PRIMARY KEY (id);

--
-- Name: hiring_requisition hiring_requisition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_pkey PRIMARY KEY (id);

--
-- Name: hiring_scorecard hiring_scorecard_interview_id_reviewer_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_scorecard
    ADD CONSTRAINT hiring_scorecard_interview_id_reviewer_id_key UNIQUE (interview_id, reviewer_id);

--
-- Name: hiring_scorecard hiring_scorecard_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_scorecard
    ADD CONSTRAINT hiring_scorecard_pkey PRIMARY KEY (id);

--
-- Name: holiday holiday_company_id_branch_id_local_date_name_en_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday
    ADD CONSTRAINT holiday_company_id_branch_id_local_date_name_en_key UNIQUE (company_id, branch_id, local_date, name_en);

--
-- Name: holiday holiday_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday
    ADD CONSTRAINT holiday_pkey PRIMARY KEY (id);

--
-- Name: issued_letter issued_letter_company_id_ref_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_company_id_ref_no_key UNIQUE (company_id, ref_no);

--
-- Name: issued_letter issued_letter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_pkey PRIMARY KEY (id);

--
-- Name: kiosk_credential kiosk_credential_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_credential
    ADD CONSTRAINT kiosk_credential_pkey PRIMARY KEY (employee_id);

--
-- Name: kiosk kiosk_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_key_hash_key UNIQUE (key_hash);

--
-- Name: kiosk kiosk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_pkey PRIMARY KEY (id);

--
-- Name: kiosk_qr_use kiosk_qr_use_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_qr_use
    ADD CONSTRAINT kiosk_qr_use_pkey PRIMARY KEY (employee_id, time_window);

--
-- Name: kudos kudos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos
    ADD CONSTRAINT kudos_pkey PRIMARY KEY (id);

--
-- Name: kudos_recipient kudos_recipient_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos_recipient
    ADD CONSTRAINT kudos_recipient_pkey PRIMARY KEY (kudos_id, employee_id);

--
-- Name: kye_campaign kye_campaign_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_campaign
    ADD CONSTRAINT kye_campaign_pkey PRIMARY KEY (id);

--
-- Name: kye_response kye_response_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_response
    ADD CONSTRAINT kye_response_pkey PRIMARY KEY (campaign_id, employee_id);

--
-- Name: late_penalty late_penalty_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_pkey PRIMARY KEY (id);

--
-- Name: leave_ledger leave_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_pkey PRIMARY KEY (id);

--
-- Name: leave_request leave_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_pkey PRIMARY KEY (id);

--
-- Name: leave_type leave_type_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_type
    ADD CONSTRAINT leave_type_company_id_code_key UNIQUE (company_id, code);

--
-- Name: leave_type leave_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_type
    ADD CONSTRAINT leave_type_pkey PRIMARY KEY (id);

--
-- Name: letter_request letter_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_request
    ADD CONSTRAINT letter_request_pkey PRIMARY KEY (id);

--
-- Name: letter_sequence letter_sequence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_sequence
    ADD CONSTRAINT letter_sequence_pkey PRIMARY KEY (company_id, bs_year);

--
-- Name: letter_template letter_template_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_template
    ADD CONSTRAINT letter_template_company_id_code_key UNIQUE (company_id, code);

--
-- Name: letter_template letter_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_template
    ADD CONSTRAINT letter_template_pkey PRIMARY KEY (id);

--
-- Name: lifecycle_case lifecycle_case_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_case
    ADD CONSTRAINT lifecycle_case_pkey PRIMARY KEY (id);

--
-- Name: lifecycle_reminder lifecycle_reminder_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_reminder
    ADD CONSTRAINT lifecycle_reminder_pkey PRIMARY KEY (kind, ref_id, key);

--
-- Name: lifecycle_task_comment lifecycle_task_comment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task_comment
    ADD CONSTRAINT lifecycle_task_comment_pkey PRIMARY KEY (id);

--
-- Name: lifecycle_task lifecycle_task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_pkey PRIMARY KEY (id);

--
-- Name: lifecycle_template lifecycle_template_company_id_kind_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template
    ADD CONSTRAINT lifecycle_template_company_id_kind_name_key UNIQUE (company_id, kind, name);

--
-- Name: lifecycle_template lifecycle_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template
    ADD CONSTRAINT lifecycle_template_pkey PRIMARY KEY (id);

--
-- Name: lifecycle_template_task lifecycle_template_task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template_task
    ADD CONSTRAINT lifecycle_template_task_pkey PRIMARY KEY (id);

--
-- Name: loan_installment loan_installment_loan_id_payslip_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan_installment
    ADD CONSTRAINT loan_installment_loan_id_payslip_id_key UNIQUE (loan_id, payslip_id);

--
-- Name: loan_installment loan_installment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan_installment
    ADD CONSTRAINT loan_installment_pkey PRIMARY KEY (id);

--
-- Name: loan loan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan
    ADD CONSTRAINT loan_pkey PRIMARY KEY (id);

--
-- Name: location_assignment location_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location_assignment
    ADD CONSTRAINT location_assignment_pkey PRIMARY KEY (location_id, employee_id);

--
-- Name: location location_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location
    ADD CONSTRAINT location_pkey PRIMARY KEY (id);

--
-- Name: login_challenge login_challenge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_challenge
    ADD CONSTRAINT login_challenge_pkey PRIMARY KEY (token_hash);

--
-- Name: lrn_attempt lrn_attempt_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_attempt
    ADD CONSTRAINT lrn_attempt_pkey PRIMARY KEY (id);

--
-- Name: lrn_course lrn_course_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_course
    ADD CONSTRAINT lrn_course_company_id_code_key UNIQUE (company_id, code);

--
-- Name: lrn_course lrn_course_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_course
    ADD CONSTRAINT lrn_course_pkey PRIMARY KEY (id);

--
-- Name: lrn_enrollment lrn_enrollment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_pkey PRIMARY KEY (id);

--
-- Name: lrn_question lrn_question_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_question
    ADD CONSTRAINT lrn_question_pkey PRIMARY KEY (id);

--
-- Name: lrn_session lrn_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_session
    ADD CONSTRAINT lrn_session_pkey PRIMARY KEY (id);

--
-- Name: notification_outbox notification_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_outbox
    ADD CONSTRAINT notification_outbox_pkey PRIMARY KEY (id);

--
-- Name: org_unit org_unit_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_company_id_code_key UNIQUE (company_id, code);

--
-- Name: org_unit org_unit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_pkey PRIMARY KEY (id);

--
-- Name: ot_request ot_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_request
    ADD CONSTRAINT ot_request_pkey PRIMARY KEY (id);

--
-- Name: ot_slip ot_slip_employee_id_local_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_employee_id_local_date_key UNIQUE (employee_id, local_date);

--
-- Name: ot_slip ot_slip_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_pkey PRIMARY KEY (id);

--
-- Name: payroll_hold payroll_hold_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_pkey PRIMARY KEY (id);

--
-- Name: payroll_hold payroll_hold_run_id_employee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_run_id_employee_id_key UNIQUE (run_id, employee_id);

--
-- Name: payroll_input payroll_input_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_input
    ADD CONSTRAINT payroll_input_pkey PRIMARY KEY (id);

--
-- Name: payroll_opening_balance payroll_opening_balance_employee_id_fiscal_year_start_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_balance
    ADD CONSTRAINT payroll_opening_balance_employee_id_fiscal_year_start_key UNIQUE (employee_id, fiscal_year_start);

--
-- Name: payroll_opening_balance payroll_opening_balance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_balance
    ADD CONSTRAINT payroll_opening_balance_pkey PRIMARY KEY (id);

--
-- Name: payroll_period payroll_period_company_id_bs_year_bs_month_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_period
    ADD CONSTRAINT payroll_period_company_id_bs_year_bs_month_key UNIQUE (company_id, bs_year, bs_month);

--
-- Name: payroll_period payroll_period_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_period
    ADD CONSTRAINT payroll_period_pkey PRIMARY KEY (id);

--
-- Name: payroll_run payroll_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_pkey PRIMARY KEY (id);

--
-- Name: payslip_line payslip_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip_line
    ADD CONSTRAINT payslip_line_pkey PRIMARY KEY (id);

--
-- Name: payslip payslip_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip
    ADD CONSTRAINT payslip_pkey PRIMARY KEY (id);

--
-- Name: payslip payslip_run_id_employee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip
    ADD CONSTRAINT payslip_run_id_employee_id_key UNIQUE (run_id, employee_id);

--
-- Name: perf_checkin perf_checkin_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_checkin
    ADD CONSTRAINT perf_checkin_pkey PRIMARY KEY (id);

--
-- Name: perf_competency perf_competency_company_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_competency
    ADD CONSTRAINT perf_competency_company_id_name_key UNIQUE (company_id, name);

--
-- Name: perf_competency perf_competency_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_competency
    ADD CONSTRAINT perf_competency_pkey PRIMARY KEY (id);

--
-- Name: perf_cycle perf_cycle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_cycle
    ADD CONSTRAINT perf_cycle_pkey PRIMARY KEY (id);

--
-- Name: perf_feedback perf_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_feedback
    ADD CONSTRAINT perf_feedback_pkey PRIMARY KEY (id);

--
-- Name: perf_goal perf_goal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_pkey PRIMARY KEY (id);

--
-- Name: perf_journal perf_journal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_journal
    ADD CONSTRAINT perf_journal_pkey PRIMARY KEY (id);

--
-- Name: perf_key_result perf_key_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_key_result
    ADD CONSTRAINT perf_key_result_pkey PRIMARY KEY (id);

--
-- Name: perf_kpi perf_kpi_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_kpi
    ADD CONSTRAINT perf_kpi_pkey PRIMARY KEY (id);

--
-- Name: perf_one_on_one_item perf_one_on_one_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one_item
    ADD CONSTRAINT perf_one_on_one_item_pkey PRIMARY KEY (id);

--
-- Name: perf_one_on_one perf_one_on_one_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one
    ADD CONSTRAINT perf_one_on_one_pkey PRIMARY KEY (id);

--
-- Name: perf_peer perf_peer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_peer
    ADD CONSTRAINT perf_peer_pkey PRIMARY KEY (id);

--
-- Name: perf_peer perf_peer_review_id_reviewer_employee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_peer
    ADD CONSTRAINT perf_peer_review_id_reviewer_employee_id_key UNIQUE (review_id, reviewer_employee_id);

--
-- Name: perf_reminder_sent perf_reminder_sent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_reminder_sent
    ADD CONSTRAINT perf_reminder_sent_pkey PRIMARY KEY (key);

--
-- Name: perf_review perf_review_cycle_id_employee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_cycle_id_employee_id_key UNIQUE (cycle_id, employee_id);

--
-- Name: perf_review_item perf_review_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review_item
    ADD CONSTRAINT perf_review_item_pkey PRIMARY KEY (id);

--
-- Name: perf_review_item perf_review_item_review_id_item_type_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review_item
    ADD CONSTRAINT perf_review_item_review_id_item_type_ref_id_key UNIQUE (review_id, item_type, ref_id);

--
-- Name: perf_review perf_review_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_pkey PRIMARY KEY (id);

--
-- Name: policy_ack policy_ack_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_ack
    ADD CONSTRAINT policy_ack_pkey PRIMARY KEY (policy_id, employee_id);

--
-- Name: policy_nudge policy_nudge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_nudge
    ADD CONSTRAINT policy_nudge_pkey PRIMARY KEY (policy_id, employee_id);

--
-- Name: probation_review probation_review_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.probation_review
    ADD CONSTRAINT probation_review_pkey PRIMARY KEY (id);

--
-- Name: profile_change_request profile_change_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_pkey PRIMARY KEY (id);

--
-- Name: punch_event punch_event_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event
    ADD CONSTRAINT punch_event_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_01 punch_event_2026_01_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_01
    ADD CONSTRAINT punch_event_2026_01_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event punch_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event
    ADD CONSTRAINT punch_event_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_01 punch_event_2026_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_01
    ADD CONSTRAINT punch_event_2026_01_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_02 punch_event_2026_02_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_02
    ADD CONSTRAINT punch_event_2026_02_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_02 punch_event_2026_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_02
    ADD CONSTRAINT punch_event_2026_02_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_03 punch_event_2026_03_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_03
    ADD CONSTRAINT punch_event_2026_03_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_03 punch_event_2026_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_03
    ADD CONSTRAINT punch_event_2026_03_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_04 punch_event_2026_04_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_04
    ADD CONSTRAINT punch_event_2026_04_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_04 punch_event_2026_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_04
    ADD CONSTRAINT punch_event_2026_04_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_05 punch_event_2026_05_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_05
    ADD CONSTRAINT punch_event_2026_05_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_05 punch_event_2026_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_05
    ADD CONSTRAINT punch_event_2026_05_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_06 punch_event_2026_06_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_06
    ADD CONSTRAINT punch_event_2026_06_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_06 punch_event_2026_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_06
    ADD CONSTRAINT punch_event_2026_06_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_07 punch_event_2026_07_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_07
    ADD CONSTRAINT punch_event_2026_07_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_07 punch_event_2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_07
    ADD CONSTRAINT punch_event_2026_07_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_08 punch_event_2026_08_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_08
    ADD CONSTRAINT punch_event_2026_08_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_08 punch_event_2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_08
    ADD CONSTRAINT punch_event_2026_08_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_09 punch_event_2026_09_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_09
    ADD CONSTRAINT punch_event_2026_09_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_09 punch_event_2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_09
    ADD CONSTRAINT punch_event_2026_09_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_10 punch_event_2026_10_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_10
    ADD CONSTRAINT punch_event_2026_10_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_10 punch_event_2026_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_10
    ADD CONSTRAINT punch_event_2026_10_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_11 punch_event_2026_11_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_11
    ADD CONSTRAINT punch_event_2026_11_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_11 punch_event_2026_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_11
    ADD CONSTRAINT punch_event_2026_11_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2026_12 punch_event_2026_12_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_12
    ADD CONSTRAINT punch_event_2026_12_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2026_12 punch_event_2026_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2026_12
    ADD CONSTRAINT punch_event_2026_12_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_01 punch_event_2027_01_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_01
    ADD CONSTRAINT punch_event_2027_01_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_01 punch_event_2027_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_01
    ADD CONSTRAINT punch_event_2027_01_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_02 punch_event_2027_02_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_02
    ADD CONSTRAINT punch_event_2027_02_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_02 punch_event_2027_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_02
    ADD CONSTRAINT punch_event_2027_02_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_03 punch_event_2027_03_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_03
    ADD CONSTRAINT punch_event_2027_03_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_03 punch_event_2027_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_03
    ADD CONSTRAINT punch_event_2027_03_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_04 punch_event_2027_04_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_04
    ADD CONSTRAINT punch_event_2027_04_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_04 punch_event_2027_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_04
    ADD CONSTRAINT punch_event_2027_04_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_05 punch_event_2027_05_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_05
    ADD CONSTRAINT punch_event_2027_05_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_05 punch_event_2027_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_05
    ADD CONSTRAINT punch_event_2027_05_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_06 punch_event_2027_06_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_06
    ADD CONSTRAINT punch_event_2027_06_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_06 punch_event_2027_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_06
    ADD CONSTRAINT punch_event_2027_06_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_07 punch_event_2027_07_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_07
    ADD CONSTRAINT punch_event_2027_07_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_07 punch_event_2027_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_07
    ADD CONSTRAINT punch_event_2027_07_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_08 punch_event_2027_08_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_08
    ADD CONSTRAINT punch_event_2027_08_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_08 punch_event_2027_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_08
    ADD CONSTRAINT punch_event_2027_08_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_09 punch_event_2027_09_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_09
    ADD CONSTRAINT punch_event_2027_09_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_09 punch_event_2027_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_09
    ADD CONSTRAINT punch_event_2027_09_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_10 punch_event_2027_10_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_10
    ADD CONSTRAINT punch_event_2027_10_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_10 punch_event_2027_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_10
    ADD CONSTRAINT punch_event_2027_10_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_11 punch_event_2027_11_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_11
    ADD CONSTRAINT punch_event_2027_11_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_11 punch_event_2027_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_11
    ADD CONSTRAINT punch_event_2027_11_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2027_12 punch_event_2027_12_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_12
    ADD CONSTRAINT punch_event_2027_12_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2027_12 punch_event_2027_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2027_12
    ADD CONSTRAINT punch_event_2027_12_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_01 punch_event_2028_01_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_01
    ADD CONSTRAINT punch_event_2028_01_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_01 punch_event_2028_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_01
    ADD CONSTRAINT punch_event_2028_01_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_02 punch_event_2028_02_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_02
    ADD CONSTRAINT punch_event_2028_02_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_02 punch_event_2028_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_02
    ADD CONSTRAINT punch_event_2028_02_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_03 punch_event_2028_03_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_03
    ADD CONSTRAINT punch_event_2028_03_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_03 punch_event_2028_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_03
    ADD CONSTRAINT punch_event_2028_03_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_04 punch_event_2028_04_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_04
    ADD CONSTRAINT punch_event_2028_04_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_04 punch_event_2028_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_04
    ADD CONSTRAINT punch_event_2028_04_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_05 punch_event_2028_05_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_05
    ADD CONSTRAINT punch_event_2028_05_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_05 punch_event_2028_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_05
    ADD CONSTRAINT punch_event_2028_05_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_06 punch_event_2028_06_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_06
    ADD CONSTRAINT punch_event_2028_06_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_06 punch_event_2028_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_06
    ADD CONSTRAINT punch_event_2028_06_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_07 punch_event_2028_07_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_07
    ADD CONSTRAINT punch_event_2028_07_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_07 punch_event_2028_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_07
    ADD CONSTRAINT punch_event_2028_07_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_08 punch_event_2028_08_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_08
    ADD CONSTRAINT punch_event_2028_08_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_08 punch_event_2028_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_08
    ADD CONSTRAINT punch_event_2028_08_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_09 punch_event_2028_09_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_09
    ADD CONSTRAINT punch_event_2028_09_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_09 punch_event_2028_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_09
    ADD CONSTRAINT punch_event_2028_09_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_10 punch_event_2028_10_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_10
    ADD CONSTRAINT punch_event_2028_10_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_10 punch_event_2028_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_10
    ADD CONSTRAINT punch_event_2028_10_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_11 punch_event_2028_11_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_11
    ADD CONSTRAINT punch_event_2028_11_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_11 punch_event_2028_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_11
    ADD CONSTRAINT punch_event_2028_11_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2028_12 punch_event_2028_12_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_12
    ADD CONSTRAINT punch_event_2028_12_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2028_12 punch_event_2028_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2028_12
    ADD CONSTRAINT punch_event_2028_12_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_01 punch_event_2029_01_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_01
    ADD CONSTRAINT punch_event_2029_01_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_01 punch_event_2029_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_01
    ADD CONSTRAINT punch_event_2029_01_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_02 punch_event_2029_02_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_02
    ADD CONSTRAINT punch_event_2029_02_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_02 punch_event_2029_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_02
    ADD CONSTRAINT punch_event_2029_02_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_03 punch_event_2029_03_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_03
    ADD CONSTRAINT punch_event_2029_03_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_03 punch_event_2029_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_03
    ADD CONSTRAINT punch_event_2029_03_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_04 punch_event_2029_04_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_04
    ADD CONSTRAINT punch_event_2029_04_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_04 punch_event_2029_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_04
    ADD CONSTRAINT punch_event_2029_04_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_05 punch_event_2029_05_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_05
    ADD CONSTRAINT punch_event_2029_05_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_05 punch_event_2029_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_05
    ADD CONSTRAINT punch_event_2029_05_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_06 punch_event_2029_06_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_06
    ADD CONSTRAINT punch_event_2029_06_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_06 punch_event_2029_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_06
    ADD CONSTRAINT punch_event_2029_06_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_07 punch_event_2029_07_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_07
    ADD CONSTRAINT punch_event_2029_07_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_07 punch_event_2029_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_07
    ADD CONSTRAINT punch_event_2029_07_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_08 punch_event_2029_08_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_08
    ADD CONSTRAINT punch_event_2029_08_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_08 punch_event_2029_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_08
    ADD CONSTRAINT punch_event_2029_08_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_09 punch_event_2029_09_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_09
    ADD CONSTRAINT punch_event_2029_09_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_09 punch_event_2029_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_09
    ADD CONSTRAINT punch_event_2029_09_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_10 punch_event_2029_10_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_10
    ADD CONSTRAINT punch_event_2029_10_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_10 punch_event_2029_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_10
    ADD CONSTRAINT punch_event_2029_10_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_11 punch_event_2029_11_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_11
    ADD CONSTRAINT punch_event_2029_11_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_11 punch_event_2029_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_11
    ADD CONSTRAINT punch_event_2029_11_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2029_12 punch_event_2029_12_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_12
    ADD CONSTRAINT punch_event_2029_12_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2029_12 punch_event_2029_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2029_12
    ADD CONSTRAINT punch_event_2029_12_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_01 punch_event_2030_01_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_01
    ADD CONSTRAINT punch_event_2030_01_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_01 punch_event_2030_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_01
    ADD CONSTRAINT punch_event_2030_01_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_02 punch_event_2030_02_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_02
    ADD CONSTRAINT punch_event_2030_02_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_02 punch_event_2030_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_02
    ADD CONSTRAINT punch_event_2030_02_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_03 punch_event_2030_03_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_03
    ADD CONSTRAINT punch_event_2030_03_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_03 punch_event_2030_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_03
    ADD CONSTRAINT punch_event_2030_03_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_04 punch_event_2030_04_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_04
    ADD CONSTRAINT punch_event_2030_04_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_04 punch_event_2030_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_04
    ADD CONSTRAINT punch_event_2030_04_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_05 punch_event_2030_05_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_05
    ADD CONSTRAINT punch_event_2030_05_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_05 punch_event_2030_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_05
    ADD CONSTRAINT punch_event_2030_05_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_06 punch_event_2030_06_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_06
    ADD CONSTRAINT punch_event_2030_06_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_06 punch_event_2030_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_06
    ADD CONSTRAINT punch_event_2030_06_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_07 punch_event_2030_07_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_07
    ADD CONSTRAINT punch_event_2030_07_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_07 punch_event_2030_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_07
    ADD CONSTRAINT punch_event_2030_07_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_08 punch_event_2030_08_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_08
    ADD CONSTRAINT punch_event_2030_08_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_08 punch_event_2030_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_08
    ADD CONSTRAINT punch_event_2030_08_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_09 punch_event_2030_09_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_09
    ADD CONSTRAINT punch_event_2030_09_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_09 punch_event_2030_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_09
    ADD CONSTRAINT punch_event_2030_09_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_10 punch_event_2030_10_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_10
    ADD CONSTRAINT punch_event_2030_10_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_10 punch_event_2030_10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_10
    ADD CONSTRAINT punch_event_2030_10_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_11 punch_event_2030_11_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_11
    ADD CONSTRAINT punch_event_2030_11_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_11 punch_event_2030_11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_11
    ADD CONSTRAINT punch_event_2030_11_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_2030_12 punch_event_2030_12_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_12
    ADD CONSTRAINT punch_event_2030_12_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_2030_12 punch_event_2030_12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_2030_12
    ADD CONSTRAINT punch_event_2030_12_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_event_default punch_event_default_idempotency_key_punched_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_default
    ADD CONSTRAINT punch_event_default_idempotency_key_punched_at_key UNIQUE (idempotency_key, punched_at);

--
-- Name: punch_event_default punch_event_default_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_event_default
    ADD CONSTRAINT punch_event_default_pkey PRIMARY KEY (id, punched_at);

--
-- Name: punch_photo punch_photo_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_photo
    ADD CONSTRAINT punch_photo_pkey PRIMARY KEY (id);

--
-- Name: recompute_queue recompute_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recompute_queue
    ADD CONSTRAINT recompute_queue_pkey PRIMARY KEY (employee_id, local_date);

--
-- Name: registered_device registered_device_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_pkey PRIMARY KEY (id);

--
-- Name: registered_device registered_device_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_token_hash_key UNIQUE (token_hash);

--
-- Name: regularisation_request regularisation_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regularisation_request
    ADD CONSTRAINT regularisation_request_pkey PRIMARY KEY (id);

--
-- Name: relations_reminder_sent relations_reminder_sent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relations_reminder_sent
    ADD CONSTRAINT relations_reminder_sent_pkey PRIMARY KEY (company_id, kind, ref);

--
-- Name: roster roster_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roster
    ADD CONSTRAINT roster_pkey PRIMARY KEY (employee_id, local_date);

--
-- Name: rule_version rule_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_version
    ADD CONSTRAINT rule_version_pkey PRIMARY KEY (id);

--
-- Name: salary_assignment salary_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_assignment
    ADD CONSTRAINT salary_assignment_pkey PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS);

--
-- Name: salary_component salary_component_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_component
    ADD CONSTRAINT salary_component_pkey PRIMARY KEY (company_id, code);

--
-- Name: salary_structure salary_structure_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure
    ADD CONSTRAINT salary_structure_company_id_code_key UNIQUE (company_id, code);

--
-- Name: salary_structure salary_structure_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure
    ADD CONSTRAINT salary_structure_pkey PRIMARY KEY (id);

--
-- Name: salary_structure_version salary_structure_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure_version
    ADD CONSTRAINT salary_structure_version_pkey PRIMARY KEY (id);

--
-- Name: salary_structure_version salary_structure_version_structure_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure_version
    ADD CONSTRAINT salary_structure_version_structure_id_version_key UNIQUE (structure_id, version);

--
-- Name: saved_report saved_report_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_report
    ADD CONSTRAINT saved_report_pkey PRIMARY KEY (id);

--
-- Name: session session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_pkey PRIMARY KEY (token_hash);

--
-- Name: shift_assignment shift_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_assignment
    ADD CONSTRAINT shift_assignment_pkey PRIMARY KEY (employee_id, valid WITHOUT OVERLAPS);

--
-- Name: shift shift_company_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift
    ADD CONSTRAINT shift_company_id_code_key UNIQUE (company_id, code);

--
-- Name: shift shift_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift
    ADD CONSTRAINT shift_pkey PRIMARY KEY (id);

--
-- Name: shift_swap shift_swap_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_pkey PRIMARY KEY (id);

--
-- Name: stored_file stored_file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_file
    ADD CONSTRAINT stored_file_pkey PRIMARY KEY (id);

--
-- Name: suggestion suggestion_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion
    ADD CONSTRAINT suggestion_pkey PRIMARY KEY (id);

--
-- Name: survey_answer survey_answer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_answer
    ADD CONSTRAINT survey_answer_pkey PRIMARY KEY (response_id, question_id);

--
-- Name: survey survey_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey
    ADD CONSTRAINT survey_pkey PRIMARY KEY (id);

--
-- Name: survey_question survey_question_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_question
    ADD CONSTRAINT survey_question_pkey PRIMARY KEY (id);

--
-- Name: survey_question survey_question_survey_id_position_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_question
    ADD CONSTRAINT survey_question_survey_id_position_key UNIQUE (survey_id, "position");

--
-- Name: survey_response survey_response_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_pkey PRIMARY KEY (id);

--
-- Name: survey_response survey_response_survey_id_respondent_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_survey_id_respondent_key_key UNIQUE (survey_id, respondent_key);

--
-- Name: tada_rate tada_rate_company_id_grade_destination_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tada_rate
    ADD CONSTRAINT tada_rate_company_id_grade_destination_key UNIQUE (company_id, grade, destination);

--
-- Name: tada_rate tada_rate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tada_rate
    ADD CONSTRAINT tada_rate_pkey PRIMARY KEY (id);

--
-- Name: task_assignee task_assignee_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_assignee
    ADD CONSTRAINT task_assignee_pkey PRIMARY KEY (task_id, user_id);

--
-- Name: task_attachment task_attachment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_attachment
    ADD CONSTRAINT task_attachment_pkey PRIMARY KEY (id);

--
-- Name: task_board_member task_board_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board_member
    ADD CONSTRAINT task_board_member_pkey PRIMARY KEY (board_id, user_id);

--
-- Name: task_board task_board_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board
    ADD CONSTRAINT task_board_pkey PRIMARY KEY (id);

--
-- Name: task_checklist_item task_checklist_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_item
    ADD CONSTRAINT task_checklist_item_pkey PRIMARY KEY (id);

--
-- Name: task_comment task_comment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_comment
    ADD CONSTRAINT task_comment_pkey PRIMARY KEY (id);

--
-- Name: task task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task
    ADD CONSTRAINT task_pkey PRIMARY KEY (id);

--
-- Name: task_reminder_sent task_reminder_sent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminder_sent
    ADD CONSTRAINT task_reminder_sent_pkey PRIMARY KEY (task_id, user_id, kind, local_date);

--
-- Name: tax_declaration tax_declaration_employee_id_fiscal_year_start_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration
    ADD CONSTRAINT tax_declaration_employee_id_fiscal_year_start_key UNIQUE (employee_id, fiscal_year_start);

--
-- Name: tax_declaration tax_declaration_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration
    ADD CONSTRAINT tax_declaration_pkey PRIMARY KEY (id);

--
-- Name: tax_declaration_request tax_declaration_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration_request
    ADD CONSTRAINT tax_declaration_request_pkey PRIMARY KEY (id);

--
-- Name: time_activity time_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_activity
    ADD CONSTRAINT time_activity_pkey PRIMARY KEY (id);

--
-- Name: time_client time_client_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_client
    ADD CONSTRAINT time_client_pkey PRIMARY KEY (id);

--
-- Name: time_entry time_entry_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_no_overlap EXCLUDE USING gist (employee_id WITH =, tstzrange(started_at, COALESCE(ended_at, 'infinity'::timestamp with time zone), '[)'::text) WITH &&) WHERE ((started_at IS NOT NULL));

--
-- Name: time_entry time_entry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_pkey PRIMARY KEY (id);

--
-- Name: time_project_member time_project_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project_member
    ADD CONSTRAINT time_project_member_pkey PRIMARY KEY (project_id, employee_id);

--
-- Name: time_project time_project_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project
    ADD CONSTRAINT time_project_pkey PRIMARY KEY (id);

--
-- Name: timesheet timesheet_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_pkey PRIMARY KEY (id);

--
-- Name: travel_bill travel_bill_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_bill
    ADD CONSTRAINT travel_bill_pkey PRIMARY KEY (id);

--
-- Name: travel_request travel_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_request
    ADD CONSTRAINT travel_request_pkey PRIMARY KEY (id);

--
-- Name: travel_settlement travel_settlement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_pkey PRIMARY KEY (id);

--
-- Name: user_group user_group_company_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group
    ADD CONSTRAINT user_group_company_id_name_key UNIQUE (company_id, name);

--
-- Name: user_group_member user_group_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group_member
    ADD CONSTRAINT user_group_member_pkey PRIMARY KEY (group_id, user_id);

--
-- Name: user_group user_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group
    ADD CONSTRAINT user_group_pkey PRIMARY KEY (id);

--
-- Name: user_recovery_code user_recovery_code_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_recovery_code
    ADD CONSTRAINT user_recovery_code_pkey PRIMARY KEY (id);

--
-- Name: work_break work_break_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_break
    ADD CONSTRAINT work_break_pkey PRIMARY KEY (id);

--
-- Name: workflow_definition workflow_definition_company_id_object_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definition
    ADD CONSTRAINT workflow_definition_company_id_object_type_key UNIQUE (company_id, object_type);

--
-- Name: workflow_definition workflow_definition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definition
    ADD CONSTRAINT workflow_definition_pkey PRIMARY KEY (id);

--
-- Name: workflow_instance workflow_instance_object_type_object_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_instance
    ADD CONSTRAINT workflow_instance_object_type_object_id_key UNIQUE (object_type, object_id);

--
-- Name: workflow_instance workflow_instance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_instance
    ADD CONSTRAINT workflow_instance_pkey PRIMARY KEY (id);

--
-- Name: announcement_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX announcement_company_idx ON public.announcement USING btree (company_id, created_at DESC) WHERE (deleted_at IS NULL);

--
-- Name: approval_flow_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX approval_flow_type_idx ON public.approval_flow USING btree (company_id, object_type) WHERE active;

--
-- Name: approver_delegate_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX approver_delegate_user_idx ON public.approver_delegate USING btree (user_id, from_date, to_date) WHERE (cancelled_at IS NULL);

--
-- Name: asset_assignment_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX asset_assignment_emp_idx ON public.asset_assignment USING btree (employee_id) WHERE (returned_on IS NULL);

--
-- Name: asset_assignment_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX asset_assignment_open_idx ON public.asset_assignment USING btree (asset_id) WHERE (returned_on IS NULL);

--
-- Name: attendance_day_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX attendance_day_date_idx ON public.attendance_day USING btree (local_date, employee_id, version DESC);

--
-- Name: attendance_day_flags_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX attendance_day_flags_gin ON public.attendance_day USING gin (flags jsonb_path_ops);

--
-- Name: attendance_day_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX attendance_day_latest_idx ON public.attendance_day USING btree (employee_id, local_date, version DESC);

--
-- Name: biometric_template_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX biometric_template_emp_idx ON public.biometric_template USING btree (employee_id, modality);

--
-- Name: career_event_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX career_event_due_idx ON public.career_event USING btree (effective_date) WHERE (status = 'scheduled'::text);

--
-- Name: career_event_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX career_event_emp_idx ON public.career_event USING btree (employee_id, effective_date);

--
-- Name: device_command_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX device_command_queue_idx ON public.device_command USING btree (device_id, status) WHERE (status = 'queued'::text);

--
-- Name: device_command_sent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX device_command_sent_idx ON public.device_command USING btree (device_id, sent_at) WHERE (status = 'sent'::text);

--
-- Name: disciplinary_case_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX disciplinary_case_employee_idx ON public.disciplinary_case USING btree (employee_id, created_at DESC);

--
-- Name: employee_confirmation_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_confirmation_emp_idx ON public.employee_confirmation USING btree (employee_id);

--
-- Name: employee_document_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_document_emp_idx ON public.employee_document USING btree (employee_id) WHERE (deleted_at IS NULL);

--
-- Name: employee_document_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_document_expiry_idx ON public.employee_document USING btree (company_id, expiry_date) WHERE ((deleted_at IS NULL) AND is_current AND (expiry_date IS NOT NULL));

--
-- Name: employee_education_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_education_emp_idx ON public.employee_education USING btree (employee_id);

--
-- Name: employee_emergency_contact_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_emergency_contact_emp_idx ON public.employee_emergency_contact USING btree (employee_id);

--
-- Name: employee_exit_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_exit_company_idx ON public.employee_exit USING btree (company_id, status);

--
-- Name: employee_exit_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX employee_exit_open_idx ON public.employee_exit USING btree (employee_id) WHERE (status = ANY (ARRAY['pending'::text, 'accepted'::text]));

--
-- Name: employee_experience_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_experience_emp_idx ON public.employee_experience USING btree (employee_id);

--
-- Name: employee_family_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_family_emp_idx ON public.employee_family USING btree (employee_id);

--
-- Name: employee_field_history_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_field_history_emp_idx ON public.employee_field_history USING btree (employee_id, changed_at DESC);

--
-- Name: employee_pan_bidx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_pan_bidx ON public.employee USING btree (pan_bidx) WHERE (pan_bidx IS NOT NULL);

--
-- Name: employee_temp_assignment_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX employee_temp_assignment_emp_idx ON public.employee_temp_assignment USING btree (employee_id, start_date);

--
-- Name: engage_comment_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX engage_comment_target_idx ON public.engage_comment USING btree (company_id, target_type, target_id, created_at);

--
-- Name: expense_claim_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX expense_claim_company_idx ON public.expense_claim USING btree (company_id, status);

--
-- Name: expense_claim_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX expense_claim_employee_idx ON public.expense_claim USING btree (employee_id, created_at DESC);

--
-- Name: expense_item_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX expense_item_claim_idx ON public.expense_item USING btree (claim_id);

--
-- Name: expense_payout_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX expense_payout_pending_idx ON public.expense_payout USING btree (company_id, employee_id) WHERE (status = 'pending'::text);

--
-- Name: expense_payout_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX expense_payout_run_idx ON public.expense_payout USING btree (payroll_run_id) WHERE (payroll_run_id IS NOT NULL);

--
-- Name: expense_payout_source_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX expense_payout_source_idx ON public.expense_payout USING btree (source_type, source_id, component_code) WHERE (parent_payout_id IS NULL);

--
-- Name: field_visit_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX field_visit_day_idx ON public.field_visit USING btree (company_id, local_date);

--
-- Name: field_visit_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX field_visit_open_idx ON public.field_visit USING btree (employee_id) WHERE (ended_at IS NULL);

--
-- Name: grievance_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX grievance_employee_idx ON public.grievance USING btree (employee_id, created_at DESC);

--
-- Name: grievance_note_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX grievance_note_idx ON public.grievance_note USING btree (grievance_id, created_at);

--
-- Name: helpdesk_message_ticket_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX helpdesk_message_ticket_idx ON public.helpdesk_message USING btree (ticket_id, created_at);

--
-- Name: helpdesk_ticket_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX helpdesk_ticket_queue_idx ON public.helpdesk_ticket USING btree (company_id, status, sla_due_at);

--
-- Name: helpdesk_ticket_requester_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX helpdesk_ticket_requester_idx ON public.helpdesk_ticket USING btree (requester_user_id, created_at DESC);

--
-- Name: hiring_application_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_application_company_idx ON public.hiring_application USING btree (company_id, created_at);

--
-- Name: hiring_application_job_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_application_job_idx ON public.hiring_application USING btree (job_id, status);

--
-- Name: hiring_candidate_email_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX hiring_candidate_email_idx ON public.hiring_candidate USING btree (company_id, lower(email)) WHERE (email IS NOT NULL);

--
-- Name: hiring_event_app_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_event_app_idx ON public.hiring_event USING btree (application_id, created_at);

--
-- Name: hiring_interview_app_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_interview_app_idx ON public.hiring_interview USING btree (application_id);

--
-- Name: hiring_interview_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_interview_company_idx ON public.hiring_interview USING btree (company_id, starts_at);

--
-- Name: hiring_job_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_job_company_idx ON public.hiring_job USING btree (company_id, status);

--
-- Name: hiring_offer_app_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_offer_app_idx ON public.hiring_offer USING btree (application_id);

--
-- Name: hiring_requisition_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hiring_requisition_company_idx ON public.hiring_requisition USING btree (company_id, status);

--
-- Name: issued_letter_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX issued_letter_emp_idx ON public.issued_letter USING btree (employee_id, issued_at);

--
-- Name: kiosk_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kiosk_company_idx ON public.kiosk USING btree (company_id);

--
-- Name: kiosk_pairing_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX kiosk_pairing_idx ON public.kiosk USING btree (pairing_code_hash) WHERE (pairing_code_hash IS NOT NULL);

--
-- Name: kudos_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kudos_company_idx ON public.kudos USING btree (company_id, created_at DESC) WHERE (deleted_at IS NULL);

--
-- Name: kudos_giver_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kudos_giver_idx ON public.kudos USING btree (giver_employee_id, created_at);

--
-- Name: kudos_recipient_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kudos_recipient_emp_idx ON public.kudos_recipient USING btree (employee_id);

--
-- Name: late_penalty_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX late_penalty_company_idx ON public.late_penalty USING btree (company_id, bs_year, bs_month);

--
-- Name: late_penalty_one_per_month; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX late_penalty_one_per_month ON public.late_penalty USING btree (employee_id, bs_year, bs_month) WHERE (status = 'applied'::text);

--
-- Name: leave_ledger_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leave_ledger_emp_idx ON public.leave_ledger USING btree (employee_id, leave_type_id, effective_date);

--
-- Name: leave_request_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leave_request_emp_idx ON public.leave_request USING btree (employee_id, from_date, to_date);

--
-- Name: letter_request_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX letter_request_company_idx ON public.letter_request USING btree (company_id, status, created_at);

--
-- Name: lifecycle_case_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lifecycle_case_company_idx ON public.lifecycle_case USING btree (company_id, kind, status);

--
-- Name: lifecycle_case_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX lifecycle_case_open_idx ON public.lifecycle_case USING btree (employee_id, kind) WHERE (status = 'active'::text);

--
-- Name: lifecycle_task_case_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lifecycle_task_case_idx ON public.lifecycle_task USING btree (case_id, sort);

--
-- Name: lifecycle_task_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lifecycle_task_open_idx ON public.lifecycle_task USING btree (company_id, due_date) WHERE (status = 'open'::text);

--
-- Name: lifecycle_template_task_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lifecycle_template_task_idx ON public.lifecycle_template_task USING btree (template_id, sort);

--
-- Name: location_assignment_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX location_assignment_employee_idx ON public.location_assignment USING btree (employee_id);

--
-- Name: login_challenge_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX login_challenge_user_idx ON public.login_challenge USING btree (user_id);

--
-- Name: lrn_enrollment_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lrn_enrollment_employee_idx ON public.lrn_enrollment USING btree (employee_id);

--
-- Name: lrn_enrollment_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lrn_enrollment_expiry_idx ON public.lrn_enrollment USING btree (certificate_expires_on) WHERE (certificate_expires_on IS NOT NULL);

--
-- Name: lrn_enrollment_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX lrn_enrollment_open_idx ON public.lrn_enrollment USING btree (course_id, employee_id) WHERE (status = ANY (ARRAY['pending_approval'::text, 'enrolled'::text]));

--
-- Name: notification_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_due_idx ON public.notification_outbox USING btree (next_attempt_at) WHERE (status = 'queued'::text);

--
-- Name: notification_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_queue_idx ON public.notification_outbox USING btree (status, created_at) WHERE (status = 'queued'::text);

--
-- Name: notification_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_user_idx ON public.notification_outbox USING btree (user_id, created_at DESC);

--
-- Name: ot_request_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ot_request_emp_idx ON public.ot_request USING btree (employee_id, local_date);

--
-- Name: payroll_hold_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payroll_hold_company_idx ON public.payroll_hold USING btree (company_id, status);

--
-- Name: payroll_opening_balance_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payroll_opening_balance_company_idx ON public.payroll_opening_balance USING btree (company_id, fiscal_year_start);

--
-- Name: payroll_run_period_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payroll_run_period_idx ON public.payroll_run USING btree (period_id, created_at DESC);

--
-- Name: payslip_line_payslip_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payslip_line_payslip_idx ON public.payslip_line USING btree (payslip_id);

--
-- Name: perf_checkin_goal_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_checkin_goal_idx ON public.perf_checkin USING btree (goal_id, created_at);

--
-- Name: perf_feedback_giver_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_feedback_giver_idx ON public.perf_feedback USING btree (giver_employee_id);

--
-- Name: perf_feedback_subject_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_feedback_subject_idx ON public.perf_feedback USING btree (subject_employee_id);

--
-- Name: perf_goal_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_goal_company_idx ON public.perf_goal USING btree (company_id, scope);

--
-- Name: perf_goal_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_goal_employee_idx ON public.perf_goal USING btree (employee_id);

--
-- Name: perf_journal_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_journal_employee_idx ON public.perf_journal USING btree (employee_id, note_date);

--
-- Name: perf_key_result_goal_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_key_result_goal_idx ON public.perf_key_result USING btree (goal_id);

--
-- Name: perf_kpi_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_kpi_company_idx ON public.perf_kpi USING btree (company_id, designation);

--
-- Name: perf_one_on_one_pair_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_one_on_one_pair_idx ON public.perf_one_on_one USING btree (manager_employee_id, employee_id, scheduled_at);

--
-- Name: perf_review_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_review_employee_idx ON public.perf_review USING btree (employee_id);

--
-- Name: perf_review_manager_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX perf_review_manager_idx ON public.perf_review USING btree (manager_employee_id);

--
-- Name: probation_review_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX probation_review_pending_idx ON public.probation_review USING btree (employee_id) WHERE (status = 'pending'::text);

--
-- Name: profile_change_request_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX profile_change_request_emp_idx ON public.profile_change_request USING btree (employee_id, created_at DESC);

--
-- Name: profile_change_request_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX profile_change_request_pending_idx ON public.profile_change_request USING btree (company_id) WHERE (status = 'pending'::text);

--
-- Name: punch_event_device_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_device_idx ON ONLY public.punch_event USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_01_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_01_device_id_punched_at_idx ON public.punch_event_2026_01 USING btree (device_id, punched_at);

--
-- Name: punch_event_employee_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_employee_date_idx ON ONLY public.punch_event USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_01_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_01_employee_id_local_date_idx ON public.punch_event_2026_01 USING btree (employee_id, local_date);

--
-- Name: punch_event_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_employee_idx ON ONLY public.punch_event USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_01_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_01_employee_id_punched_at_idx ON public.punch_event_2026_01 USING btree (employee_id, punched_at);

--
-- Name: punch_event_unmapped_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_unmapped_idx ON ONLY public.punch_event USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_01_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_01_ingested_at_idx ON public.punch_event_2026_01 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_brin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_brin ON ONLY public.punch_event USING brin (punched_at);

--
-- Name: punch_event_2026_01_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_01_punched_at_idx ON public.punch_event_2026_01 USING brin (punched_at);

--
-- Name: punch_event_2026_02_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_02_device_id_punched_at_idx ON public.punch_event_2026_02 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_02_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_02_employee_id_local_date_idx ON public.punch_event_2026_02 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_02_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_02_employee_id_punched_at_idx ON public.punch_event_2026_02 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_02_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_02_ingested_at_idx ON public.punch_event_2026_02 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_02_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_02_punched_at_idx ON public.punch_event_2026_02 USING brin (punched_at);

--
-- Name: punch_event_2026_03_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_03_device_id_punched_at_idx ON public.punch_event_2026_03 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_03_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_03_employee_id_local_date_idx ON public.punch_event_2026_03 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_03_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_03_employee_id_punched_at_idx ON public.punch_event_2026_03 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_03_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_03_ingested_at_idx ON public.punch_event_2026_03 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_03_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_03_punched_at_idx ON public.punch_event_2026_03 USING brin (punched_at);

--
-- Name: punch_event_2026_04_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_04_device_id_punched_at_idx ON public.punch_event_2026_04 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_04_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_04_employee_id_local_date_idx ON public.punch_event_2026_04 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_04_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_04_employee_id_punched_at_idx ON public.punch_event_2026_04 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_04_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_04_ingested_at_idx ON public.punch_event_2026_04 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_04_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_04_punched_at_idx ON public.punch_event_2026_04 USING brin (punched_at);

--
-- Name: punch_event_2026_05_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_05_device_id_punched_at_idx ON public.punch_event_2026_05 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_05_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_05_employee_id_local_date_idx ON public.punch_event_2026_05 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_05_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_05_employee_id_punched_at_idx ON public.punch_event_2026_05 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_05_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_05_ingested_at_idx ON public.punch_event_2026_05 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_05_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_05_punched_at_idx ON public.punch_event_2026_05 USING brin (punched_at);

--
-- Name: punch_event_2026_06_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_06_device_id_punched_at_idx ON public.punch_event_2026_06 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_06_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_06_employee_id_local_date_idx ON public.punch_event_2026_06 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_06_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_06_employee_id_punched_at_idx ON public.punch_event_2026_06 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_06_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_06_ingested_at_idx ON public.punch_event_2026_06 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_06_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_06_punched_at_idx ON public.punch_event_2026_06 USING brin (punched_at);

--
-- Name: punch_event_2026_07_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_07_device_id_punched_at_idx ON public.punch_event_2026_07 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_07_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_07_employee_id_local_date_idx ON public.punch_event_2026_07 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_07_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_07_employee_id_punched_at_idx ON public.punch_event_2026_07 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_07_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_07_ingested_at_idx ON public.punch_event_2026_07 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_07_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_07_punched_at_idx ON public.punch_event_2026_07 USING brin (punched_at);

--
-- Name: punch_event_2026_08_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_08_device_id_punched_at_idx ON public.punch_event_2026_08 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_08_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_08_employee_id_local_date_idx ON public.punch_event_2026_08 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_08_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_08_employee_id_punched_at_idx ON public.punch_event_2026_08 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_08_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_08_ingested_at_idx ON public.punch_event_2026_08 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_08_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_08_punched_at_idx ON public.punch_event_2026_08 USING brin (punched_at);

--
-- Name: punch_event_2026_09_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_09_device_id_punched_at_idx ON public.punch_event_2026_09 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_09_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_09_employee_id_local_date_idx ON public.punch_event_2026_09 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_09_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_09_employee_id_punched_at_idx ON public.punch_event_2026_09 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_09_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_09_ingested_at_idx ON public.punch_event_2026_09 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_09_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_09_punched_at_idx ON public.punch_event_2026_09 USING brin (punched_at);

--
-- Name: punch_event_2026_10_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_10_device_id_punched_at_idx ON public.punch_event_2026_10 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_10_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_10_employee_id_local_date_idx ON public.punch_event_2026_10 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_10_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_10_employee_id_punched_at_idx ON public.punch_event_2026_10 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_10_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_10_ingested_at_idx ON public.punch_event_2026_10 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_10_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_10_punched_at_idx ON public.punch_event_2026_10 USING brin (punched_at);

--
-- Name: punch_event_2026_11_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_11_device_id_punched_at_idx ON public.punch_event_2026_11 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_11_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_11_employee_id_local_date_idx ON public.punch_event_2026_11 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_11_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_11_employee_id_punched_at_idx ON public.punch_event_2026_11 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_11_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_11_ingested_at_idx ON public.punch_event_2026_11 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_11_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_11_punched_at_idx ON public.punch_event_2026_11 USING brin (punched_at);

--
-- Name: punch_event_2026_12_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_12_device_id_punched_at_idx ON public.punch_event_2026_12 USING btree (device_id, punched_at);

--
-- Name: punch_event_2026_12_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_12_employee_id_local_date_idx ON public.punch_event_2026_12 USING btree (employee_id, local_date);

--
-- Name: punch_event_2026_12_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_12_employee_id_punched_at_idx ON public.punch_event_2026_12 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2026_12_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_12_ingested_at_idx ON public.punch_event_2026_12 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2026_12_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2026_12_punched_at_idx ON public.punch_event_2026_12 USING brin (punched_at);

--
-- Name: punch_event_2027_01_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_01_device_id_punched_at_idx ON public.punch_event_2027_01 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_01_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_01_employee_id_local_date_idx ON public.punch_event_2027_01 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_01_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_01_employee_id_punched_at_idx ON public.punch_event_2027_01 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_01_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_01_ingested_at_idx ON public.punch_event_2027_01 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_01_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_01_punched_at_idx ON public.punch_event_2027_01 USING brin (punched_at);

--
-- Name: punch_event_2027_02_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_02_device_id_punched_at_idx ON public.punch_event_2027_02 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_02_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_02_employee_id_local_date_idx ON public.punch_event_2027_02 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_02_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_02_employee_id_punched_at_idx ON public.punch_event_2027_02 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_02_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_02_ingested_at_idx ON public.punch_event_2027_02 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_02_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_02_punched_at_idx ON public.punch_event_2027_02 USING brin (punched_at);

--
-- Name: punch_event_2027_03_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_03_device_id_punched_at_idx ON public.punch_event_2027_03 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_03_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_03_employee_id_local_date_idx ON public.punch_event_2027_03 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_03_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_03_employee_id_punched_at_idx ON public.punch_event_2027_03 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_03_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_03_ingested_at_idx ON public.punch_event_2027_03 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_03_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_03_punched_at_idx ON public.punch_event_2027_03 USING brin (punched_at);

--
-- Name: punch_event_2027_04_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_04_device_id_punched_at_idx ON public.punch_event_2027_04 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_04_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_04_employee_id_local_date_idx ON public.punch_event_2027_04 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_04_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_04_employee_id_punched_at_idx ON public.punch_event_2027_04 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_04_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_04_ingested_at_idx ON public.punch_event_2027_04 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_04_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_04_punched_at_idx ON public.punch_event_2027_04 USING brin (punched_at);

--
-- Name: punch_event_2027_05_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_05_device_id_punched_at_idx ON public.punch_event_2027_05 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_05_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_05_employee_id_local_date_idx ON public.punch_event_2027_05 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_05_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_05_employee_id_punched_at_idx ON public.punch_event_2027_05 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_05_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_05_ingested_at_idx ON public.punch_event_2027_05 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_05_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_05_punched_at_idx ON public.punch_event_2027_05 USING brin (punched_at);

--
-- Name: punch_event_2027_06_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_06_device_id_punched_at_idx ON public.punch_event_2027_06 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_06_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_06_employee_id_local_date_idx ON public.punch_event_2027_06 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_06_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_06_employee_id_punched_at_idx ON public.punch_event_2027_06 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_06_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_06_ingested_at_idx ON public.punch_event_2027_06 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_06_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_06_punched_at_idx ON public.punch_event_2027_06 USING brin (punched_at);

--
-- Name: punch_event_2027_07_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_07_device_id_punched_at_idx ON public.punch_event_2027_07 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_07_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_07_employee_id_local_date_idx ON public.punch_event_2027_07 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_07_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_07_employee_id_punched_at_idx ON public.punch_event_2027_07 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_07_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_07_ingested_at_idx ON public.punch_event_2027_07 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_07_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_07_punched_at_idx ON public.punch_event_2027_07 USING brin (punched_at);

--
-- Name: punch_event_2027_08_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_08_device_id_punched_at_idx ON public.punch_event_2027_08 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_08_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_08_employee_id_local_date_idx ON public.punch_event_2027_08 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_08_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_08_employee_id_punched_at_idx ON public.punch_event_2027_08 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_08_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_08_ingested_at_idx ON public.punch_event_2027_08 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_08_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_08_punched_at_idx ON public.punch_event_2027_08 USING brin (punched_at);

--
-- Name: punch_event_2027_09_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_09_device_id_punched_at_idx ON public.punch_event_2027_09 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_09_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_09_employee_id_local_date_idx ON public.punch_event_2027_09 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_09_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_09_employee_id_punched_at_idx ON public.punch_event_2027_09 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_09_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_09_ingested_at_idx ON public.punch_event_2027_09 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_09_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_09_punched_at_idx ON public.punch_event_2027_09 USING brin (punched_at);

--
-- Name: punch_event_2027_10_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_10_device_id_punched_at_idx ON public.punch_event_2027_10 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_10_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_10_employee_id_local_date_idx ON public.punch_event_2027_10 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_10_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_10_employee_id_punched_at_idx ON public.punch_event_2027_10 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_10_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_10_ingested_at_idx ON public.punch_event_2027_10 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_10_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_10_punched_at_idx ON public.punch_event_2027_10 USING brin (punched_at);

--
-- Name: punch_event_2027_11_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_11_device_id_punched_at_idx ON public.punch_event_2027_11 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_11_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_11_employee_id_local_date_idx ON public.punch_event_2027_11 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_11_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_11_employee_id_punched_at_idx ON public.punch_event_2027_11 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_11_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_11_ingested_at_idx ON public.punch_event_2027_11 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_11_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_11_punched_at_idx ON public.punch_event_2027_11 USING brin (punched_at);

--
-- Name: punch_event_2027_12_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_12_device_id_punched_at_idx ON public.punch_event_2027_12 USING btree (device_id, punched_at);

--
-- Name: punch_event_2027_12_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_12_employee_id_local_date_idx ON public.punch_event_2027_12 USING btree (employee_id, local_date);

--
-- Name: punch_event_2027_12_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_12_employee_id_punched_at_idx ON public.punch_event_2027_12 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2027_12_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_12_ingested_at_idx ON public.punch_event_2027_12 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2027_12_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2027_12_punched_at_idx ON public.punch_event_2027_12 USING brin (punched_at);

--
-- Name: punch_event_2028_01_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_01_device_id_punched_at_idx ON public.punch_event_2028_01 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_01_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_01_employee_id_local_date_idx ON public.punch_event_2028_01 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_01_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_01_employee_id_punched_at_idx ON public.punch_event_2028_01 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_01_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_01_ingested_at_idx ON public.punch_event_2028_01 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_01_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_01_punched_at_idx ON public.punch_event_2028_01 USING brin (punched_at);

--
-- Name: punch_event_2028_02_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_02_device_id_punched_at_idx ON public.punch_event_2028_02 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_02_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_02_employee_id_local_date_idx ON public.punch_event_2028_02 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_02_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_02_employee_id_punched_at_idx ON public.punch_event_2028_02 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_02_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_02_ingested_at_idx ON public.punch_event_2028_02 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_02_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_02_punched_at_idx ON public.punch_event_2028_02 USING brin (punched_at);

--
-- Name: punch_event_2028_03_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_03_device_id_punched_at_idx ON public.punch_event_2028_03 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_03_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_03_employee_id_local_date_idx ON public.punch_event_2028_03 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_03_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_03_employee_id_punched_at_idx ON public.punch_event_2028_03 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_03_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_03_ingested_at_idx ON public.punch_event_2028_03 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_03_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_03_punched_at_idx ON public.punch_event_2028_03 USING brin (punched_at);

--
-- Name: punch_event_2028_04_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_04_device_id_punched_at_idx ON public.punch_event_2028_04 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_04_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_04_employee_id_local_date_idx ON public.punch_event_2028_04 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_04_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_04_employee_id_punched_at_idx ON public.punch_event_2028_04 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_04_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_04_ingested_at_idx ON public.punch_event_2028_04 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_04_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_04_punched_at_idx ON public.punch_event_2028_04 USING brin (punched_at);

--
-- Name: punch_event_2028_05_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_05_device_id_punched_at_idx ON public.punch_event_2028_05 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_05_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_05_employee_id_local_date_idx ON public.punch_event_2028_05 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_05_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_05_employee_id_punched_at_idx ON public.punch_event_2028_05 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_05_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_05_ingested_at_idx ON public.punch_event_2028_05 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_05_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_05_punched_at_idx ON public.punch_event_2028_05 USING brin (punched_at);

--
-- Name: punch_event_2028_06_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_06_device_id_punched_at_idx ON public.punch_event_2028_06 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_06_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_06_employee_id_local_date_idx ON public.punch_event_2028_06 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_06_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_06_employee_id_punched_at_idx ON public.punch_event_2028_06 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_06_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_06_ingested_at_idx ON public.punch_event_2028_06 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_06_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_06_punched_at_idx ON public.punch_event_2028_06 USING brin (punched_at);

--
-- Name: punch_event_2028_07_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_07_device_id_punched_at_idx ON public.punch_event_2028_07 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_07_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_07_employee_id_local_date_idx ON public.punch_event_2028_07 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_07_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_07_employee_id_punched_at_idx ON public.punch_event_2028_07 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_07_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_07_ingested_at_idx ON public.punch_event_2028_07 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_07_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_07_punched_at_idx ON public.punch_event_2028_07 USING brin (punched_at);

--
-- Name: punch_event_2028_08_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_08_device_id_punched_at_idx ON public.punch_event_2028_08 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_08_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_08_employee_id_local_date_idx ON public.punch_event_2028_08 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_08_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_08_employee_id_punched_at_idx ON public.punch_event_2028_08 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_08_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_08_ingested_at_idx ON public.punch_event_2028_08 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_08_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_08_punched_at_idx ON public.punch_event_2028_08 USING brin (punched_at);

--
-- Name: punch_event_2028_09_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_09_device_id_punched_at_idx ON public.punch_event_2028_09 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_09_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_09_employee_id_local_date_idx ON public.punch_event_2028_09 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_09_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_09_employee_id_punched_at_idx ON public.punch_event_2028_09 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_09_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_09_ingested_at_idx ON public.punch_event_2028_09 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_09_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_09_punched_at_idx ON public.punch_event_2028_09 USING brin (punched_at);

--
-- Name: punch_event_2028_10_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_10_device_id_punched_at_idx ON public.punch_event_2028_10 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_10_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_10_employee_id_local_date_idx ON public.punch_event_2028_10 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_10_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_10_employee_id_punched_at_idx ON public.punch_event_2028_10 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_10_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_10_ingested_at_idx ON public.punch_event_2028_10 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_10_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_10_punched_at_idx ON public.punch_event_2028_10 USING brin (punched_at);

--
-- Name: punch_event_2028_11_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_11_device_id_punched_at_idx ON public.punch_event_2028_11 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_11_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_11_employee_id_local_date_idx ON public.punch_event_2028_11 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_11_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_11_employee_id_punched_at_idx ON public.punch_event_2028_11 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_11_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_11_ingested_at_idx ON public.punch_event_2028_11 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_11_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_11_punched_at_idx ON public.punch_event_2028_11 USING brin (punched_at);

--
-- Name: punch_event_2028_12_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_12_device_id_punched_at_idx ON public.punch_event_2028_12 USING btree (device_id, punched_at);

--
-- Name: punch_event_2028_12_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_12_employee_id_local_date_idx ON public.punch_event_2028_12 USING btree (employee_id, local_date);

--
-- Name: punch_event_2028_12_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_12_employee_id_punched_at_idx ON public.punch_event_2028_12 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2028_12_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_12_ingested_at_idx ON public.punch_event_2028_12 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2028_12_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2028_12_punched_at_idx ON public.punch_event_2028_12 USING brin (punched_at);

--
-- Name: punch_event_2029_01_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_01_device_id_punched_at_idx ON public.punch_event_2029_01 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_01_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_01_employee_id_local_date_idx ON public.punch_event_2029_01 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_01_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_01_employee_id_punched_at_idx ON public.punch_event_2029_01 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_01_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_01_ingested_at_idx ON public.punch_event_2029_01 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_01_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_01_punched_at_idx ON public.punch_event_2029_01 USING brin (punched_at);

--
-- Name: punch_event_2029_02_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_02_device_id_punched_at_idx ON public.punch_event_2029_02 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_02_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_02_employee_id_local_date_idx ON public.punch_event_2029_02 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_02_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_02_employee_id_punched_at_idx ON public.punch_event_2029_02 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_02_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_02_ingested_at_idx ON public.punch_event_2029_02 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_02_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_02_punched_at_idx ON public.punch_event_2029_02 USING brin (punched_at);

--
-- Name: punch_event_2029_03_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_03_device_id_punched_at_idx ON public.punch_event_2029_03 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_03_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_03_employee_id_local_date_idx ON public.punch_event_2029_03 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_03_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_03_employee_id_punched_at_idx ON public.punch_event_2029_03 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_03_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_03_ingested_at_idx ON public.punch_event_2029_03 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_03_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_03_punched_at_idx ON public.punch_event_2029_03 USING brin (punched_at);

--
-- Name: punch_event_2029_04_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_04_device_id_punched_at_idx ON public.punch_event_2029_04 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_04_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_04_employee_id_local_date_idx ON public.punch_event_2029_04 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_04_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_04_employee_id_punched_at_idx ON public.punch_event_2029_04 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_04_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_04_ingested_at_idx ON public.punch_event_2029_04 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_04_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_04_punched_at_idx ON public.punch_event_2029_04 USING brin (punched_at);

--
-- Name: punch_event_2029_05_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_05_device_id_punched_at_idx ON public.punch_event_2029_05 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_05_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_05_employee_id_local_date_idx ON public.punch_event_2029_05 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_05_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_05_employee_id_punched_at_idx ON public.punch_event_2029_05 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_05_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_05_ingested_at_idx ON public.punch_event_2029_05 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_05_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_05_punched_at_idx ON public.punch_event_2029_05 USING brin (punched_at);

--
-- Name: punch_event_2029_06_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_06_device_id_punched_at_idx ON public.punch_event_2029_06 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_06_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_06_employee_id_local_date_idx ON public.punch_event_2029_06 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_06_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_06_employee_id_punched_at_idx ON public.punch_event_2029_06 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_06_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_06_ingested_at_idx ON public.punch_event_2029_06 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_06_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_06_punched_at_idx ON public.punch_event_2029_06 USING brin (punched_at);

--
-- Name: punch_event_2029_07_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_07_device_id_punched_at_idx ON public.punch_event_2029_07 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_07_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_07_employee_id_local_date_idx ON public.punch_event_2029_07 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_07_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_07_employee_id_punched_at_idx ON public.punch_event_2029_07 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_07_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_07_ingested_at_idx ON public.punch_event_2029_07 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_07_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_07_punched_at_idx ON public.punch_event_2029_07 USING brin (punched_at);

--
-- Name: punch_event_2029_08_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_08_device_id_punched_at_idx ON public.punch_event_2029_08 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_08_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_08_employee_id_local_date_idx ON public.punch_event_2029_08 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_08_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_08_employee_id_punched_at_idx ON public.punch_event_2029_08 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_08_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_08_ingested_at_idx ON public.punch_event_2029_08 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_08_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_08_punched_at_idx ON public.punch_event_2029_08 USING brin (punched_at);

--
-- Name: punch_event_2029_09_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_09_device_id_punched_at_idx ON public.punch_event_2029_09 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_09_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_09_employee_id_local_date_idx ON public.punch_event_2029_09 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_09_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_09_employee_id_punched_at_idx ON public.punch_event_2029_09 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_09_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_09_ingested_at_idx ON public.punch_event_2029_09 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_09_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_09_punched_at_idx ON public.punch_event_2029_09 USING brin (punched_at);

--
-- Name: punch_event_2029_10_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_10_device_id_punched_at_idx ON public.punch_event_2029_10 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_10_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_10_employee_id_local_date_idx ON public.punch_event_2029_10 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_10_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_10_employee_id_punched_at_idx ON public.punch_event_2029_10 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_10_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_10_ingested_at_idx ON public.punch_event_2029_10 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_10_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_10_punched_at_idx ON public.punch_event_2029_10 USING brin (punched_at);

--
-- Name: punch_event_2029_11_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_11_device_id_punched_at_idx ON public.punch_event_2029_11 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_11_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_11_employee_id_local_date_idx ON public.punch_event_2029_11 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_11_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_11_employee_id_punched_at_idx ON public.punch_event_2029_11 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_11_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_11_ingested_at_idx ON public.punch_event_2029_11 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_11_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_11_punched_at_idx ON public.punch_event_2029_11 USING brin (punched_at);

--
-- Name: punch_event_2029_12_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_12_device_id_punched_at_idx ON public.punch_event_2029_12 USING btree (device_id, punched_at);

--
-- Name: punch_event_2029_12_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_12_employee_id_local_date_idx ON public.punch_event_2029_12 USING btree (employee_id, local_date);

--
-- Name: punch_event_2029_12_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_12_employee_id_punched_at_idx ON public.punch_event_2029_12 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2029_12_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_12_ingested_at_idx ON public.punch_event_2029_12 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2029_12_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2029_12_punched_at_idx ON public.punch_event_2029_12 USING brin (punched_at);

--
-- Name: punch_event_2030_01_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_01_device_id_punched_at_idx ON public.punch_event_2030_01 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_01_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_01_employee_id_local_date_idx ON public.punch_event_2030_01 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_01_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_01_employee_id_punched_at_idx ON public.punch_event_2030_01 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_01_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_01_ingested_at_idx ON public.punch_event_2030_01 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_01_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_01_punched_at_idx ON public.punch_event_2030_01 USING brin (punched_at);

--
-- Name: punch_event_2030_02_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_02_device_id_punched_at_idx ON public.punch_event_2030_02 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_02_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_02_employee_id_local_date_idx ON public.punch_event_2030_02 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_02_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_02_employee_id_punched_at_idx ON public.punch_event_2030_02 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_02_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_02_ingested_at_idx ON public.punch_event_2030_02 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_02_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_02_punched_at_idx ON public.punch_event_2030_02 USING brin (punched_at);

--
-- Name: punch_event_2030_03_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_03_device_id_punched_at_idx ON public.punch_event_2030_03 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_03_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_03_employee_id_local_date_idx ON public.punch_event_2030_03 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_03_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_03_employee_id_punched_at_idx ON public.punch_event_2030_03 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_03_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_03_ingested_at_idx ON public.punch_event_2030_03 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_03_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_03_punched_at_idx ON public.punch_event_2030_03 USING brin (punched_at);

--
-- Name: punch_event_2030_04_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_04_device_id_punched_at_idx ON public.punch_event_2030_04 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_04_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_04_employee_id_local_date_idx ON public.punch_event_2030_04 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_04_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_04_employee_id_punched_at_idx ON public.punch_event_2030_04 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_04_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_04_ingested_at_idx ON public.punch_event_2030_04 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_04_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_04_punched_at_idx ON public.punch_event_2030_04 USING brin (punched_at);

--
-- Name: punch_event_2030_05_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_05_device_id_punched_at_idx ON public.punch_event_2030_05 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_05_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_05_employee_id_local_date_idx ON public.punch_event_2030_05 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_05_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_05_employee_id_punched_at_idx ON public.punch_event_2030_05 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_05_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_05_ingested_at_idx ON public.punch_event_2030_05 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_05_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_05_punched_at_idx ON public.punch_event_2030_05 USING brin (punched_at);

--
-- Name: punch_event_2030_06_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_06_device_id_punched_at_idx ON public.punch_event_2030_06 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_06_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_06_employee_id_local_date_idx ON public.punch_event_2030_06 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_06_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_06_employee_id_punched_at_idx ON public.punch_event_2030_06 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_06_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_06_ingested_at_idx ON public.punch_event_2030_06 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_06_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_06_punched_at_idx ON public.punch_event_2030_06 USING brin (punched_at);

--
-- Name: punch_event_2030_07_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_07_device_id_punched_at_idx ON public.punch_event_2030_07 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_07_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_07_employee_id_local_date_idx ON public.punch_event_2030_07 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_07_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_07_employee_id_punched_at_idx ON public.punch_event_2030_07 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_07_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_07_ingested_at_idx ON public.punch_event_2030_07 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_07_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_07_punched_at_idx ON public.punch_event_2030_07 USING brin (punched_at);

--
-- Name: punch_event_2030_08_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_08_device_id_punched_at_idx ON public.punch_event_2030_08 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_08_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_08_employee_id_local_date_idx ON public.punch_event_2030_08 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_08_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_08_employee_id_punched_at_idx ON public.punch_event_2030_08 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_08_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_08_ingested_at_idx ON public.punch_event_2030_08 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_08_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_08_punched_at_idx ON public.punch_event_2030_08 USING brin (punched_at);

--
-- Name: punch_event_2030_09_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_09_device_id_punched_at_idx ON public.punch_event_2030_09 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_09_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_09_employee_id_local_date_idx ON public.punch_event_2030_09 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_09_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_09_employee_id_punched_at_idx ON public.punch_event_2030_09 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_09_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_09_ingested_at_idx ON public.punch_event_2030_09 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_09_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_09_punched_at_idx ON public.punch_event_2030_09 USING brin (punched_at);

--
-- Name: punch_event_2030_10_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_10_device_id_punched_at_idx ON public.punch_event_2030_10 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_10_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_10_employee_id_local_date_idx ON public.punch_event_2030_10 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_10_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_10_employee_id_punched_at_idx ON public.punch_event_2030_10 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_10_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_10_ingested_at_idx ON public.punch_event_2030_10 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_10_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_10_punched_at_idx ON public.punch_event_2030_10 USING brin (punched_at);

--
-- Name: punch_event_2030_11_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_11_device_id_punched_at_idx ON public.punch_event_2030_11 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_11_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_11_employee_id_local_date_idx ON public.punch_event_2030_11 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_11_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_11_employee_id_punched_at_idx ON public.punch_event_2030_11 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_11_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_11_ingested_at_idx ON public.punch_event_2030_11 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_11_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_11_punched_at_idx ON public.punch_event_2030_11 USING brin (punched_at);

--
-- Name: punch_event_2030_12_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_12_device_id_punched_at_idx ON public.punch_event_2030_12 USING btree (device_id, punched_at);

--
-- Name: punch_event_2030_12_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_12_employee_id_local_date_idx ON public.punch_event_2030_12 USING btree (employee_id, local_date);

--
-- Name: punch_event_2030_12_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_12_employee_id_punched_at_idx ON public.punch_event_2030_12 USING btree (employee_id, punched_at);

--
-- Name: punch_event_2030_12_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_12_ingested_at_idx ON public.punch_event_2030_12 USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_2030_12_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_2030_12_punched_at_idx ON public.punch_event_2030_12 USING brin (punched_at);

--
-- Name: punch_event_default_device_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_default_device_id_punched_at_idx ON public.punch_event_default USING btree (device_id, punched_at);

--
-- Name: punch_event_default_employee_id_local_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_default_employee_id_local_date_idx ON public.punch_event_default USING btree (employee_id, local_date);

--
-- Name: punch_event_default_employee_id_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_default_employee_id_punched_at_idx ON public.punch_event_default USING btree (employee_id, punched_at);

--
-- Name: punch_event_default_ingested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_default_ingested_at_idx ON public.punch_event_default USING btree (ingested_at) WHERE (employee_id IS NULL);

--
-- Name: punch_event_default_punched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_event_default_punched_at_idx ON public.punch_event_default USING brin (punched_at);

--
-- Name: punch_photo_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_photo_company_idx ON public.punch_photo USING btree (company_id, local_date);

--
-- Name: punch_photo_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX punch_photo_employee_idx ON public.punch_photo USING btree (employee_id, local_date);

--
-- Name: punch_photo_punch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX punch_photo_punch_idx ON public.punch_photo USING btree (punch_id);

--
-- Name: registered_device_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX registered_device_employee_idx ON public.registered_device USING btree (employee_id);

--
-- Name: regularisation_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX regularisation_emp_idx ON public.regularisation_request USING btree (employee_id, local_date);

--
-- Name: rule_version_key_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX rule_version_key_idx ON public.rule_version USING btree (rule_key, jurisdiction, valid_from_ad);

--
-- Name: saved_report_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX saved_report_company_idx ON public.saved_report USING btree (company_id, created_by);

--
-- Name: session_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX session_id_idx ON public.session USING btree (id);

--
-- Name: session_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX session_user_idx ON public.session USING btree (user_id);

--
-- Name: shift_swap_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX shift_swap_company_idx ON public.shift_swap USING btree (company_id, local_date);

--
-- Name: shift_swap_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX shift_swap_open_idx ON public.shift_swap USING btree (requester_id, local_date) WHERE (status = ANY (ARRAY['awaiting_colleague'::text, 'pending'::text]));

--
-- Name: stored_file_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stored_file_company_idx ON public.stored_file USING btree (company_id, created_at);

--
-- Name: stored_file_unattached_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stored_file_unattached_idx ON public.stored_file USING btree (created_at) WHERE ((attached_to IS NULL) AND (deleted_at IS NULL));

--
-- Name: suggestion_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX suggestion_company_idx ON public.suggestion USING btree (company_id, received_on DESC);

--
-- Name: survey_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX survey_company_idx ON public.survey USING btree (company_id, status, closes_on);

--
-- Name: task_assignee_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_assignee_user_idx ON public.task_assignee USING btree (user_id);

--
-- Name: task_board_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_board_company_idx ON public.task_board USING btree (company_id);

--
-- Name: task_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_board_idx ON public.task USING btree (board_id, column_id, "position");

--
-- Name: task_board_member_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_board_member_user_idx ON public.task_board_member USING btree (user_id);

--
-- Name: task_board_one_personal; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX task_board_one_personal ON public.task_board USING btree (owner_user_id) WHERE (kind = 'personal'::text);

--
-- Name: task_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_due_idx ON public.task USING btree (company_id, due_date) WHERE ((completed_at IS NULL) AND (archived_at IS NULL));

--
-- Name: tax_declaration_request_emp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tax_declaration_request_emp_idx ON public.tax_declaration_request USING btree (employee_id, fiscal_year_start, created_at DESC);

--
-- Name: tax_declaration_request_one_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX tax_declaration_request_one_pending ON public.tax_declaration_request USING btree (employee_id, fiscal_year_start) WHERE (status = 'pending'::text);

--
-- Name: time_activity_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX time_activity_name_idx ON public.time_activity USING btree (company_id, COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name));

--
-- Name: time_client_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX time_client_name_idx ON public.time_client USING btree (company_id, lower(name));

--
-- Name: time_entry_company_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX time_entry_company_date_idx ON public.time_entry USING btree (company_id, local_date);

--
-- Name: time_entry_employee_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX time_entry_employee_date_idx ON public.time_entry USING btree (employee_id, local_date);

--
-- Name: time_entry_one_running_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX time_entry_one_running_idx ON public.time_entry USING btree (employee_id) WHERE ((ended_at IS NULL) AND (duration_min IS NULL));

--
-- Name: time_entry_project_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX time_entry_project_idx ON public.time_entry USING btree (project_id);

--
-- Name: time_project_code_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX time_project_code_idx ON public.time_project USING btree (company_id, upper(code));

--
-- Name: timesheet_company_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX timesheet_company_week_idx ON public.timesheet USING btree (company_id, week_start);

--
-- Name: timesheet_live_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX timesheet_live_week_idx ON public.timesheet USING btree (employee_id, week_start) WHERE (status = ANY (ARRAY['submitted'::text, 'approved'::text]));

--
-- Name: travel_bill_settlement_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX travel_bill_settlement_idx ON public.travel_bill USING btree (settlement_id);

--
-- Name: travel_request_company_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX travel_request_company_idx ON public.travel_request USING btree (company_id, status);

--
-- Name: travel_request_employee_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX travel_request_employee_idx ON public.travel_request USING btree (employee_id, from_date DESC);

--
-- Name: travel_settlement_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX travel_settlement_active_idx ON public.travel_settlement USING btree (travel_request_id) WHERE (status = ANY (ARRAY['pending'::text, 'approved'::text]));

--
-- Name: user_recovery_code_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_recovery_code_user_idx ON public.user_recovery_code USING btree (user_id) WHERE (used_at IS NULL);

--
-- Name: work_break_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX work_break_day_idx ON public.work_break USING btree (company_id, local_date);

--
-- Name: work_break_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX work_break_open_idx ON public.work_break USING btree (employee_id) WHERE (ended_at IS NULL);

--
-- Name: workflow_instance_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX workflow_instance_pending_idx ON public.workflow_instance USING btree (company_id, state) WHERE (state = 'pending'::text);

--
-- Name: punch_event_2026_01_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_01_device_id_punched_at_idx;

--
-- Name: punch_event_2026_01_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_01_employee_id_local_date_idx;

--
-- Name: punch_event_2026_01_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_01_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_01_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_01_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_01_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_01_ingested_at_idx;

--
-- Name: punch_event_2026_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_01_pkey;

--
-- Name: punch_event_2026_01_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_01_punched_at_idx;

--
-- Name: punch_event_2026_02_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_02_device_id_punched_at_idx;

--
-- Name: punch_event_2026_02_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_02_employee_id_local_date_idx;

--
-- Name: punch_event_2026_02_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_02_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_02_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_02_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_02_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_02_ingested_at_idx;

--
-- Name: punch_event_2026_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_02_pkey;

--
-- Name: punch_event_2026_02_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_02_punched_at_idx;

--
-- Name: punch_event_2026_03_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_03_device_id_punched_at_idx;

--
-- Name: punch_event_2026_03_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_03_employee_id_local_date_idx;

--
-- Name: punch_event_2026_03_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_03_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_03_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_03_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_03_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_03_ingested_at_idx;

--
-- Name: punch_event_2026_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_03_pkey;

--
-- Name: punch_event_2026_03_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_03_punched_at_idx;

--
-- Name: punch_event_2026_04_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_04_device_id_punched_at_idx;

--
-- Name: punch_event_2026_04_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_04_employee_id_local_date_idx;

--
-- Name: punch_event_2026_04_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_04_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_04_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_04_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_04_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_04_ingested_at_idx;

--
-- Name: punch_event_2026_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_04_pkey;

--
-- Name: punch_event_2026_04_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_04_punched_at_idx;

--
-- Name: punch_event_2026_05_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_05_device_id_punched_at_idx;

--
-- Name: punch_event_2026_05_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_05_employee_id_local_date_idx;

--
-- Name: punch_event_2026_05_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_05_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_05_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_05_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_05_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_05_ingested_at_idx;

--
-- Name: punch_event_2026_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_05_pkey;

--
-- Name: punch_event_2026_05_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_05_punched_at_idx;

--
-- Name: punch_event_2026_06_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_06_device_id_punched_at_idx;

--
-- Name: punch_event_2026_06_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_06_employee_id_local_date_idx;

--
-- Name: punch_event_2026_06_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_06_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_06_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_06_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_06_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_06_ingested_at_idx;

--
-- Name: punch_event_2026_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_06_pkey;

--
-- Name: punch_event_2026_06_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_06_punched_at_idx;

--
-- Name: punch_event_2026_07_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_07_device_id_punched_at_idx;

--
-- Name: punch_event_2026_07_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_07_employee_id_local_date_idx;

--
-- Name: punch_event_2026_07_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_07_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_07_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_07_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_07_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_07_ingested_at_idx;

--
-- Name: punch_event_2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_07_pkey;

--
-- Name: punch_event_2026_07_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_07_punched_at_idx;

--
-- Name: punch_event_2026_08_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_08_device_id_punched_at_idx;

--
-- Name: punch_event_2026_08_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_08_employee_id_local_date_idx;

--
-- Name: punch_event_2026_08_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_08_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_08_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_08_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_08_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_08_ingested_at_idx;

--
-- Name: punch_event_2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_08_pkey;

--
-- Name: punch_event_2026_08_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_08_punched_at_idx;

--
-- Name: punch_event_2026_09_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_09_device_id_punched_at_idx;

--
-- Name: punch_event_2026_09_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_09_employee_id_local_date_idx;

--
-- Name: punch_event_2026_09_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_09_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_09_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_09_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_09_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_09_ingested_at_idx;

--
-- Name: punch_event_2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_09_pkey;

--
-- Name: punch_event_2026_09_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_09_punched_at_idx;

--
-- Name: punch_event_2026_10_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_10_device_id_punched_at_idx;

--
-- Name: punch_event_2026_10_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_10_employee_id_local_date_idx;

--
-- Name: punch_event_2026_10_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_10_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_10_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_10_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_10_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_10_ingested_at_idx;

--
-- Name: punch_event_2026_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_10_pkey;

--
-- Name: punch_event_2026_10_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_10_punched_at_idx;

--
-- Name: punch_event_2026_11_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_11_device_id_punched_at_idx;

--
-- Name: punch_event_2026_11_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_11_employee_id_local_date_idx;

--
-- Name: punch_event_2026_11_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_11_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_11_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_11_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_11_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_11_ingested_at_idx;

--
-- Name: punch_event_2026_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_11_pkey;

--
-- Name: punch_event_2026_11_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_11_punched_at_idx;

--
-- Name: punch_event_2026_12_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2026_12_device_id_punched_at_idx;

--
-- Name: punch_event_2026_12_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2026_12_employee_id_local_date_idx;

--
-- Name: punch_event_2026_12_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2026_12_employee_id_punched_at_idx;

--
-- Name: punch_event_2026_12_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2026_12_idempotency_key_punched_at_key;

--
-- Name: punch_event_2026_12_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2026_12_ingested_at_idx;

--
-- Name: punch_event_2026_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2026_12_pkey;

--
-- Name: punch_event_2026_12_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2026_12_punched_at_idx;

--
-- Name: punch_event_2027_01_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_01_device_id_punched_at_idx;

--
-- Name: punch_event_2027_01_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_01_employee_id_local_date_idx;

--
-- Name: punch_event_2027_01_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_01_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_01_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_01_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_01_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_01_ingested_at_idx;

--
-- Name: punch_event_2027_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_01_pkey;

--
-- Name: punch_event_2027_01_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_01_punched_at_idx;

--
-- Name: punch_event_2027_02_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_02_device_id_punched_at_idx;

--
-- Name: punch_event_2027_02_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_02_employee_id_local_date_idx;

--
-- Name: punch_event_2027_02_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_02_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_02_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_02_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_02_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_02_ingested_at_idx;

--
-- Name: punch_event_2027_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_02_pkey;

--
-- Name: punch_event_2027_02_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_02_punched_at_idx;

--
-- Name: punch_event_2027_03_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_03_device_id_punched_at_idx;

--
-- Name: punch_event_2027_03_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_03_employee_id_local_date_idx;

--
-- Name: punch_event_2027_03_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_03_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_03_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_03_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_03_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_03_ingested_at_idx;

--
-- Name: punch_event_2027_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_03_pkey;

--
-- Name: punch_event_2027_03_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_03_punched_at_idx;

--
-- Name: punch_event_2027_04_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_04_device_id_punched_at_idx;

--
-- Name: punch_event_2027_04_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_04_employee_id_local_date_idx;

--
-- Name: punch_event_2027_04_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_04_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_04_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_04_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_04_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_04_ingested_at_idx;

--
-- Name: punch_event_2027_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_04_pkey;

--
-- Name: punch_event_2027_04_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_04_punched_at_idx;

--
-- Name: punch_event_2027_05_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_05_device_id_punched_at_idx;

--
-- Name: punch_event_2027_05_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_05_employee_id_local_date_idx;

--
-- Name: punch_event_2027_05_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_05_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_05_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_05_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_05_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_05_ingested_at_idx;

--
-- Name: punch_event_2027_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_05_pkey;

--
-- Name: punch_event_2027_05_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_05_punched_at_idx;

--
-- Name: punch_event_2027_06_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_06_device_id_punched_at_idx;

--
-- Name: punch_event_2027_06_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_06_employee_id_local_date_idx;

--
-- Name: punch_event_2027_06_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_06_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_06_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_06_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_06_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_06_ingested_at_idx;

--
-- Name: punch_event_2027_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_06_pkey;

--
-- Name: punch_event_2027_06_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_06_punched_at_idx;

--
-- Name: punch_event_2027_07_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_07_device_id_punched_at_idx;

--
-- Name: punch_event_2027_07_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_07_employee_id_local_date_idx;

--
-- Name: punch_event_2027_07_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_07_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_07_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_07_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_07_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_07_ingested_at_idx;

--
-- Name: punch_event_2027_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_07_pkey;

--
-- Name: punch_event_2027_07_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_07_punched_at_idx;

--
-- Name: punch_event_2027_08_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_08_device_id_punched_at_idx;

--
-- Name: punch_event_2027_08_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_08_employee_id_local_date_idx;

--
-- Name: punch_event_2027_08_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_08_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_08_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_08_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_08_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_08_ingested_at_idx;

--
-- Name: punch_event_2027_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_08_pkey;

--
-- Name: punch_event_2027_08_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_08_punched_at_idx;

--
-- Name: punch_event_2027_09_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_09_device_id_punched_at_idx;

--
-- Name: punch_event_2027_09_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_09_employee_id_local_date_idx;

--
-- Name: punch_event_2027_09_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_09_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_09_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_09_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_09_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_09_ingested_at_idx;

--
-- Name: punch_event_2027_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_09_pkey;

--
-- Name: punch_event_2027_09_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_09_punched_at_idx;

--
-- Name: punch_event_2027_10_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_10_device_id_punched_at_idx;

--
-- Name: punch_event_2027_10_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_10_employee_id_local_date_idx;

--
-- Name: punch_event_2027_10_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_10_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_10_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_10_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_10_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_10_ingested_at_idx;

--
-- Name: punch_event_2027_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_10_pkey;

--
-- Name: punch_event_2027_10_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_10_punched_at_idx;

--
-- Name: punch_event_2027_11_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_11_device_id_punched_at_idx;

--
-- Name: punch_event_2027_11_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_11_employee_id_local_date_idx;

--
-- Name: punch_event_2027_11_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_11_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_11_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_11_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_11_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_11_ingested_at_idx;

--
-- Name: punch_event_2027_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_11_pkey;

--
-- Name: punch_event_2027_11_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_11_punched_at_idx;

--
-- Name: punch_event_2027_12_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2027_12_device_id_punched_at_idx;

--
-- Name: punch_event_2027_12_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2027_12_employee_id_local_date_idx;

--
-- Name: punch_event_2027_12_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2027_12_employee_id_punched_at_idx;

--
-- Name: punch_event_2027_12_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2027_12_idempotency_key_punched_at_key;

--
-- Name: punch_event_2027_12_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2027_12_ingested_at_idx;

--
-- Name: punch_event_2027_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2027_12_pkey;

--
-- Name: punch_event_2027_12_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2027_12_punched_at_idx;

--
-- Name: punch_event_2028_01_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_01_device_id_punched_at_idx;

--
-- Name: punch_event_2028_01_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_01_employee_id_local_date_idx;

--
-- Name: punch_event_2028_01_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_01_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_01_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_01_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_01_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_01_ingested_at_idx;

--
-- Name: punch_event_2028_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_01_pkey;

--
-- Name: punch_event_2028_01_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_01_punched_at_idx;

--
-- Name: punch_event_2028_02_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_02_device_id_punched_at_idx;

--
-- Name: punch_event_2028_02_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_02_employee_id_local_date_idx;

--
-- Name: punch_event_2028_02_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_02_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_02_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_02_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_02_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_02_ingested_at_idx;

--
-- Name: punch_event_2028_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_02_pkey;

--
-- Name: punch_event_2028_02_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_02_punched_at_idx;

--
-- Name: punch_event_2028_03_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_03_device_id_punched_at_idx;

--
-- Name: punch_event_2028_03_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_03_employee_id_local_date_idx;

--
-- Name: punch_event_2028_03_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_03_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_03_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_03_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_03_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_03_ingested_at_idx;

--
-- Name: punch_event_2028_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_03_pkey;

--
-- Name: punch_event_2028_03_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_03_punched_at_idx;

--
-- Name: punch_event_2028_04_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_04_device_id_punched_at_idx;

--
-- Name: punch_event_2028_04_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_04_employee_id_local_date_idx;

--
-- Name: punch_event_2028_04_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_04_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_04_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_04_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_04_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_04_ingested_at_idx;

--
-- Name: punch_event_2028_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_04_pkey;

--
-- Name: punch_event_2028_04_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_04_punched_at_idx;

--
-- Name: punch_event_2028_05_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_05_device_id_punched_at_idx;

--
-- Name: punch_event_2028_05_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_05_employee_id_local_date_idx;

--
-- Name: punch_event_2028_05_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_05_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_05_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_05_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_05_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_05_ingested_at_idx;

--
-- Name: punch_event_2028_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_05_pkey;

--
-- Name: punch_event_2028_05_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_05_punched_at_idx;

--
-- Name: punch_event_2028_06_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_06_device_id_punched_at_idx;

--
-- Name: punch_event_2028_06_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_06_employee_id_local_date_idx;

--
-- Name: punch_event_2028_06_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_06_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_06_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_06_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_06_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_06_ingested_at_idx;

--
-- Name: punch_event_2028_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_06_pkey;

--
-- Name: punch_event_2028_06_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_06_punched_at_idx;

--
-- Name: punch_event_2028_07_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_07_device_id_punched_at_idx;

--
-- Name: punch_event_2028_07_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_07_employee_id_local_date_idx;

--
-- Name: punch_event_2028_07_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_07_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_07_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_07_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_07_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_07_ingested_at_idx;

--
-- Name: punch_event_2028_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_07_pkey;

--
-- Name: punch_event_2028_07_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_07_punched_at_idx;

--
-- Name: punch_event_2028_08_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_08_device_id_punched_at_idx;

--
-- Name: punch_event_2028_08_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_08_employee_id_local_date_idx;

--
-- Name: punch_event_2028_08_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_08_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_08_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_08_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_08_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_08_ingested_at_idx;

--
-- Name: punch_event_2028_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_08_pkey;

--
-- Name: punch_event_2028_08_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_08_punched_at_idx;

--
-- Name: punch_event_2028_09_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_09_device_id_punched_at_idx;

--
-- Name: punch_event_2028_09_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_09_employee_id_local_date_idx;

--
-- Name: punch_event_2028_09_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_09_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_09_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_09_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_09_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_09_ingested_at_idx;

--
-- Name: punch_event_2028_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_09_pkey;

--
-- Name: punch_event_2028_09_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_09_punched_at_idx;

--
-- Name: punch_event_2028_10_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_10_device_id_punched_at_idx;

--
-- Name: punch_event_2028_10_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_10_employee_id_local_date_idx;

--
-- Name: punch_event_2028_10_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_10_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_10_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_10_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_10_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_10_ingested_at_idx;

--
-- Name: punch_event_2028_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_10_pkey;

--
-- Name: punch_event_2028_10_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_10_punched_at_idx;

--
-- Name: punch_event_2028_11_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_11_device_id_punched_at_idx;

--
-- Name: punch_event_2028_11_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_11_employee_id_local_date_idx;

--
-- Name: punch_event_2028_11_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_11_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_11_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_11_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_11_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_11_ingested_at_idx;

--
-- Name: punch_event_2028_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_11_pkey;

--
-- Name: punch_event_2028_11_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_11_punched_at_idx;

--
-- Name: punch_event_2028_12_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2028_12_device_id_punched_at_idx;

--
-- Name: punch_event_2028_12_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2028_12_employee_id_local_date_idx;

--
-- Name: punch_event_2028_12_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2028_12_employee_id_punched_at_idx;

--
-- Name: punch_event_2028_12_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2028_12_idempotency_key_punched_at_key;

--
-- Name: punch_event_2028_12_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2028_12_ingested_at_idx;

--
-- Name: punch_event_2028_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2028_12_pkey;

--
-- Name: punch_event_2028_12_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2028_12_punched_at_idx;

--
-- Name: punch_event_2029_01_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_01_device_id_punched_at_idx;

--
-- Name: punch_event_2029_01_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_01_employee_id_local_date_idx;

--
-- Name: punch_event_2029_01_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_01_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_01_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_01_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_01_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_01_ingested_at_idx;

--
-- Name: punch_event_2029_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_01_pkey;

--
-- Name: punch_event_2029_01_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_01_punched_at_idx;

--
-- Name: punch_event_2029_02_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_02_device_id_punched_at_idx;

--
-- Name: punch_event_2029_02_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_02_employee_id_local_date_idx;

--
-- Name: punch_event_2029_02_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_02_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_02_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_02_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_02_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_02_ingested_at_idx;

--
-- Name: punch_event_2029_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_02_pkey;

--
-- Name: punch_event_2029_02_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_02_punched_at_idx;

--
-- Name: punch_event_2029_03_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_03_device_id_punched_at_idx;

--
-- Name: punch_event_2029_03_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_03_employee_id_local_date_idx;

--
-- Name: punch_event_2029_03_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_03_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_03_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_03_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_03_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_03_ingested_at_idx;

--
-- Name: punch_event_2029_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_03_pkey;

--
-- Name: punch_event_2029_03_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_03_punched_at_idx;

--
-- Name: punch_event_2029_04_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_04_device_id_punched_at_idx;

--
-- Name: punch_event_2029_04_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_04_employee_id_local_date_idx;

--
-- Name: punch_event_2029_04_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_04_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_04_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_04_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_04_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_04_ingested_at_idx;

--
-- Name: punch_event_2029_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_04_pkey;

--
-- Name: punch_event_2029_04_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_04_punched_at_idx;

--
-- Name: punch_event_2029_05_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_05_device_id_punched_at_idx;

--
-- Name: punch_event_2029_05_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_05_employee_id_local_date_idx;

--
-- Name: punch_event_2029_05_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_05_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_05_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_05_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_05_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_05_ingested_at_idx;

--
-- Name: punch_event_2029_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_05_pkey;

--
-- Name: punch_event_2029_05_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_05_punched_at_idx;

--
-- Name: punch_event_2029_06_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_06_device_id_punched_at_idx;

--
-- Name: punch_event_2029_06_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_06_employee_id_local_date_idx;

--
-- Name: punch_event_2029_06_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_06_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_06_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_06_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_06_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_06_ingested_at_idx;

--
-- Name: punch_event_2029_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_06_pkey;

--
-- Name: punch_event_2029_06_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_06_punched_at_idx;

--
-- Name: punch_event_2029_07_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_07_device_id_punched_at_idx;

--
-- Name: punch_event_2029_07_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_07_employee_id_local_date_idx;

--
-- Name: punch_event_2029_07_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_07_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_07_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_07_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_07_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_07_ingested_at_idx;

--
-- Name: punch_event_2029_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_07_pkey;

--
-- Name: punch_event_2029_07_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_07_punched_at_idx;

--
-- Name: punch_event_2029_08_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_08_device_id_punched_at_idx;

--
-- Name: punch_event_2029_08_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_08_employee_id_local_date_idx;

--
-- Name: punch_event_2029_08_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_08_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_08_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_08_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_08_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_08_ingested_at_idx;

--
-- Name: punch_event_2029_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_08_pkey;

--
-- Name: punch_event_2029_08_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_08_punched_at_idx;

--
-- Name: punch_event_2029_09_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_09_device_id_punched_at_idx;

--
-- Name: punch_event_2029_09_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_09_employee_id_local_date_idx;

--
-- Name: punch_event_2029_09_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_09_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_09_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_09_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_09_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_09_ingested_at_idx;

--
-- Name: punch_event_2029_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_09_pkey;

--
-- Name: punch_event_2029_09_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_09_punched_at_idx;

--
-- Name: punch_event_2029_10_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_10_device_id_punched_at_idx;

--
-- Name: punch_event_2029_10_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_10_employee_id_local_date_idx;

--
-- Name: punch_event_2029_10_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_10_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_10_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_10_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_10_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_10_ingested_at_idx;

--
-- Name: punch_event_2029_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_10_pkey;

--
-- Name: punch_event_2029_10_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_10_punched_at_idx;

--
-- Name: punch_event_2029_11_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_11_device_id_punched_at_idx;

--
-- Name: punch_event_2029_11_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_11_employee_id_local_date_idx;

--
-- Name: punch_event_2029_11_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_11_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_11_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_11_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_11_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_11_ingested_at_idx;

--
-- Name: punch_event_2029_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_11_pkey;

--
-- Name: punch_event_2029_11_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_11_punched_at_idx;

--
-- Name: punch_event_2029_12_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2029_12_device_id_punched_at_idx;

--
-- Name: punch_event_2029_12_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2029_12_employee_id_local_date_idx;

--
-- Name: punch_event_2029_12_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2029_12_employee_id_punched_at_idx;

--
-- Name: punch_event_2029_12_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2029_12_idempotency_key_punched_at_key;

--
-- Name: punch_event_2029_12_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2029_12_ingested_at_idx;

--
-- Name: punch_event_2029_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2029_12_pkey;

--
-- Name: punch_event_2029_12_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2029_12_punched_at_idx;

--
-- Name: punch_event_2030_01_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_01_device_id_punched_at_idx;

--
-- Name: punch_event_2030_01_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_01_employee_id_local_date_idx;

--
-- Name: punch_event_2030_01_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_01_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_01_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_01_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_01_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_01_ingested_at_idx;

--
-- Name: punch_event_2030_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_01_pkey;

--
-- Name: punch_event_2030_01_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_01_punched_at_idx;

--
-- Name: punch_event_2030_02_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_02_device_id_punched_at_idx;

--
-- Name: punch_event_2030_02_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_02_employee_id_local_date_idx;

--
-- Name: punch_event_2030_02_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_02_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_02_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_02_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_02_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_02_ingested_at_idx;

--
-- Name: punch_event_2030_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_02_pkey;

--
-- Name: punch_event_2030_02_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_02_punched_at_idx;

--
-- Name: punch_event_2030_03_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_03_device_id_punched_at_idx;

--
-- Name: punch_event_2030_03_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_03_employee_id_local_date_idx;

--
-- Name: punch_event_2030_03_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_03_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_03_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_03_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_03_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_03_ingested_at_idx;

--
-- Name: punch_event_2030_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_03_pkey;

--
-- Name: punch_event_2030_03_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_03_punched_at_idx;

--
-- Name: punch_event_2030_04_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_04_device_id_punched_at_idx;

--
-- Name: punch_event_2030_04_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_04_employee_id_local_date_idx;

--
-- Name: punch_event_2030_04_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_04_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_04_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_04_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_04_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_04_ingested_at_idx;

--
-- Name: punch_event_2030_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_04_pkey;

--
-- Name: punch_event_2030_04_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_04_punched_at_idx;

--
-- Name: punch_event_2030_05_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_05_device_id_punched_at_idx;

--
-- Name: punch_event_2030_05_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_05_employee_id_local_date_idx;

--
-- Name: punch_event_2030_05_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_05_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_05_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_05_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_05_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_05_ingested_at_idx;

--
-- Name: punch_event_2030_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_05_pkey;

--
-- Name: punch_event_2030_05_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_05_punched_at_idx;

--
-- Name: punch_event_2030_06_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_06_device_id_punched_at_idx;

--
-- Name: punch_event_2030_06_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_06_employee_id_local_date_idx;

--
-- Name: punch_event_2030_06_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_06_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_06_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_06_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_06_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_06_ingested_at_idx;

--
-- Name: punch_event_2030_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_06_pkey;

--
-- Name: punch_event_2030_06_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_06_punched_at_idx;

--
-- Name: punch_event_2030_07_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_07_device_id_punched_at_idx;

--
-- Name: punch_event_2030_07_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_07_employee_id_local_date_idx;

--
-- Name: punch_event_2030_07_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_07_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_07_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_07_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_07_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_07_ingested_at_idx;

--
-- Name: punch_event_2030_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_07_pkey;

--
-- Name: punch_event_2030_07_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_07_punched_at_idx;

--
-- Name: punch_event_2030_08_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_08_device_id_punched_at_idx;

--
-- Name: punch_event_2030_08_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_08_employee_id_local_date_idx;

--
-- Name: punch_event_2030_08_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_08_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_08_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_08_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_08_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_08_ingested_at_idx;

--
-- Name: punch_event_2030_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_08_pkey;

--
-- Name: punch_event_2030_08_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_08_punched_at_idx;

--
-- Name: punch_event_2030_09_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_09_device_id_punched_at_idx;

--
-- Name: punch_event_2030_09_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_09_employee_id_local_date_idx;

--
-- Name: punch_event_2030_09_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_09_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_09_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_09_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_09_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_09_ingested_at_idx;

--
-- Name: punch_event_2030_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_09_pkey;

--
-- Name: punch_event_2030_09_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_09_punched_at_idx;

--
-- Name: punch_event_2030_10_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_10_device_id_punched_at_idx;

--
-- Name: punch_event_2030_10_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_10_employee_id_local_date_idx;

--
-- Name: punch_event_2030_10_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_10_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_10_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_10_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_10_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_10_ingested_at_idx;

--
-- Name: punch_event_2030_10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_10_pkey;

--
-- Name: punch_event_2030_10_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_10_punched_at_idx;

--
-- Name: punch_event_2030_11_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_11_device_id_punched_at_idx;

--
-- Name: punch_event_2030_11_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_11_employee_id_local_date_idx;

--
-- Name: punch_event_2030_11_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_11_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_11_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_11_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_11_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_11_ingested_at_idx;

--
-- Name: punch_event_2030_11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_11_pkey;

--
-- Name: punch_event_2030_11_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_11_punched_at_idx;

--
-- Name: punch_event_2030_12_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_2030_12_device_id_punched_at_idx;

--
-- Name: punch_event_2030_12_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_2030_12_employee_id_local_date_idx;

--
-- Name: punch_event_2030_12_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_2030_12_employee_id_punched_at_idx;

--
-- Name: punch_event_2030_12_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_2030_12_idempotency_key_punched_at_key;

--
-- Name: punch_event_2030_12_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_2030_12_ingested_at_idx;

--
-- Name: punch_event_2030_12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_2030_12_pkey;

--
-- Name: punch_event_2030_12_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_2030_12_punched_at_idx;

--
-- Name: punch_event_default_device_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_device_idx ATTACH PARTITION public.punch_event_default_device_id_punched_at_idx;

--
-- Name: punch_event_default_employee_id_local_date_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_date_idx ATTACH PARTITION public.punch_event_default_employee_id_local_date_idx;

--
-- Name: punch_event_default_employee_id_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_employee_idx ATTACH PARTITION public.punch_event_default_employee_id_punched_at_idx;

--
-- Name: punch_event_default_idempotency_key_punched_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_idempotency_key_punched_at_key ATTACH PARTITION public.punch_event_default_idempotency_key_punched_at_key;

--
-- Name: punch_event_default_ingested_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_unmapped_idx ATTACH PARTITION public.punch_event_default_ingested_at_idx;

--
-- Name: punch_event_default_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_pkey ATTACH PARTITION public.punch_event_default_pkey;

--
-- Name: punch_event_default_punched_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.punch_event_brin ATTACH PARTITION public.punch_event_default_punched_at_idx;

--
-- Name: audit_log audit_log_chain_trg; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_log_chain_trg BEFORE INSERT ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_chain();

--
-- Name: audit_log audit_log_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_log_immutable BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.forbid_mutation();

--
-- Name: issued_letter issued_letter_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER issued_letter_immutable BEFORE UPDATE ON public.issued_letter FOR EACH ROW EXECUTE FUNCTION public.issued_letter_immutable();

--
-- Name: issued_letter issued_letter_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER issued_letter_no_delete BEFORE DELETE ON public.issued_letter FOR EACH ROW EXECUTE FUNCTION public.issued_letter_no_delete();

--
-- Name: payroll_run payroll_run_locked_trg; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_run_locked_trg BEFORE DELETE OR UPDATE ON public.payroll_run FOR EACH ROW EXECUTE FUNCTION public.payroll_run_locked_guard();

--
-- Name: payslip_line payslip_line_locked_trg; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payslip_line_locked_trg BEFORE DELETE OR UPDATE ON public.payslip_line FOR EACH ROW EXECUTE FUNCTION public.payslip_locked_guard();

--
-- Name: payslip payslip_locked_trg; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payslip_locked_trg BEFORE DELETE OR UPDATE ON public.payslip FOR EACH ROW EXECUTE FUNCTION public.payslip_locked_guard();

--
-- Name: punch_event punch_event_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER punch_event_immutable BEFORE DELETE OR UPDATE ON public.punch_event FOR EACH ROW EXECUTE FUNCTION public.forbid_mutation();

--
-- Name: announcement_ack announcement_ack_announcement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_ack
    ADD CONSTRAINT announcement_ack_announcement_id_fkey FOREIGN KEY (announcement_id) REFERENCES public.announcement(id);

--
-- Name: announcement_ack announcement_ack_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_ack
    ADD CONSTRAINT announcement_ack_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: announcement announcement_author_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement
    ADD CONSTRAINT announcement_author_employee_id_fkey FOREIGN KEY (author_employee_id) REFERENCES public.employee(id);

--
-- Name: announcement announcement_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement
    ADD CONSTRAINT announcement_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.app_user(id);

--
-- Name: announcement announcement_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement
    ADD CONSTRAINT announcement_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: announcement_popup_seen announcement_popup_seen_announcement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_popup_seen
    ADD CONSTRAINT announcement_popup_seen_announcement_id_fkey FOREIGN KEY (announcement_id) REFERENCES public.announcement(id);

--
-- Name: announcement_popup_seen announcement_popup_seen_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcement_popup_seen
    ADD CONSTRAINT announcement_popup_seen_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: app_user app_user_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: app_user app_user_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: approval_action approval_action_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_action
    ADD CONSTRAINT approval_action_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.app_user(id);

--
-- Name: approval_action approval_action_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_action
    ADD CONSTRAINT approval_action_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.workflow_instance(id);

--
-- Name: approval_action approval_action_on_behalf_of_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_action
    ADD CONSTRAINT approval_action_on_behalf_of_fkey FOREIGN KEY (on_behalf_of) REFERENCES public.app_user(id);

--
-- Name: approval_flow approval_flow_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_flow
    ADD CONSTRAINT approval_flow_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: approval_flow approval_flow_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_flow
    ADD CONSTRAINT approval_flow_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: approver_delegate approver_delegate_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approver_delegate
    ADD CONSTRAINT approver_delegate_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: approver_delegate approver_delegate_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approver_delegate
    ADD CONSTRAINT approver_delegate_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: approver_delegate approver_delegate_delegate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approver_delegate
    ADD CONSTRAINT approver_delegate_delegate_id_fkey FOREIGN KEY (delegate_id) REFERENCES public.app_user(id);

--
-- Name: approver_delegate approver_delegate_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approver_delegate
    ADD CONSTRAINT approver_delegate_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: asset_assignment asset_assignment_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.asset(id);

--
-- Name: asset_assignment asset_assignment_assigned_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_assigned_by_fkey FOREIGN KEY (assigned_by) REFERENCES public.app_user(id);

--
-- Name: asset_assignment asset_assignment_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: asset_assignment asset_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: asset_assignment asset_assignment_returned_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_assignment
    ADD CONSTRAINT asset_assignment_returned_by_fkey FOREIGN KEY (returned_by) REFERENCES public.app_user(id);

--
-- Name: asset asset_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT asset_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: asset_type asset_type_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_type
    ADD CONSTRAINT asset_type_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: asset asset_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT asset_type_id_fkey FOREIGN KEY (type_id) REFERENCES public.asset_type(id);

--
-- Name: attendance_day attendance_day_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_day
    ADD CONSTRAINT attendance_day_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: attendance_day attendance_day_ot_slip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_day
    ADD CONSTRAINT attendance_day_ot_slip_id_fkey FOREIGN KEY (ot_slip_id) REFERENCES public.ot_slip(id);

--
-- Name: attendance_day attendance_day_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_day
    ADD CONSTRAINT attendance_day_shift_id_fkey FOREIGN KEY (shift_id) REFERENCES public.shift(id);

--
-- Name: attendance_nudge attendance_nudge_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_nudge
    ADD CONSTRAINT attendance_nudge_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: attendance_nudge attendance_nudge_nudged_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_nudge
    ADD CONSTRAINT attendance_nudge_nudged_by_fkey FOREIGN KEY (nudged_by) REFERENCES public.app_user(id);

--
-- Name: attendance_nudge attendance_nudge_target_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_nudge
    ADD CONSTRAINT attendance_nudge_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES public.app_user(id);

--
-- Name: attendance_upload attendance_upload_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_upload
    ADD CONSTRAINT attendance_upload_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: attendance_upload attendance_upload_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_upload
    ADD CONSTRAINT attendance_upload_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.app_user(id);

--
-- Name: bank_file bank_file_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_file
    ADD CONSTRAINT bank_file_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: bank_file bank_file_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_file
    ADD CONSTRAINT bank_file_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.payroll_run(id);

--
-- Name: biometric_template biometric_template_consent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.biometric_template
    ADD CONSTRAINT biometric_template_consent_id_fkey FOREIGN KEY (consent_id) REFERENCES public.consent_record(id);

--
-- Name: biometric_template biometric_template_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.biometric_template
    ADD CONSTRAINT biometric_template_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: biometric_template biometric_template_enrolled_on_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.biometric_template
    ADD CONSTRAINT biometric_template_enrolled_on_device_id_fkey FOREIGN KEY (enrolled_on_device_id) REFERENCES public.device(id);

--
-- Name: branch branch_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch
    ADD CONSTRAINT branch_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: branch branch_head_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch
    ADD CONSTRAINT branch_head_employee_id_fkey FOREIGN KEY (head_employee_id) REFERENCES public.employee(id);

--
-- Name: career_event career_event_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.career_event
    ADD CONSTRAINT career_event_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: career_event career_event_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.career_event
    ADD CONSTRAINT career_event_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: career_event career_event_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.career_event
    ADD CONSTRAINT career_event_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: company_policy company_policy_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_policy
    ADD CONSTRAINT company_policy_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: company_policy company_policy_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_policy
    ADD CONSTRAINT company_policy_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: company_policy company_policy_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_policy
    ADD CONSTRAINT company_policy_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.stored_file(id);

--
-- Name: company_setting company_setting_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_setting
    ADD CONSTRAINT company_setting_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: company_setting company_setting_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_setting
    ADD CONSTRAINT company_setting_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.app_user(id);

--
-- Name: consent_record consent_record_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_record
    ADD CONSTRAINT consent_record_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: consent_record consent_record_recorded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_record
    ADD CONSTRAINT consent_record_recorded_by_fkey FOREIGN KEY (recorded_by) REFERENCES public.app_user(id);

--
-- Name: designation designation_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designation
    ADD CONSTRAINT designation_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: designation designation_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designation
    ADD CONSTRAINT designation_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: device device_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device
    ADD CONSTRAINT device_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: device_command device_command_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_command
    ADD CONSTRAINT device_command_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.device(id);

--
-- Name: device device_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device
    ADD CONSTRAINT device_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: device_user_map device_user_map_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_user_map
    ADD CONSTRAINT device_user_map_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.device(id);

--
-- Name: device_user_map device_user_map_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_user_map
    ADD CONSTRAINT device_user_map_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: disciplinary_case disciplinary_case_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: disciplinary_case disciplinary_case_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: disciplinary_case disciplinary_case_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: disciplinary_case disciplinary_case_letter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_letter_id_fkey FOREIGN KEY (letter_id) REFERENCES public.issued_letter(id);

--
-- Name: disciplinary_case disciplinary_case_opened_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_opened_by_fkey FOREIGN KEY (opened_by) REFERENCES public.app_user(id);

--
-- Name: disciplinary_case disciplinary_case_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_case
    ADD CONSTRAINT disciplinary_case_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.disciplinary_stage(id);

--
-- Name: disciplinary_stage disciplinary_stage_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disciplinary_stage
    ADD CONSTRAINT disciplinary_stage_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: document_category document_category_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_category
    ADD CONSTRAINT document_category_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: document_expiry_alert document_expiry_alert_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_expiry_alert
    ADD CONSTRAINT document_expiry_alert_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.employee_document(id);

--
-- Name: employee_assignment employee_assignment_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignment
    ADD CONSTRAINT employee_assignment_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: employee_assignment employee_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignment
    ADD CONSTRAINT employee_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_assignment employee_assignment_manager_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignment
    ADD CONSTRAINT employee_assignment_manager_id_fkey FOREIGN KEY (manager_id) REFERENCES public.employee(id);

--
-- Name: employee_assignment employee_assignment_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignment
    ADD CONSTRAINT employee_assignment_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: employee employee_avatar_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_avatar_file_id_fkey FOREIGN KEY (avatar_file_id) REFERENCES public.stored_file(id);

--
-- Name: employee employee_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_confirmation employee_confirmation_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_confirmation
    ADD CONSTRAINT employee_confirmation_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_confirmation employee_confirmation_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_confirmation
    ADD CONSTRAINT employee_confirmation_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: employee_confirmation employee_confirmation_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_confirmation
    ADD CONSTRAINT employee_confirmation_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_confirmation employee_confirmation_review_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_confirmation
    ADD CONSTRAINT employee_confirmation_review_id_fkey FOREIGN KEY (review_id) REFERENCES public.probation_review(id);

--
-- Name: employee employee_designation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_designation_id_fkey FOREIGN KEY (designation_id) REFERENCES public.designation(id);

--
-- Name: employee_document employee_document_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.document_category(id);

--
-- Name: employee_document employee_document_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_document employee_document_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.app_user(id);

--
-- Name: employee_document employee_document_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_document employee_document_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.stored_file(id);

--
-- Name: employee_document employee_document_replaces_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_replaces_id_fkey FOREIGN KEY (replaces_id) REFERENCES public.employee_document(id);

--
-- Name: employee_document employee_document_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.app_user(id);

--
-- Name: employee_document employee_document_verified_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_document
    ADD CONSTRAINT employee_document_verified_by_fkey FOREIGN KEY (verified_by) REFERENCES public.app_user(id);

--
-- Name: employee_education employee_education_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_education
    ADD CONSTRAINT employee_education_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_emergency_contact employee_emergency_contact_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_emergency_contact
    ADD CONSTRAINT employee_emergency_contact_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_exit employee_exit_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exit
    ADD CONSTRAINT employee_exit_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_exit employee_exit_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exit
    ADD CONSTRAINT employee_exit_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_exit employee_exit_initiated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exit
    ADD CONSTRAINT employee_exit_initiated_by_fkey FOREIGN KEY (initiated_by) REFERENCES public.app_user(id);

--
-- Name: employee_experience employee_experience_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_experience
    ADD CONSTRAINT employee_experience_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_family employee_family_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_family
    ADD CONSTRAINT employee_family_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_field_history employee_field_history_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_field_history
    ADD CONSTRAINT employee_field_history_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.app_user(id);

--
-- Name: employee_field_history employee_field_history_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_field_history
    ADD CONSTRAINT employee_field_history_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_field_history employee_field_history_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_field_history
    ADD CONSTRAINT employee_field_history_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_designation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_designation_id_fkey FOREIGN KEY (designation_id) REFERENCES public.designation(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: employee_temp_assignment employee_temp_assignment_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_temp_assignment
    ADD CONSTRAINT employee_temp_assignment_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: engage_comment engage_comment_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_comment
    ADD CONSTRAINT engage_comment_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: engage_comment engage_comment_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_comment
    ADD CONSTRAINT engage_comment_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: engage_pref engage_pref_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_pref
    ADD CONSTRAINT engage_pref_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: engage_reaction engage_reaction_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_reaction
    ADD CONSTRAINT engage_reaction_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: engage_reaction engage_reaction_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_reaction
    ADD CONSTRAINT engage_reaction_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: engage_setting engage_setting_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_setting
    ADD CONSTRAINT engage_setting_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: engage_value engage_value_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.engage_value
    ADD CONSTRAINT engage_value_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: exit_interview exit_interview_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview
    ADD CONSTRAINT exit_interview_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: exit_interview exit_interview_exit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview
    ADD CONSTRAINT exit_interview_exit_id_fkey FOREIGN KEY (exit_id) REFERENCES public.employee_exit(id);

--
-- Name: exit_interview_question exit_interview_question_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview_question
    ADD CONSTRAINT exit_interview_question_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: exit_interview exit_interview_recorded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exit_interview
    ADD CONSTRAINT exit_interview_recorded_by_fkey FOREIGN KEY (recorded_by) REFERENCES public.app_user(id);

--
-- Name: expense_category expense_category_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_category
    ADD CONSTRAINT expense_category_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: expense_claim expense_claim_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_claim
    ADD CONSTRAINT expense_claim_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: expense_claim expense_claim_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_claim
    ADD CONSTRAINT expense_claim_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: expense_claim expense_claim_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_claim
    ADD CONSTRAINT expense_claim_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: expense_claim expense_claim_workflow_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_claim
    ADD CONSTRAINT expense_claim_workflow_instance_id_fkey FOREIGN KEY (workflow_instance_id) REFERENCES public.workflow_instance(id);

--
-- Name: expense_item expense_item_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_item
    ADD CONSTRAINT expense_item_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.expense_category(id);

--
-- Name: expense_item expense_item_claim_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_item
    ADD CONSTRAINT expense_item_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES public.expense_claim(id) ON DELETE CASCADE;

--
-- Name: expense_item expense_item_receipt_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_item
    ADD CONSTRAINT expense_item_receipt_file_id_fkey FOREIGN KEY (receipt_file_id) REFERENCES public.stored_file(id);

--
-- Name: expense_limit expense_limit_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_limit
    ADD CONSTRAINT expense_limit_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.expense_category(id) ON DELETE CASCADE;

--
-- Name: expense_limit expense_limit_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_limit
    ADD CONSTRAINT expense_limit_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: expense_payout expense_payout_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_payout
    ADD CONSTRAINT expense_payout_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: expense_payout expense_payout_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_payout
    ADD CONSTRAINT expense_payout_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: expense_payout expense_payout_parent_payout_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_payout
    ADD CONSTRAINT expense_payout_parent_payout_id_fkey FOREIGN KEY (parent_payout_id) REFERENCES public.expense_payout(id);

--
-- Name: expense_payout expense_payout_payroll_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expense_payout
    ADD CONSTRAINT expense_payout_payroll_run_id_fkey FOREIGN KEY (payroll_run_id) REFERENCES public.payroll_run(id) ON DELETE SET NULL;

--
-- Name: field_visit field_visit_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_visit
    ADD CONSTRAINT field_visit_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: field_visit field_visit_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_visit
    ADD CONSTRAINT field_visit_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: field_visit field_visit_photo_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_visit
    ADD CONSTRAINT field_visit_photo_file_id_fkey FOREIGN KEY (photo_file_id) REFERENCES public.stored_file(id);

--
-- Name: grievance grievance_appeal_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_appeal_decided_by_fkey FOREIGN KEY (appeal_decided_by) REFERENCES public.app_user(id);

--
-- Name: grievance_committee_member grievance_committee_member_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_committee_member
    ADD CONSTRAINT grievance_committee_member_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.app_user(id);

--
-- Name: grievance_committee_member grievance_committee_member_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_committee_member
    ADD CONSTRAINT grievance_committee_member_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: grievance_committee_member grievance_committee_member_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_committee_member
    ADD CONSTRAINT grievance_committee_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: grievance grievance_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: grievance grievance_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: grievance grievance_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: grievance_file grievance_file_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_file
    ADD CONSTRAINT grievance_file_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.stored_file(id);

--
-- Name: grievance_file grievance_file_grievance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_file
    ADD CONSTRAINT grievance_file_grievance_id_fkey FOREIGN KEY (grievance_id) REFERENCES public.grievance(id);

--
-- Name: grievance_note grievance_note_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_note
    ADD CONSTRAINT grievance_note_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.app_user(id);

--
-- Name: grievance_note grievance_note_grievance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance_note
    ADD CONSTRAINT grievance_note_grievance_id_fkey FOREIGN KEY (grievance_id) REFERENCES public.grievance(id);

--
-- Name: grievance grievance_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grievance
    ADD CONSTRAINT grievance_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: helpdesk_category helpdesk_category_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_category
    ADD CONSTRAINT helpdesk_category_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: helpdesk_category helpdesk_category_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_category
    ADD CONSTRAINT helpdesk_category_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.app_user(id);

--
-- Name: helpdesk_message helpdesk_message_author_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_message
    ADD CONSTRAINT helpdesk_message_author_user_id_fkey FOREIGN KEY (author_user_id) REFERENCES public.app_user(id);

--
-- Name: helpdesk_message helpdesk_message_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_message
    ADD CONSTRAINT helpdesk_message_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: helpdesk_message helpdesk_message_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_message
    ADD CONSTRAINT helpdesk_message_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.helpdesk_ticket(id) ON DELETE CASCADE;

--
-- Name: helpdesk_ticket helpdesk_ticket_assignee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_assignee_user_id_fkey FOREIGN KEY (assignee_user_id) REFERENCES public.app_user(id);

--
-- Name: helpdesk_ticket helpdesk_ticket_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.helpdesk_category(id);

--
-- Name: helpdesk_ticket helpdesk_ticket_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: helpdesk_ticket helpdesk_ticket_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: helpdesk_ticket helpdesk_ticket_requester_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.helpdesk_ticket
    ADD CONSTRAINT helpdesk_ticket_requester_user_id_fkey FOREIGN KEY (requester_user_id) REFERENCES public.app_user(id);

--
-- Name: hiring_application hiring_application_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.hiring_candidate(id);

--
-- Name: hiring_application hiring_application_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_application hiring_application_cv_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_cv_file_id_fkey FOREIGN KEY (cv_file_id) REFERENCES public.stored_file(id);

--
-- Name: hiring_application hiring_application_hired_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_hired_employee_id_fkey FOREIGN KEY (hired_employee_id) REFERENCES public.employee(id);

--
-- Name: hiring_application hiring_application_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.hiring_job(id);

--
-- Name: hiring_application hiring_application_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_application
    ADD CONSTRAINT hiring_application_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.app_user(id);

--
-- Name: hiring_candidate hiring_candidate_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_candidate
    ADD CONSTRAINT hiring_candidate_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_event hiring_event_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_event
    ADD CONSTRAINT hiring_event_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.app_user(id);

--
-- Name: hiring_event hiring_event_application_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_event
    ADD CONSTRAINT hiring_event_application_id_fkey FOREIGN KEY (application_id) REFERENCES public.hiring_application(id);

--
-- Name: hiring_event hiring_event_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_event
    ADD CONSTRAINT hiring_event_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_interview hiring_interview_application_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_interview
    ADD CONSTRAINT hiring_interview_application_id_fkey FOREIGN KEY (application_id) REFERENCES public.hiring_application(id);

--
-- Name: hiring_interview hiring_interview_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_interview
    ADD CONSTRAINT hiring_interview_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_interview hiring_interview_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_interview
    ADD CONSTRAINT hiring_interview_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: hiring_job hiring_job_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: hiring_job hiring_job_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_job hiring_job_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: hiring_job hiring_job_hiring_manager_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_hiring_manager_id_fkey FOREIGN KEY (hiring_manager_id) REFERENCES public.employee(id);

--
-- Name: hiring_job hiring_job_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: hiring_job hiring_job_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_job
    ADD CONSTRAINT hiring_job_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.hiring_requisition(id);

--
-- Name: hiring_offer hiring_offer_application_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_offer
    ADD CONSTRAINT hiring_offer_application_id_fkey FOREIGN KEY (application_id) REFERENCES public.hiring_application(id);

--
-- Name: hiring_offer hiring_offer_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_offer
    ADD CONSTRAINT hiring_offer_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_offer hiring_offer_created_by_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_offer
    ADD CONSTRAINT hiring_offer_created_by_employee_id_fkey FOREIGN KEY (created_by_employee_id) REFERENCES public.employee(id);

--
-- Name: hiring_offer hiring_offer_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_offer
    ADD CONSTRAINT hiring_offer_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: hiring_requisition hiring_requisition_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: hiring_requisition hiring_requisition_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: hiring_requisition hiring_requisition_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: hiring_requisition hiring_requisition_raised_by_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_raised_by_employee_id_fkey FOREIGN KEY (raised_by_employee_id) REFERENCES public.employee(id);

--
-- Name: hiring_requisition hiring_requisition_raised_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_raised_by_fkey FOREIGN KEY (raised_by) REFERENCES public.app_user(id);

--
-- Name: hiring_requisition hiring_requisition_replacing_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_requisition
    ADD CONSTRAINT hiring_requisition_replacing_employee_id_fkey FOREIGN KEY (replacing_employee_id) REFERENCES public.employee(id);

--
-- Name: hiring_scorecard hiring_scorecard_interview_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_scorecard
    ADD CONSTRAINT hiring_scorecard_interview_id_fkey FOREIGN KEY (interview_id) REFERENCES public.hiring_interview(id);

--
-- Name: hiring_scorecard hiring_scorecard_reviewer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hiring_scorecard
    ADD CONSTRAINT hiring_scorecard_reviewer_id_fkey FOREIGN KEY (reviewer_id) REFERENCES public.app_user(id);

--
-- Name: holiday holiday_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday
    ADD CONSTRAINT holiday_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: holiday holiday_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday
    ADD CONSTRAINT holiday_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: issued_letter issued_letter_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: issued_letter issued_letter_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: issued_letter issued_letter_issued_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_issued_by_fkey FOREIGN KEY (issued_by) REFERENCES public.app_user(id);

--
-- Name: issued_letter issued_letter_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.letter_request(id);

--
-- Name: issued_letter issued_letter_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.letter_template(id);

--
-- Name: issued_letter issued_letter_voided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issued_letter
    ADD CONSTRAINT issued_letter_voided_by_fkey FOREIGN KEY (voided_by) REFERENCES public.app_user(id);

--
-- Name: kiosk kiosk_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: kiosk kiosk_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: kiosk kiosk_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: kiosk_credential kiosk_credential_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_credential
    ADD CONSTRAINT kiosk_credential_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: kiosk_credential kiosk_credential_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_credential
    ADD CONSTRAINT kiosk_credential_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: kiosk kiosk_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk
    ADD CONSTRAINT kiosk_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.location(id) ON DELETE SET NULL;

--
-- Name: kiosk_qr_use kiosk_qr_use_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_qr_use
    ADD CONSTRAINT kiosk_qr_use_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: kiosk_qr_use kiosk_qr_use_kiosk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kiosk_qr_use
    ADD CONSTRAINT kiosk_qr_use_kiosk_id_fkey FOREIGN KEY (kiosk_id) REFERENCES public.kiosk(id);

--
-- Name: kudos kudos_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos
    ADD CONSTRAINT kudos_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: kudos kudos_giver_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos
    ADD CONSTRAINT kudos_giver_employee_id_fkey FOREIGN KEY (giver_employee_id) REFERENCES public.employee(id);

--
-- Name: kudos kudos_giver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos
    ADD CONSTRAINT kudos_giver_id_fkey FOREIGN KEY (giver_id) REFERENCES public.app_user(id);

--
-- Name: kudos_recipient kudos_recipient_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos_recipient
    ADD CONSTRAINT kudos_recipient_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: kudos_recipient kudos_recipient_kudos_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos_recipient
    ADD CONSTRAINT kudos_recipient_kudos_id_fkey FOREIGN KEY (kudos_id) REFERENCES public.kudos(id);

--
-- Name: kudos kudos_value_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kudos
    ADD CONSTRAINT kudos_value_id_fkey FOREIGN KEY (value_id) REFERENCES public.engage_value(id);

--
-- Name: kye_campaign kye_campaign_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_campaign
    ADD CONSTRAINT kye_campaign_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: kye_campaign kye_campaign_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_campaign
    ADD CONSTRAINT kye_campaign_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: kye_response kye_response_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_response
    ADD CONSTRAINT kye_response_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.kye_campaign(id);

--
-- Name: kye_response kye_response_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kye_response
    ADD CONSTRAINT kye_response_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: late_penalty late_penalty_applied_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_applied_by_fkey FOREIGN KEY (applied_by) REFERENCES public.app_user(id);

--
-- Name: late_penalty late_penalty_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: late_penalty late_penalty_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: late_penalty late_penalty_leave_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_leave_type_id_fkey FOREIGN KEY (leave_type_id) REFERENCES public.leave_type(id);

--
-- Name: late_penalty late_penalty_reversed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_penalty
    ADD CONSTRAINT late_penalty_reversed_by_fkey FOREIGN KEY (reversed_by) REFERENCES public.app_user(id);

--
-- Name: leave_ledger leave_ledger_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: leave_ledger leave_ledger_leave_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_leave_type_id_fkey FOREIGN KEY (leave_type_id) REFERENCES public.leave_type(id);

--
-- Name: leave_ledger leave_ledger_posted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_posted_by_fkey FOREIGN KEY (posted_by) REFERENCES public.app_user(id);

--
-- Name: leave_request leave_request_bridge_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_bridge_type_id_fkey FOREIGN KEY (bridge_type_id) REFERENCES public.leave_type(id);

--
-- Name: leave_request leave_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: leave_request leave_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: leave_request leave_request_leave_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_leave_type_id_fkey FOREIGN KEY (leave_type_id) REFERENCES public.leave_type(id);

--
-- Name: leave_type leave_type_club_with_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_type
    ADD CONSTRAINT leave_type_club_with_id_fkey FOREIGN KEY (club_with_id) REFERENCES public.leave_type(id) ON DELETE SET NULL;

--
-- Name: leave_type leave_type_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_type
    ADD CONSTRAINT leave_type_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: letter_request letter_request_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_request
    ADD CONSTRAINT letter_request_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: letter_request letter_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_request
    ADD CONSTRAINT letter_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: letter_request letter_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_request
    ADD CONSTRAINT letter_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: letter_request letter_request_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_request
    ADD CONSTRAINT letter_request_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.letter_template(id);

--
-- Name: letter_sequence letter_sequence_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_sequence
    ADD CONSTRAINT letter_sequence_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: letter_template letter_template_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_template
    ADD CONSTRAINT letter_template_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: letter_template letter_template_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.letter_template
    ADD CONSTRAINT letter_template_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.app_user(id);

--
-- Name: lifecycle_case lifecycle_case_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_case
    ADD CONSTRAINT lifecycle_case_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lifecycle_case lifecycle_case_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_case
    ADD CONSTRAINT lifecycle_case_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: lifecycle_case lifecycle_case_started_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_case
    ADD CONSTRAINT lifecycle_case_started_by_fkey FOREIGN KEY (started_by) REFERENCES public.app_user(id);

--
-- Name: lifecycle_case lifecycle_case_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_case
    ADD CONSTRAINT lifecycle_case_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.lifecycle_template(id);

--
-- Name: lifecycle_reminder lifecycle_reminder_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_reminder
    ADD CONSTRAINT lifecycle_reminder_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lifecycle_task lifecycle_task_assignee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_assignee_user_id_fkey FOREIGN KEY (assignee_user_id) REFERENCES public.app_user(id);

--
-- Name: lifecycle_task lifecycle_task_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.lifecycle_case(id) ON DELETE CASCADE;

--
-- Name: lifecycle_task_comment lifecycle_task_comment_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task_comment
    ADD CONSTRAINT lifecycle_task_comment_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.app_user(id);

--
-- Name: lifecycle_task_comment lifecycle_task_comment_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task_comment
    ADD CONSTRAINT lifecycle_task_comment_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.lifecycle_task(id) ON DELETE CASCADE;

--
-- Name: lifecycle_task lifecycle_task_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lifecycle_task lifecycle_task_done_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_done_by_fkey FOREIGN KEY (done_by) REFERENCES public.app_user(id);

--
-- Name: lifecycle_task lifecycle_task_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_task
    ADD CONSTRAINT lifecycle_task_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: lifecycle_template lifecycle_template_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template
    ADD CONSTRAINT lifecycle_template_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: lifecycle_template lifecycle_template_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template
    ADD CONSTRAINT lifecycle_template_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lifecycle_template lifecycle_template_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template
    ADD CONSTRAINT lifecycle_template_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: lifecycle_template_task lifecycle_template_task_assignee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template_task
    ADD CONSTRAINT lifecycle_template_task_assignee_user_id_fkey FOREIGN KEY (assignee_user_id) REFERENCES public.app_user(id);

--
-- Name: lifecycle_template_task lifecycle_template_task_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_template_task
    ADD CONSTRAINT lifecycle_template_task_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.lifecycle_template(id) ON DELETE CASCADE;

--
-- Name: loan loan_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan
    ADD CONSTRAINT loan_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: loan loan_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan
    ADD CONSTRAINT loan_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: loan_installment loan_installment_loan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan_installment
    ADD CONSTRAINT loan_installment_loan_id_fkey FOREIGN KEY (loan_id) REFERENCES public.loan(id);

--
-- Name: loan_installment loan_installment_payslip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loan_installment
    ADD CONSTRAINT loan_installment_payslip_id_fkey FOREIGN KEY (payslip_id) REFERENCES public.payslip(id);

--
-- Name: location_assignment location_assignment_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location_assignment
    ADD CONSTRAINT location_assignment_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: location_assignment location_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location_assignment
    ADD CONSTRAINT location_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: location_assignment location_assignment_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location_assignment
    ADD CONSTRAINT location_assignment_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.location(id) ON DELETE CASCADE;

--
-- Name: location location_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location
    ADD CONSTRAINT location_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: location location_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.location
    ADD CONSTRAINT location_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: login_challenge login_challenge_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_challenge
    ADD CONSTRAINT login_challenge_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: lrn_attempt lrn_attempt_enrollment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_attempt
    ADD CONSTRAINT lrn_attempt_enrollment_id_fkey FOREIGN KEY (enrollment_id) REFERENCES public.lrn_enrollment(id) ON DELETE CASCADE;

--
-- Name: lrn_course lrn_course_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_course
    ADD CONSTRAINT lrn_course_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lrn_enrollment lrn_enrollment_assigned_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_assigned_by_fkey FOREIGN KEY (assigned_by) REFERENCES public.app_user(id);

--
-- Name: lrn_enrollment lrn_enrollment_certificate_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_certificate_file_id_fkey FOREIGN KEY (certificate_file_id) REFERENCES public.stored_file(id);

--
-- Name: lrn_enrollment lrn_enrollment_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lrn_enrollment lrn_enrollment_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.lrn_course(id);

--
-- Name: lrn_enrollment lrn_enrollment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: lrn_enrollment lrn_enrollment_review_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_review_id_fkey FOREIGN KEY (review_id) REFERENCES public.perf_review(id) ON DELETE SET NULL;

--
-- Name: lrn_enrollment lrn_enrollment_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_enrollment
    ADD CONSTRAINT lrn_enrollment_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.lrn_session(id);

--
-- Name: lrn_question lrn_question_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_question
    ADD CONSTRAINT lrn_question_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.lrn_course(id) ON DELETE CASCADE;

--
-- Name: lrn_session lrn_session_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_session
    ADD CONSTRAINT lrn_session_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: lrn_session lrn_session_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lrn_session
    ADD CONSTRAINT lrn_session_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.lrn_course(id) ON DELETE CASCADE;

--
-- Name: notification_outbox notification_outbox_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_outbox
    ADD CONSTRAINT notification_outbox_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: notification_outbox notification_outbox_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_outbox
    ADD CONSTRAINT notification_outbox_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: notification_outbox notification_outbox_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_outbox
    ADD CONSTRAINT notification_outbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: org_unit org_unit_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: org_unit org_unit_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: org_unit org_unit_head_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_head_employee_id_fkey FOREIGN KEY (head_employee_id) REFERENCES public.employee(id);

--
-- Name: org_unit org_unit_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_unit
    ADD CONSTRAINT org_unit_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.org_unit(id);

--
-- Name: ot_request ot_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_request
    ADD CONSTRAINT ot_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: ot_request ot_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_request
    ADD CONSTRAINT ot_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: ot_slip ot_slip_attendance_day_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_attendance_day_id_fkey FOREIGN KEY (attendance_day_id) REFERENCES public.attendance_day(id);

--
-- Name: ot_slip ot_slip_comp_off_ledger_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_comp_off_ledger_id_fkey FOREIGN KEY (comp_off_ledger_id) REFERENCES public.leave_ledger(id);

--
-- Name: ot_slip ot_slip_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: ot_slip ot_slip_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ot_slip
    ADD CONSTRAINT ot_slip_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public.app_user(id);

--
-- Name: payroll_hold payroll_hold_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: payroll_hold payroll_hold_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: payroll_hold payroll_hold_held_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_held_by_fkey FOREIGN KEY (held_by) REFERENCES public.app_user(id);

--
-- Name: payroll_hold payroll_hold_release_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_release_run_id_fkey FOREIGN KEY (release_run_id) REFERENCES public.payroll_run(id);

--
-- Name: payroll_hold payroll_hold_released_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_released_by_fkey FOREIGN KEY (released_by) REFERENCES public.app_user(id);

--
-- Name: payroll_hold payroll_hold_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_hold
    ADD CONSTRAINT payroll_hold_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.payroll_run(id);

--
-- Name: payroll_input payroll_input_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_input
    ADD CONSTRAINT payroll_input_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: payroll_input payroll_input_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_input
    ADD CONSTRAINT payroll_input_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: payroll_input payroll_input_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_input
    ADD CONSTRAINT payroll_input_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.payroll_run(id);

--
-- Name: payroll_opening_balance payroll_opening_balance_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_balance
    ADD CONSTRAINT payroll_opening_balance_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: payroll_opening_balance payroll_opening_balance_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_balance
    ADD CONSTRAINT payroll_opening_balance_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: payroll_opening_balance payroll_opening_balance_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_balance
    ADD CONSTRAINT payroll_opening_balance_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.app_user(id);

--
-- Name: payroll_period payroll_period_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_period
    ADD CONSTRAINT payroll_period_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: payroll_run payroll_run_checker_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_checker_id_fkey FOREIGN KEY (checker_id) REFERENCES public.app_user(id);

--
-- Name: payroll_run payroll_run_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: payroll_run payroll_run_maker_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_maker_id_fkey FOREIGN KEY (maker_id) REFERENCES public.app_user(id);

--
-- Name: payroll_run payroll_run_period_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_period_id_fkey FOREIGN KEY (period_id) REFERENCES public.payroll_period(id);

--
-- Name: payroll_run payroll_run_references_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_run
    ADD CONSTRAINT payroll_run_references_run_id_fkey FOREIGN KEY (references_run_id) REFERENCES public.payroll_run(id);

--
-- Name: payslip payslip_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip
    ADD CONSTRAINT payslip_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: payslip_line payslip_line_payslip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip_line
    ADD CONSTRAINT payslip_line_payslip_id_fkey FOREIGN KEY (payslip_id) REFERENCES public.payslip(id);

--
-- Name: payslip payslip_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payslip
    ADD CONSTRAINT payslip_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.payroll_run(id);

--
-- Name: perf_checkin perf_checkin_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_checkin
    ADD CONSTRAINT perf_checkin_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.app_user(id);

--
-- Name: perf_checkin perf_checkin_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_checkin
    ADD CONSTRAINT perf_checkin_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.perf_goal(id) ON DELETE CASCADE;

--
-- Name: perf_checkin perf_checkin_key_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_checkin
    ADD CONSTRAINT perf_checkin_key_result_id_fkey FOREIGN KEY (key_result_id) REFERENCES public.perf_key_result(id) ON DELETE SET NULL;

--
-- Name: perf_competency perf_competency_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_competency
    ADD CONSTRAINT perf_competency_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_cycle perf_cycle_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_cycle
    ADD CONSTRAINT perf_cycle_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_cycle perf_cycle_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_cycle
    ADD CONSTRAINT perf_cycle_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: perf_feedback perf_feedback_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_feedback
    ADD CONSTRAINT perf_feedback_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_feedback perf_feedback_giver_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_feedback
    ADD CONSTRAINT perf_feedback_giver_employee_id_fkey FOREIGN KEY (giver_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_feedback perf_feedback_requester_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_feedback
    ADD CONSTRAINT perf_feedback_requester_employee_id_fkey FOREIGN KEY (requester_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_feedback perf_feedback_subject_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_feedback
    ADD CONSTRAINT perf_feedback_subject_employee_id_fkey FOREIGN KEY (subject_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_goal perf_goal_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_goal perf_goal_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: perf_goal perf_goal_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: perf_goal perf_goal_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: perf_goal perf_goal_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_goal
    ADD CONSTRAINT perf_goal_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.perf_goal(id) ON DELETE SET NULL;

--
-- Name: perf_journal perf_journal_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_journal
    ADD CONSTRAINT perf_journal_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.app_user(id);

--
-- Name: perf_journal perf_journal_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_journal
    ADD CONSTRAINT perf_journal_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_journal perf_journal_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_journal
    ADD CONSTRAINT perf_journal_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: perf_key_result perf_key_result_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_key_result
    ADD CONSTRAINT perf_key_result_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.perf_goal(id) ON DELETE CASCADE;

--
-- Name: perf_kpi perf_kpi_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_kpi
    ADD CONSTRAINT perf_kpi_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_one_on_one perf_one_on_one_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one
    ADD CONSTRAINT perf_one_on_one_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_one_on_one perf_one_on_one_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one
    ADD CONSTRAINT perf_one_on_one_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: perf_one_on_one perf_one_on_one_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one
    ADD CONSTRAINT perf_one_on_one_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: perf_one_on_one_item perf_one_on_one_item_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one_item
    ADD CONSTRAINT perf_one_on_one_item_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.app_user(id);

--
-- Name: perf_one_on_one_item perf_one_on_one_item_meeting_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one_item
    ADD CONSTRAINT perf_one_on_one_item_meeting_id_fkey FOREIGN KEY (meeting_id) REFERENCES public.perf_one_on_one(id) ON DELETE CASCADE;

--
-- Name: perf_one_on_one_item perf_one_on_one_item_owner_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one_item
    ADD CONSTRAINT perf_one_on_one_item_owner_employee_id_fkey FOREIGN KEY (owner_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_one_on_one perf_one_on_one_manager_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_one_on_one
    ADD CONSTRAINT perf_one_on_one_manager_employee_id_fkey FOREIGN KEY (manager_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_peer perf_peer_nominated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_peer
    ADD CONSTRAINT perf_peer_nominated_by_fkey FOREIGN KEY (nominated_by) REFERENCES public.app_user(id);

--
-- Name: perf_peer perf_peer_review_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_peer
    ADD CONSTRAINT perf_peer_review_id_fkey FOREIGN KEY (review_id) REFERENCES public.perf_review(id) ON DELETE CASCADE;

--
-- Name: perf_peer perf_peer_reviewer_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_peer
    ADD CONSTRAINT perf_peer_reviewer_employee_id_fkey FOREIGN KEY (reviewer_employee_id) REFERENCES public.employee(id);

--
-- Name: perf_review perf_review_calibrated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_calibrated_by_fkey FOREIGN KEY (calibrated_by) REFERENCES public.app_user(id);

--
-- Name: perf_review perf_review_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: perf_review perf_review_cycle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_cycle_id_fkey FOREIGN KEY (cycle_id) REFERENCES public.perf_cycle(id) ON DELETE CASCADE;

--
-- Name: perf_review perf_review_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: perf_review_item perf_review_item_review_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review_item
    ADD CONSTRAINT perf_review_item_review_id_fkey FOREIGN KEY (review_id) REFERENCES public.perf_review(id) ON DELETE CASCADE;

--
-- Name: perf_review perf_review_manager_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perf_review
    ADD CONSTRAINT perf_review_manager_employee_id_fkey FOREIGN KEY (manager_employee_id) REFERENCES public.employee(id);

--
-- Name: policy_ack policy_ack_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_ack
    ADD CONSTRAINT policy_ack_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: policy_ack policy_ack_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_ack
    ADD CONSTRAINT policy_ack_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.company_policy(id);

--
-- Name: policy_nudge policy_nudge_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_nudge
    ADD CONSTRAINT policy_nudge_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: policy_nudge policy_nudge_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_nudge
    ADD CONSTRAINT policy_nudge_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.company_policy(id);

--
-- Name: probation_review probation_review_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.probation_review
    ADD CONSTRAINT probation_review_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: probation_review probation_review_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.probation_review
    ADD CONSTRAINT probation_review_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: probation_review probation_review_submitted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.probation_review
    ADD CONSTRAINT probation_review_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES public.app_user(id);

--
-- Name: profile_change_request profile_change_request_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: profile_change_request profile_change_request_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: profile_change_request profile_change_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: profile_change_request profile_change_request_proof_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_proof_file_id_fkey FOREIGN KEY (proof_file_id) REFERENCES public.stored_file(id);

--
-- Name: profile_change_request profile_change_request_requested_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_change_request
    ADD CONSTRAINT profile_change_request_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES public.app_user(id);

--
-- Name: punch_event punch_event_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.punch_event
    ADD CONSTRAINT punch_event_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.device(id);

--
-- Name: punch_event punch_event_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.punch_event
    ADD CONSTRAINT punch_event_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: punch_photo punch_photo_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_photo
    ADD CONSTRAINT punch_photo_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: punch_photo punch_photo_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.punch_photo
    ADD CONSTRAINT punch_photo_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: recompute_queue recompute_queue_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recompute_queue
    ADD CONSTRAINT recompute_queue_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: registered_device registered_device_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: registered_device registered_device_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: registered_device registered_device_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: registered_device registered_device_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_device
    ADD CONSTRAINT registered_device_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: regularisation_request regularisation_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regularisation_request
    ADD CONSTRAINT regularisation_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: regularisation_request regularisation_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regularisation_request
    ADD CONSTRAINT regularisation_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: regularisation_request regularisation_request_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regularisation_request
    ADD CONSTRAINT regularisation_request_shift_id_fkey FOREIGN KEY (shift_id) REFERENCES public.shift(id);

--
-- Name: relations_reminder_sent relations_reminder_sent_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relations_reminder_sent
    ADD CONSTRAINT relations_reminder_sent_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: roster roster_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roster
    ADD CONSTRAINT roster_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: roster roster_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roster
    ADD CONSTRAINT roster_shift_id_fkey FOREIGN KEY (shift_id) REFERENCES public.shift(id);

--
-- Name: rule_version rule_version_approved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_version
    ADD CONSTRAINT rule_version_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.app_user(id);

--
-- Name: rule_version rule_version_supersedes_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_version
    ADD CONSTRAINT rule_version_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES public.rule_version(id);

--
-- Name: salary_assignment salary_assignment_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_assignment
    ADD CONSTRAINT salary_assignment_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: salary_assignment salary_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_assignment
    ADD CONSTRAINT salary_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: salary_assignment salary_assignment_structure_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_assignment
    ADD CONSTRAINT salary_assignment_structure_version_id_fkey FOREIGN KEY (structure_version_id) REFERENCES public.salary_structure_version(id);

--
-- Name: salary_component salary_component_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_component
    ADD CONSTRAINT salary_component_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: salary_structure salary_structure_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure
    ADD CONSTRAINT salary_structure_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: salary_structure_version salary_structure_version_structure_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.salary_structure_version
    ADD CONSTRAINT salary_structure_version_structure_id_fkey FOREIGN KEY (structure_id) REFERENCES public.salary_structure(id);

--
-- Name: saved_report saved_report_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_report
    ADD CONSTRAINT saved_report_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: saved_report saved_report_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_report
    ADD CONSTRAINT saved_report_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: session session_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: shift_assignment shift_assignment_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_assignment
    ADD CONSTRAINT shift_assignment_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: shift_assignment shift_assignment_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_assignment
    ADD CONSTRAINT shift_assignment_shift_id_fkey FOREIGN KEY (shift_id) REFERENCES public.shift(id);

--
-- Name: shift shift_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift
    ADD CONSTRAINT shift_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: shift_swap shift_swap_colleague_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_colleague_id_fkey FOREIGN KEY (colleague_id) REFERENCES public.employee(id);

--
-- Name: shift_swap shift_swap_colleague_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_colleague_shift_id_fkey FOREIGN KEY (colleague_shift_id) REFERENCES public.shift(id);

--
-- Name: shift_swap shift_swap_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: shift_swap shift_swap_requester_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_requester_id_fkey FOREIGN KEY (requester_id) REFERENCES public.employee(id);

--
-- Name: shift_swap shift_swap_requester_shift_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shift_swap
    ADD CONSTRAINT shift_swap_requester_shift_id_fkey FOREIGN KEY (requester_shift_id) REFERENCES public.shift(id);

--
-- Name: stored_file stored_file_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_file
    ADD CONSTRAINT stored_file_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: stored_file stored_file_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_file
    ADD CONSTRAINT stored_file_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.app_user(id);

--
-- Name: suggestion suggestion_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion
    ADD CONSTRAINT suggestion_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: suggestion suggestion_replied_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion
    ADD CONSTRAINT suggestion_replied_by_fkey FOREIGN KEY (replied_by) REFERENCES public.app_user(id);

--
-- Name: survey_answer survey_answer_question_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_answer
    ADD CONSTRAINT survey_answer_question_id_fkey FOREIGN KEY (question_id) REFERENCES public.survey_question(id);

--
-- Name: survey_answer survey_answer_response_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_answer
    ADD CONSTRAINT survey_answer_response_id_fkey FOREIGN KEY (response_id) REFERENCES public.survey_response(id);

--
-- Name: survey survey_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey
    ADD CONSTRAINT survey_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: survey survey_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey
    ADD CONSTRAINT survey_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: survey_question survey_question_survey_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_question
    ADD CONSTRAINT survey_question_survey_id_fkey FOREIGN KEY (survey_id) REFERENCES public.survey(id);

--
-- Name: survey_response survey_response_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id);

--
-- Name: survey_response survey_response_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: survey_response survey_response_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES public.org_unit(id);

--
-- Name: survey_response survey_response_survey_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_response
    ADD CONSTRAINT survey_response_survey_id_fkey FOREIGN KEY (survey_id) REFERENCES public.survey(id);

--
-- Name: tada_rate tada_rate_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tada_rate
    ADD CONSTRAINT tada_rate_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: task_assignee task_assignee_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_assignee
    ADD CONSTRAINT task_assignee_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task(id) ON DELETE CASCADE;

--
-- Name: task_assignee task_assignee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_assignee
    ADD CONSTRAINT task_assignee_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: task_attachment task_attachment_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_attachment
    ADD CONSTRAINT task_attachment_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.app_user(id);

--
-- Name: task_attachment task_attachment_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_attachment
    ADD CONSTRAINT task_attachment_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.stored_file(id);

--
-- Name: task_attachment task_attachment_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_attachment
    ADD CONSTRAINT task_attachment_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task(id) ON DELETE CASCADE;

--
-- Name: task_board task_board_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board
    ADD CONSTRAINT task_board_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: task task_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task
    ADD CONSTRAINT task_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.task_board(id) ON DELETE CASCADE;

--
-- Name: task_board_member task_board_member_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board_member
    ADD CONSTRAINT task_board_member_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.task_board(id) ON DELETE CASCADE;

--
-- Name: task_board_member task_board_member_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board_member
    ADD CONSTRAINT task_board_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: task_board task_board_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_board
    ADD CONSTRAINT task_board_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.app_user(id);

--
-- Name: task_checklist_item task_checklist_item_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_item
    ADD CONSTRAINT task_checklist_item_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task(id) ON DELETE CASCADE;

--
-- Name: task_comment task_comment_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_comment
    ADD CONSTRAINT task_comment_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task(id) ON DELETE CASCADE;

--
-- Name: task_comment task_comment_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_comment
    ADD CONSTRAINT task_comment_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: task task_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task
    ADD CONSTRAINT task_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: task task_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task
    ADD CONSTRAINT task_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: task_reminder_sent task_reminder_sent_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminder_sent
    ADD CONSTRAINT task_reminder_sent_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task(id) ON DELETE CASCADE;

--
-- Name: task_reminder_sent task_reminder_sent_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminder_sent
    ADD CONSTRAINT task_reminder_sent_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: tax_declaration tax_declaration_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration
    ADD CONSTRAINT tax_declaration_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: tax_declaration_request tax_declaration_request_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration_request
    ADD CONSTRAINT tax_declaration_request_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: tax_declaration_request tax_declaration_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration_request
    ADD CONSTRAINT tax_declaration_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: tax_declaration_request tax_declaration_request_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration_request
    ADD CONSTRAINT tax_declaration_request_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: tax_declaration_request tax_declaration_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration_request
    ADD CONSTRAINT tax_declaration_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: tax_declaration tax_declaration_verified_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_declaration
    ADD CONSTRAINT tax_declaration_verified_by_fkey FOREIGN KEY (verified_by) REFERENCES public.app_user(id);

--
-- Name: time_activity time_activity_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_activity
    ADD CONSTRAINT time_activity_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: time_activity time_activity_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_activity
    ADD CONSTRAINT time_activity_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.time_project(id) ON DELETE CASCADE;

--
-- Name: time_client time_client_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_client
    ADD CONSTRAINT time_client_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: time_entry time_entry_activity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_activity_id_fkey FOREIGN KEY (activity_id) REFERENCES public.time_activity(id);

--
-- Name: time_entry time_entry_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: time_entry time_entry_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: time_entry time_entry_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: time_entry time_entry_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entry
    ADD CONSTRAINT time_entry_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.time_project(id);

--
-- Name: time_project time_project_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project
    ADD CONSTRAINT time_project_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.time_client(id);

--
-- Name: time_project time_project_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project
    ADD CONSTRAINT time_project_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: time_project_member time_project_member_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project_member
    ADD CONSTRAINT time_project_member_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: time_project_member time_project_member_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_project_member
    ADD CONSTRAINT time_project_member_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.time_project(id) ON DELETE CASCADE;

--
-- Name: timesheet timesheet_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: timesheet timesheet_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.app_user(id);

--
-- Name: timesheet timesheet_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: timesheet timesheet_submitted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES public.app_user(id);

--
-- Name: timesheet timesheet_workflow_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.timesheet
    ADD CONSTRAINT timesheet_workflow_instance_id_fkey FOREIGN KEY (workflow_instance_id) REFERENCES public.workflow_instance(id);

--
-- Name: travel_bill travel_bill_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_bill
    ADD CONSTRAINT travel_bill_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.stored_file(id);

--
-- Name: travel_bill travel_bill_settlement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_bill
    ADD CONSTRAINT travel_bill_settlement_id_fkey FOREIGN KEY (settlement_id) REFERENCES public.travel_settlement(id) ON DELETE CASCADE;

--
-- Name: travel_request travel_request_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_request
    ADD CONSTRAINT travel_request_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: travel_request travel_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_request
    ADD CONSTRAINT travel_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: travel_request travel_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_request
    ADD CONSTRAINT travel_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: travel_request travel_request_workflow_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_request
    ADD CONSTRAINT travel_request_workflow_instance_id_fkey FOREIGN KEY (workflow_instance_id) REFERENCES public.workflow_instance(id);

--
-- Name: travel_settlement travel_settlement_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: travel_settlement travel_settlement_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id);

--
-- Name: travel_settlement travel_settlement_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: travel_settlement travel_settlement_travel_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_travel_request_id_fkey FOREIGN KEY (travel_request_id) REFERENCES public.travel_request(id);

--
-- Name: travel_settlement travel_settlement_workflow_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.travel_settlement
    ADD CONSTRAINT travel_settlement_workflow_instance_id_fkey FOREIGN KEY (workflow_instance_id) REFERENCES public.workflow_instance(id);

--
-- Name: user_group user_group_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group
    ADD CONSTRAINT user_group_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: user_group_member user_group_member_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group_member
    ADD CONSTRAINT user_group_member_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.user_group(id) ON DELETE CASCADE;

--
-- Name: user_group_member user_group_member_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_group_member
    ADD CONSTRAINT user_group_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: user_recovery_code user_recovery_code_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_recovery_code
    ADD CONSTRAINT user_recovery_code_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id);

--
-- Name: work_break work_break_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_break
    ADD CONSTRAINT work_break_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: work_break work_break_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_break
    ADD CONSTRAINT work_break_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- Name: workflow_definition workflow_definition_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definition
    ADD CONSTRAINT workflow_definition_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: workflow_instance workflow_instance_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_instance
    ADD CONSTRAINT workflow_instance_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.company(id);

--
-- Name: workflow_instance workflow_instance_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_instance
    ADD CONSTRAINT workflow_instance_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id);

--
-- PostgreSQL database dump complete
--

