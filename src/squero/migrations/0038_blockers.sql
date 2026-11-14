-- Blockers (slice 15.1b): each cluster's Actuator's latest report of the pods that may
-- keep a node from being removed, as facts; replaced by every report. `received` is the
-- Hub's clock, the one its age goes by.
create table cluster_blockers (
    contract_id text not null,
    cluster text not null,
    received double precision not null,
    report jsonb not null,  -- blockers.Report: at, namespaces, truncated, pods
    primary key (contract_id, cluster)
);
