-- Device command resend policy (plan §7.3/§7.7): count delivery attempts so an unacknowledged command is re-sent once
-- and then marked failed instead of staying 'sent' forever.
ALTER TABLE device_command ADD COLUMN attempts integer NOT NULL DEFAULT 0;
CREATE INDEX device_command_sent_idx ON device_command (device_id, sent_at) WHERE status = 'sent';
-- The poll response is newline-delimited: a command containing a line break would smuggle a second command past the
-- SHELL check. Enforce single-line commands for every writer, not only the gateway store.
ALTER TABLE device_command ADD CONSTRAINT device_command_single_line CHECK (command !~ E'[\r\n]');
