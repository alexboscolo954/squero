-- Audit A7: the Hub counts what each cluster's Actuator reported in the last hour, on every
-- report it receives, to store at most 1,000 an hour.
create index actuations_received on actuations (contract_id, cluster, received_at);
