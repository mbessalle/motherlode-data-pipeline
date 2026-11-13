-- Read-only integrity checks for a region table (north_america, oceania, ...).
-- Run after every bulk import and on a schedule. Replace :table before running, e.g. with psql:
--     psql "$DATABASE_URL" -v table=north_america -f tools/integrity_checks.sql
-- Each query returns one row of counts; the comment says what a healthy table looks like.

-- 1. Row-shift detector.
--    A column pasted one row off still looks plausible cell by cell. It shows up when a value matches
--    the arithmetic of the NEXT or PREVIOUS row far more often than its own.
--    Healthy: own_row is the large number; next_row and previous_row are near zero.
with t as (
    select id, shares_value,
           shares_amount * share_price                               as own,
           lead(shares_amount * share_price) over (order by id)      as next,
           lag(shares_amount * share_price)  over (order by id)      as previous
    from :table
)
select count(shares_value)                                                                     as with_share_value,
       count(*) filter (where abs(own      - shares_value) <= greatest(1, 0.02 * shares_value)) as own_row,
       count(*) filter (where abs(next     - shares_value) <= greatest(1, 0.02 * shares_value)) as next_row,
       count(*) filter (where abs(previous - shares_value) <= greatest(1, 0.02 * shares_value)) as previous_row
from t;

-- 2. Aggregate consistency.
--    Healthy: consistent is close to with_aggregate. A low share means a part was moved or overwritten.
select count(aggregate_deal_value) as with_aggregate,
       count(*) filter (where abs(coalesce(cash_payments_value, 0) + coalesce(exp_comit_value, 0) + coalesce(shares_value, 0)
                                  - aggregate_deal_value) <= greatest(1, 0.02 * aggregate_deal_value)) as consistent
from :table;

-- 3. Name and row belong together.
--    A project name sorted independently of its row still looks valid. The article title is the witness:
--    in a healthy table a good share of titles mention their own project, and no project appears twice.
select count(*)                                                                    as rows,
       count(*) filter (where news_title ilike '%' || project_name || '%')         as title_mentions_project,
       count(*) - count(distinct project_name)                                     as duplicate_names,
       count(*) - count(distinct news_link)                                        as duplicate_links
from :table;

-- 4. Shape of the values.
--    Healthy: every count is zero.
select count(*) filter (where nsr_percent not between 0 and 100)                    as nsr_out_of_range,
       count(*) filter (where interest_acquired_percent not between 0 and 100)      as interest_out_of_range,
       count(*) filter (where latitude not between -90 and 90
                           or longitude not between -180 and 180)                   as bad_coordinates,
       count(*) filter (where cash_payments_value < 0 or shares_amount < 0)         as negative_amounts,
       count(*) filter (where aggregate_deal_value is not null
                          and aggregate_deal_currency is null)                      as aggregate_without_currency
from :table;
