-- Audit A7b: an Approved Sizing is superseded by a later Sizing of its container, never by a
-- run that read its cluster without it, and that is kept on the approval (the run that
-- stored the Sizing), so pruning that run never makes it current again. One a later run
-- already read the cluster of keeps that answer: superseded by the first such run.
alter table approved_sizings add column superseded_run bigint;
update approved_sizings a set superseded_run = (
    select min(r.id) from sizing_runs r
    where r.contract_id = a.contract_id and r.id > a.run_id
    and r.clusters -> a.cluster = 'null'::jsonb
);
create index approved_current on approved_sizings (contract_id) where superseded_run is null;
