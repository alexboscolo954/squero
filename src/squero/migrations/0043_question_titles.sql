-- Audit A2: a Question's title is the Question whole, cut where it is shown after
-- masking; the Questions stored cut at 200 get theirs back from their input.
update investigations
set title = input ->> 'question'
where kind = 'question' and title <> input ->> 'question';  -- null: left as it is
