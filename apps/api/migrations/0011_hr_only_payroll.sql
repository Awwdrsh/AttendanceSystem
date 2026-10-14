-- Payroll is run end to end by HR; there is no separate finance role. Existing finance users become HR users.
UPDATE app_user
   SET roles = (SELECT array_agg(DISTINCT r ORDER BY r) FROM unnest(array_replace(roles, 'finance', 'hr')) AS r)
 WHERE 'finance' = ANY(roles);

UPDATE workflow_definition
   SET steps = replace(steps::text, '"finance"', '"hr"')::jsonb
 WHERE steps::text LIKE '%"finance"%';

-- One HR sign-off replaces the finance maker-checker and two-approver threshold; a separate approver is optional.
UPDATE company_setting
   SET value = (value - 'twoApproverThreshold') || jsonb_build_object('separateApprover', false)
 WHERE key = 'payroll.policy' AND NOT value ? 'separateApprover';
