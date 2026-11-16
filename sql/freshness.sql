-- name: freshness
-- description: First and last block_date present in the NEAR blocks table, with the latest block height and time. Run before every other job: no later query may request dates beyond max_block_date, and a last day whose final block is before 23:59 UTC is treated as partial. Ingestion is UNVERIFIED after NEAR Lake stopped on 2026-03-24 (docs.near.org/data-infrastructure/lake-framework/near-lake), so this is the only source of truth for the usable window. Other tables may lag blocks; this checks blocks only. Cost: three 8-byte columns per block over the scanned range, so the first full-window run is several GB and later incremental runs scan a few days.
-- grain: one row for the whole window
-- params: @start_date DATE, @end_date DATE (inclusive block_date range; the runner passes BQ_START_DATE or 2020-07-21 genesis and today UTC on the first run, then the cached max_block_date and today UTC, keeping the cached min_block_date and adding days_present minus the one shared day)
-- est: 0.002 GB/day (blocks: block_date, block_height, block_timestamp_utc = 24 bytes x ~78k blocks/day, DERIVED from the Nov 2024 table size; recent days are heavier, the dry run is authoritative)
SELECT
  MIN(block_date) AS min_block_date,
  MAX(block_date) AS max_block_date,
  COUNT(DISTINCT block_date) AS days_present,
  DATE_DIFF(MAX(block_date), MIN(block_date), DAY) + 1 AS days_spanned,
  MAX(block_height) AS max_block_height,
  MAX(block_timestamp_utc) AS max_block_timestamp_utc
FROM `bigquery-public-data.crypto_near_mainnet_us.blocks`
WHERE block_date BETWEEN @start_date AND @end_date
