-- Slice 16.4b: what a billed day's spot lines cost, beside its total, so a spot price can
-- be checked against the bill; null for a day read before (the next daily read has it).
alter table billed_costs add column spot_amount double precision;
-- and why the last read of a cluster's spot lines failed (a read of its own, beside the
-- total's: a shape not as expected costs only the spot part), null when it worked
alter table billing_tries add column spot_error text;
