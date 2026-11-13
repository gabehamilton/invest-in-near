-- name: daily_supply_flows
-- description: End-of-day total supply and the gross mint and burn behind it, from the per-block change in blocks.total_supply (yoctoNEAR, FLOAT64) ordered by block_height. Supply only rises at epoch boundaries (validator and treasury reward mint) and only falls through burns, so positive deltas are summed as minted_near and negative deltas as burnt_near; the boundary block's own burn is netted into its mint. epoch_boundaries counts deltas above 10 NEAR. The query also reads @start_date - 1 so the first block of the range has a predecessor, then drops that day. A day whose first block has no predecessor in the previous calendar day (a gap in the table) gets empty mint, burn and boundary counts instead of a delta that spans the gap; genesis day 2020-07-21 is exempt. burnt_near is total supply destruction (fee burn net of the 30% contract reward, plus any other burn), not gross fees.
-- grain: one row per block_date
-- params: @start_date DATE, @end_date DATE (inclusive block_date range; the query reads block_date from @start_date - 1 for the LAG and drops that day)
-- est: 0.002 GB/day (blocks: block_date, block_height, total_supply = 24 bytes x ~78k blocks/day, DERIVED from the Nov 2024 table size; recent days are heavier, the dry run is authoritative)
WITH block_supply AS (
  SELECT
    block_date,
    block_height,
    CAST(total_supply AS FLOAT64) AS total_supply
  FROM `bigquery-public-data.crypto_near_mainnet_us.blocks`
  WHERE block_date BETWEEN DATE_SUB(@start_date, INTERVAL 1 DAY) AND @end_date
),
deltas AS (
  SELECT
    block_date,
    block_height,
    total_supply,
    total_supply - LAG(total_supply) OVER chain AS supply_delta,
    LAG(block_date) OVER chain AS prev_block_date
  FROM block_supply
  WINDOW chain AS (ORDER BY block_height)
),
days AS (
  SELECT
    block_date,
    ARRAY_AGG(total_supply IGNORE NULLS ORDER BY block_height DESC LIMIT 1)[SAFE_OFFSET(0)] AS eod_supply,
    SUM(IF(supply_delta > 0, supply_delta, 0)) AS minted,
    SUM(IF(supply_delta < 0, -supply_delta, 0)) AS burnt,
    COUNTIF(supply_delta > 1e25) AS boundaries,
    LOGICAL_AND(
      block_date = DATE '2020-07-21'
      OR (supply_delta IS NOT NULL AND DATE_DIFF(block_date, prev_block_date, DAY) <= 1)
    ) AS deltas_complete
  FROM deltas
  WHERE block_date BETWEEN @start_date AND @end_date
  GROUP BY block_date
)
SELECT
  block_date,
  eod_supply / 1e24 AS eod_supply_near,
  IF(deltas_complete, minted / 1e24, NULL) AS minted_near,
  IF(deltas_complete, burnt / 1e24, NULL) AS burnt_near,
  IF(deltas_complete, boundaries, NULL) AS epoch_boundaries
FROM days
ORDER BY block_date
