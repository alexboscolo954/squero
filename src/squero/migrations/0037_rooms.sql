-- Nodes (slice 15.1a): each cluster's nodes with their Room, as each hourly run reads
-- them from kube-state-metrics; a cluster's latest, replaced on every run.
create table cluster_rooms (
    contract_id text not null,
    cluster text not null,
    at double precision not null,  -- the run that read them
    rooms jsonb not null,  -- {"nodes": [...], "problem": text|null} (usage.rooms())
    primary key (contract_id, cluster)
);
