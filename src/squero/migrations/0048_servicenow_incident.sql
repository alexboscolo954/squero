-- The Contract's ServiceNow incident for a Report (slice 19.5b), filed once by the
-- Operator: its number, its sys_id (for the link), who, and when it was claimed
-- (claimed without a number: being filed, or unknown).
alter table investigations add column servicenow_incident text,
    add column servicenow_id text,
    add column servicenow_by text,
    add column servicenow_at double precision;
