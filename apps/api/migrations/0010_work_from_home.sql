-- Employees allowed to work from home can clock in from anywhere without a reason; their home clock-ins are tagged remote.
ALTER TABLE employee ADD COLUMN IF NOT EXISTS work_from_home boolean NOT NULL DEFAULT false;
