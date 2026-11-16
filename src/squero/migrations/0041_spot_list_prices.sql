-- Slice 16.4a: spot list prices, beside the on-demand ones (a table of their own: a spot
-- row can never take an on-demand one's place, and an older Hub reads list_prices as it
-- did). An AWS one is read with a Contract's own credentials and kept for it alone
-- (contract_id); Azure's and GCP's are public, shared ('').
create table spot_list_prices (
    currency text not null,
    provider text not null,
    region text not null,
    instance_type text not null,
    contract_id text not null default '',
    hour_price double precision,  -- the last good price, null before the first
    ok_at double precision,  -- when it was read
    tried_at double precision not null,
    error text,  -- why the last try failed, null when it worked
    primary key (currency, provider, region, instance_type, contract_id)
);
