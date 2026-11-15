-- Nodes (slice 15.3b): a DaemonSet's Approved Sizing per instance type, as its row had
-- them when it was approved: {"<type>": {"cpu": cores|null, "memory": bytes|null}}, null
-- where a type keeps a request. Null: one Sizing for every pod, as before.
alter table approved_sizings add column types jsonb;
