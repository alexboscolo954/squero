-- Audit A6a: the RAM GiB-hours a billed day's lines charged for (GCP's export only), so a
-- day billed for fewer hours than its nodes were sampled shows as still settling; null
-- where the cloud's read doesn't say (AWS, Azure) or for a day read before.
alter table billed_costs add column ram_hours double precision;
