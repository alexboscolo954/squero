-- Audit A14: Stale notices. One row per Contract, cluster and kind of read while it fails:
-- since when (the read's own, else when the Hub first saw it), the Slack thread of its
-- notice once posted, and when it was last told (claimed before posting, so never twice).
create table stale_notices (
    contract_id text not null,
    cluster text not null,
    kind text not null,
    since double precision not null,
    ts text,
    told_at double precision,
    primary key (contract_id, cluster, kind)
);
