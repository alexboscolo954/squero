-- Nodes (slice 15.3c): the money of a DaemonSet applied per instance type.
-- Whether each cluster's Actuator applies a DaemonSet's Sizings per instance type, as its
-- last pull of the desired state said; written when it changes.
create table cluster_actuators (
    contract_id text not null,
    cluster text not null,
    types boolean not null,
    at double precision not null,  -- the pull that said it
    primary key (contract_id, cluster)
);
-- A realized drop of one instance type's pods' request; null: the container's, as before
-- (rows stored before keep null). "" is a type: nodes without the label.
alter table realized add column type text;
do $$
declare n text;
begin
    select conname into n from pg_constraint where conrelid = 'realized'::regclass and contype = 'u';
    execute format('alter table realized drop constraint %I', n);
end $$;
alter table realized add constraint realized_drop unique nulls not distinct
    (contract_id, cluster, namespace, kind, name, container, resource, type, prev_run);
