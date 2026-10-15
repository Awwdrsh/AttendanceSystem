-- Employee engagement: announcements, kudos with company values, celebrations, surveys and polls, and an anonymous
-- suggestion box. Anonymous survey responses carry only a keyed hash per survey and person (to stop double voting);
-- nothing in a response row identifies the respondent.

CREATE TABLE IF NOT EXISTS engage_setting (
  company_id           uuid PRIMARY KEY REFERENCES company(id),
  kudos_monthly_limit  smallint CHECK (kudos_monthly_limit IS NULL OR kudos_monthly_limit BETWEEN 1 AND 100),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS engage_value (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  description text CHECK (length(description) <= 200),
  emoji       text NOT NULL DEFAULT '⭐' CHECK (length(emoji) <= 8),
  active      boolean NOT NULL DEFAULT true,
  sort_order  smallint NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, name)
);

INSERT INTO engage_value (company_id, name, description, emoji, sort_order)
SELECT c.id, v.name, v.description, v.emoji, v.sort_order
  FROM company c
 CROSS JOIN (VALUES
   ('Ownership', 'Took responsibility and saw it through', '🎯', 1),
   ('Teamwork', 'Made the people around them better', '🤝', 2),
   ('Customer first', 'Went the extra mile for a customer', '💚', 3),
   ('Innovation', 'Found a better way to do it', '💡', 4),
   ('Integrity', 'Did the right thing, even when it was hard', '🛡️', 5)
 ) AS v(name, description, emoji, sort_order)
 WHERE NOT EXISTS (SELECT 1 FROM engage_value x WHERE x.company_id = c.id)
ON CONFLICT DO NOTHING;

-- Employees choose whether colleagues see their birthday (off until they turn it on).
CREATE TABLE IF NOT EXISTS engage_pref (
  employee_id     uuid PRIMARY KEY REFERENCES employee(id),
  show_birthday   boolean NOT NULL DEFAULT false,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS announcement (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  author_id          uuid NOT NULL REFERENCES app_user(id),
  author_employee_id uuid REFERENCES employee(id),
  title              text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  body               text NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
  audience_kind      text NOT NULL DEFAULT 'all' CHECK (audience_kind IN ('all', 'branch', 'department', 'reports')),
  audience_id        uuid,
  pinned             boolean NOT NULL DEFAULT false,
  requires_ack       boolean NOT NULL DEFAULT false,
  expires_on         date,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  deleted_at         timestamptz
);
CREATE INDEX IF NOT EXISTS announcement_company_idx ON announcement (company_id, created_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS announcement_ack (
  announcement_id uuid NOT NULL REFERENCES announcement(id),
  user_id         uuid NOT NULL REFERENCES app_user(id),
  acked_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (announcement_id, user_id)
);

CREATE TABLE IF NOT EXISTS kudos (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid NOT NULL REFERENCES company(id),
  giver_id          uuid NOT NULL REFERENCES app_user(id),
  giver_employee_id uuid NOT NULL REFERENCES employee(id),
  value_id          uuid REFERENCES engage_value(id),
  message           text NOT NULL CHECK (length(message) BETWEEN 1 AND 1000),
  created_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz
);
CREATE INDEX IF NOT EXISTS kudos_company_idx ON kudos (company_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kudos_giver_idx ON kudos (giver_employee_id, created_at);

CREATE TABLE IF NOT EXISTS kudos_recipient (
  kudos_id    uuid NOT NULL REFERENCES kudos(id),
  employee_id uuid NOT NULL REFERENCES employee(id),
  PRIMARY KEY (kudos_id, employee_id)
);
CREATE INDEX IF NOT EXISTS kudos_recipient_emp_idx ON kudos_recipient (employee_id);

-- Reactions and comments on feed items: kudos, announcements and celebrations (birthday:<emp>:<bsYear>, anniversary:..., joined:...).
CREATE TABLE IF NOT EXISTS engage_reaction (
  company_id  uuid NOT NULL REFERENCES company(id),
  target_type text NOT NULL CHECK (target_type IN ('kudos', 'announcement', 'celebration')),
  target_id   text NOT NULL CHECK (length(target_id) <= 120),
  user_id     uuid NOT NULL REFERENCES app_user(id),
  emoji       text NOT NULL CHECK (emoji IN ('like', 'love', 'clap', 'celebrate', 'laugh')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, target_type, target_id, user_id, emoji)
);

CREATE TABLE IF NOT EXISTS engage_comment (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  target_type text NOT NULL CHECK (target_type IN ('kudos', 'announcement', 'celebration')),
  target_id   text NOT NULL CHECK (length(target_id) <= 120),
  user_id     uuid NOT NULL REFERENCES app_user(id),
  body        text NOT NULL CHECK (length(body) BETWEEN 1 AND 1000),
  created_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS engage_comment_target_idx ON engage_comment (company_id, target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS survey (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  kind           text NOT NULL DEFAULT 'survey' CHECK (kind IN ('survey', 'poll')),
  title          text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  description    text CHECK (length(description) <= 2000),
  anonymous      boolean NOT NULL DEFAULT true,
  min_group      smallint NOT NULL DEFAULT 5 CHECK (min_group BETWEEN 3 AND 50),
  audience_kind  text NOT NULL DEFAULT 'all' CHECK (audience_kind IN ('all', 'branch', 'department')),
  audience_id    uuid,
  status         text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'closed')),
  opens_on       date NOT NULL,
  closes_on      date NOT NULL,
  recurrence     text NOT NULL DEFAULT 'none' CHECK (recurrence IN ('none', 'weekly', 'monthly')),
  series_id      uuid,
  created_by     uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  published_at   timestamptz,
  opened_notified_at timestamptz,
  reminded_at    timestamptz,
  closed_at      timestamptz,
  CHECK (closes_on >= opens_on)
);
CREATE INDEX IF NOT EXISTS survey_company_idx ON survey (company_id, status, closes_on);

CREATE TABLE IF NOT EXISTS survey_question (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  survey_id  uuid NOT NULL REFERENCES survey(id),
  position   smallint NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('rating', 'enps', 'single', 'multi', 'text')),
  prompt     text NOT NULL CHECK (length(prompt) BETWEEN 1 AND 300),
  options    jsonb NOT NULL DEFAULT '[]',
  required   boolean NOT NULL DEFAULT true,
  UNIQUE (survey_id, position)
);

-- respondent_key: keyed hash of (survey, user); employee_id only for surveys that are not anonymous. The org unit and
-- branch are kept for breakdowns, which are shown only for groups at or above the survey's minimum group size.
CREATE TABLE IF NOT EXISTS survey_response (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),   -- random, not time-ordered: ids must not reveal when someone answered
  survey_id      uuid NOT NULL REFERENCES survey(id),
  respondent_key text NOT NULL,
  employee_id    uuid REFERENCES employee(id),
  org_unit_id    uuid REFERENCES org_unit(id),
  branch_id      uuid REFERENCES branch(id),
  submitted_on   date NOT NULL,
  UNIQUE (survey_id, respondent_key)
);

CREATE TABLE IF NOT EXISTS survey_answer (
  response_id  uuid NOT NULL REFERENCES survey_response(id),
  question_id  uuid NOT NULL REFERENCES survey_question(id),
  value_num    smallint,
  choices      text[],
  text_value   text CHECK (length(text_value) <= 2000),
  PRIMARY KEY (response_id, question_id)
);

-- Anonymous suggestion box: no author is stored, and only the day it arrived.
CREATE TABLE IF NOT EXISTS suggestion (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES company(id),
  category     text NOT NULL DEFAULT 'general' CHECK (category IN ('general', 'workplace', 'process', 'benefits', 'culture', 'other')),
  body         text NOT NULL CHECK (length(body) BETWEEN 5 AND 3000),
  received_on  date NOT NULL,
  status       text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewed', 'answered', 'archived')),
  reply        text CHECK (length(reply) <= 3000),
  is_public    boolean NOT NULL DEFAULT false,
  replied_by   uuid REFERENCES app_user(id),
  replied_at   timestamptz
);
CREATE INDEX IF NOT EXISTS suggestion_company_idx ON suggestion (company_id, received_on DESC);
