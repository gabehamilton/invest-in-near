-- name: fee_burn
-- description: Gross fee burn and gas burnt per sampled day: execution_outcomes.tokens_burnt and gas_burnt (receipt execution) plus transactions.receipt_conversion_tokens_burnt and receipt_conversion_gas_burnt (transaction-to-receipt conversion), yoctoNEAR / 1e24 and gas / 1e12. Gross: until HSP-027 took effect (thesis/constants.toml hsp027_activation: first block with burnt_gas_reward 0, nearcore 2.14.0) nearcore recorded tokens_burnt before paying 30% of function-call gas to the called contract (runtime/src/lib.rs, burnt_gas_reward 3/10), so on days before that block this overstates the true supply burn; from that block the rebate is 0 and gross equals net. daily_supply_flows.burnt_near is the net throughout. Amount columns are cast to FLOAT64 because their stored types are only known from a Nov 2024 schema dump. A day missing from either table produces no row.
-- grain: one row per block_date
-- params: @start_date DATE, @end_date DATE (inclusive block_date range on both tables; the runner passes one sampled day or a contiguous run of sampled days within one month)
-- est: 0.19 GB/day (execution_outcomes: block_date, tokens_burnt, gas_burnt = 24 bytes x ~6.2M outcomes/day; transactions: block_date, receipt_conversion_tokens_burnt, receipt_conversion_gas_burnt = 24 bytes x ~1.75M tx/day; DERIVED from the Nov 2024 table sizes, recent days are heavier, the dry run is authoritative)
WITH outcomes AS (
  SELECT
    block_date,
    SUM(CAST(tokens_burnt AS FLOAT64)) AS tokens_burnt,
    SUM(CAST(gas_burnt AS FLOAT64)) AS gas_burnt
  FROM `bigquery-public-data.crypto_near_mainnet_us.execution_outcomes`
  WHERE block_date BETWEEN @start_date AND @end_date
  GROUP BY block_date
),
conversions AS (
  SELECT
    block_date,
    SUM(CAST(receipt_conversion_tokens_burnt AS FLOAT64)) AS tokens_burnt,
    SUM(CAST(receipt_conversion_gas_burnt AS FLOAT64)) AS gas_burnt
  FROM `bigquery-public-data.crypto_near_mainnet_us.transactions`
  WHERE block_date BETWEEN @start_date AND @end_date
  GROUP BY block_date
)
SELECT
  o.block_date,
  (o.tokens_burnt + c.tokens_burnt) / 1e24 AS gross_fee_burn_near,
  (o.gas_burnt + c.gas_burnt) / 1e12 AS gas_burnt_tgas
FROM outcomes AS o
INNER JOIN conversions AS c
  ON c.block_date = o.block_date
ORDER BY o.block_date
