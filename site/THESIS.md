# NEAR, tested

**Relative to the dollar and the major currencies, NEAR's supply grows more slowly. It is not deflationary in absolute terms, and it is not cash.**

**The claim under test:** “NEAR is deflationary relative to the dollar and other currencies.”

I split the hypothesis behind that sentence into claims and tested each one, and the assumptions hiding inside them, against public data on today's economy. Of the 7 stated claims I could test, 3 hold, 2 fail and 2 land in between, and all 3 hidden assumptions fail. Two pieces carry the thesis. NEAR's supply grows slower than the major currencies' money, by a median +2.12 pp. Staking ran 0.32 pp above compounded fed funds over the past year. Thin, but there. The claim that NEAR suits sweep balances fails its own test in the ledger below.

_Data as of 2026-11-12 21:28 UTC. Every figure comes from `site/data/numbers.json`, where each one has its formula, inputs and raw files. The charts are on the site, https://gabehamilton.github.io/invest-in-near/ (`site/index.html` in this repository), with sources and formulas on its data page._

## The claim

NEAR is deflationary relative to the dollar and other currencies. Its supply grows slower than the money of the 10 most-traded currencies, by a median +2.12 pp over each one's latest year. Staking ran 0.32 pp above compounded fed funds over the past year.

**Scope.** I judge NEAR the way someone deciding today whether to hold it would, on the economy as it is now. Governance is left out, past and future: if the rules change, holders hear about it well in advance and can sell. Past prices count where they say something about the coming year, so most tests use the latest year of data.

**Strongest:** Supply, against money. In the year to 2026-09 NEAR's supply grew 2.67%, while the major currencies' money grew a median 4.79%, each over its latest year: a median gap of +2.12 pp. Chart [c21](https://gabehamilton.github.io/invest-in-near/index.html#c21).

**Weakest:** Staking ran only 0.32 pp above compounded fed funds over the year, the thinnest margin, in percentage points, of any supported claim. The ninety-day tripwire on this spread fires if staking falls below fed funds. Chart [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01).

There is no price target and no valuation model here. The thesis covers supply and yield, and stops before demand and price.

## Terms

Key: H is a claim as I wrote it, O an objection, A a question from the hypothesis with the claims that answer it, T a tripwire and c a chart.

- **pp (percentage points)**: The plain difference between two rates. Staking at a yield a little above fed funds is a spread of a fraction of a pp.
- **Epoch**: One validator round on NEAR, about 6.99 h long at the moment. The protocol mints new NEAR once per epoch.
- **Issuance ceiling**: The protocol's max_inflation_rate: the most new NEAR it mints in a year, as a share of supply. It is 2.50% today, and governance sets it.
- **Liquid staking token (stNEAR, LiNEAR)**: A receipt for staked NEAR whose price in NEAR rises as staking rewards accrue. Meta Pool issues stNEAR and LiNEAR Protocol issues LiNEAR. Liquid staking yield is the growth of that price.
- **Fed funds compounded**: What a dollar earning the effective fed funds rate every day of a window would have made. It is the like-for-like benchmark for a staking yield over the same days.
- **Spread**: Staking yield minus fed funds over the same days, in pp.
- **Fee burn**: NEAR destroyed when transactions pay for gas. Burn offsets issuance, and supply shrinks only when burn exceeds what the protocol mints.
- **Upper bound (burn)**: The most the burn can be, from supply alone: what the ceiling would have minted minus what supply grew by. It counts rewards left unminted by offline validators as burn, so the true burn cannot exceed it.
- **CPI and core CPI**: The US consumer price index for all urban consumers, published by the Bureau of Labor Statistics. Core leaves out food and energy.
- **US money supply**: The Federal Reserve's broad money measure: currency, checking and savings deposits, small time deposits and retail money funds.
- **Realized volatility**: The annualized standard deviation of daily price changes. A bigger number means bigger typical swings.
- **Sweep account rules**: The SEC's money market fund rule and its broker customer-protection rule, which set what a broker may sweep idle cash into.
- **Implied volatility**: The volatility an options price assumes. Implied minus realized is, on average, what an option seller earns for taking the risk.
- **Tripwire**: A measured result that would change the thesis, checked against the latest data. Some would break part of it; others would strengthen it, or open a test that is closed today.

## The thesis that survives

This is for someone who holds NEAR or is deciding whether to. NEAR's supply grows every epoch, 2.48% over the past year, so on its own it inflates. Hold it against money and it deflates: the dollar's money grew 5.71%, and 9 of the 10 most-traded currencies grew their money faster than NEAR's supply. The yield side is thinner. Staking ran 0.32 pp above compounded fed funds, and against other currencies' policy rates the result is mixed. The current issuance rule has been in force for 3 full quarters, too few to say whether the two rates stay in balance. In dollars, NEAR is a volatility bet.

**Supply grows slower than the major currencies' money** (H1b ✓ Supported · H2 ◐ Partly). Median gap to the major currencies' money growth, past year: +2.12 pp. In the year to 2026-09 NEAR's supply grew 2.67% and the dollar's money 5.71%; 9 of the 10 most-traded currencies grew their money faster than NEAR's supply over their own latest year. Against US consumer prices (3.72% in the year to 2026-09) NEAR's supply grew 1.04 pp slower, but the gap sat near the one the hypothesis claimed in only 1 of the last 11 months, so that claim is only partly supported. Charts [c21](https://gabehamilton.github.io/invest-in-near/index.html#c21), [c11](https://gabehamilton.github.io/invest-in-near/index.html#c11).

**Staking runs a little above fed funds; against other currencies' policy rates the result is mixed** (H3 ✓ Supported · H5 ◐ Partly · H4 ? Not testable). Liquid staking minus compounded fed funds, past year: +0.32 pp. Liquid staking yielded 4.13% against 3.81% for fed funds compounded over the same days. Against the policy rates of 10 economies the result is mixed: on rate minus own inflation NEAR's gap is 1.65 pp against a panel median of 0.85 pp, and on rate minus the US rate it is 0.44 pp against 1.38 pp. The current issuance rule has been in force for 3 full quarters, too few to say whether the two rates stay in balance. Charts [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01), [c13](https://gabehamilton.github.io/invest-in-near/index.html#c13), [c12](https://gabehamilton.github.io/invest-in-near/index.html#c12).

**What holding pays, and where staked NEAR ranks**. 7 of the 9 most-traded currencies with a policy rate pay less now than liquid staking NEAR paid over the last year, 4.13%. That sets each central bank's latest reported rate beside a past year's staking yield, and a policy rate is a benchmark that cash rarely earns in full; the like-for-like comparison is the one above. China's rate is the one-year Loan Prime Rate and Hong Kong's the HKMA base rate, which follows the US rate under the peg. A currency with no policy rate is left out. Staked NEAR ranks 1 of 10 among staked NEAR and cash at each of those policy rates, by what a year did to a holder's share of the total. For staked NEAR that is the staking yield against supply growth over the year to 2026-11-10, 4.13% against 2.48%, a change of 1.61%; for cash it is the policy rate against the currency's money growth over its latest year. Staking pays in NEAR, so its dollar value moves with NEAR's price.

## Evidence for

- [H1b](https://gabehamilton.github.io/invest-in-near/index.html#claim-H1b) “NEAR is deflationary compared to other currencies.”: ✓ Supported. Measured: how much slower NEAR supply grew than US money +3.04 pp, median of how much faster each major currency's money grew than NEAR supply +2.12 pp, major currencies whose money grew faster than NEAR supply 9, major currencies with a latest money-growth reading 10, US money supply, year over year 5.71%, NEAR supply growth, year to the money-supply month 2.67%. Charts [c21](https://gabehamilton.github.io/invest-in-near/index.html#c21), [c11](https://gabehamilton.github.io/invest-in-near/index.html#c11).
- [H3](https://gabehamilton.github.io/invest-in-near/index.html#claim-H3) “Current staking rewards of 4.13% are approximately 0.25% higher than the effective federal funds rate of 3.88%.”: ✓ Supported. Measured: staking minus fed funds, like for like, last year +0.32 pp, liquid staking yield, last year 4.13%, fed funds compounded, last year 3.81%, staking minus the average fed funds rate, last year +0.44 pp, average effective fed funds rate, last year 3.69%. Charts [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01), [c12](https://gabehamilton.github.io/invest-in-near/index.html#c12).
- [H7](https://gabehamilton.github.io/invest-in-near/index.html#claim-H7) “In addition, NEAR is volatile.”: ✓ Supported. Measured: NEAR price volatility, last year 103.3%, NEAR price volatility, last ninety days 127.5%, bitcoin price volatility, last year 44.0%. Charts [c08](https://gabehamilton.github.io/invest-in-near/index.html#c08), [c09](https://gabehamilton.github.io/invest-in-near/index.html#c09).
- [H6_near](https://gabehamilton.github.io/invest-in-near/index.html#claim-H6_near) Staked NEAR as storage for balances already held in NEAR: ✓ Supported. Measured: NEAR treasury tests passed, principal and yield 2, NEAR treasury liquidity test failed 0. Chart [c14](https://gabehamilton.github.io/invest-in-near/index.html#c14).

## Evidence against

- [H1](https://gabehamilton.github.io/invest-in-near/index.html#claim-H1) “NEAR is deflationary”: ✕ Refuted. Test: Did NEAR's total supply shrink over the last 365 days, and keep shrinking for at least 90 consecutive days? Measured: NEAR supply growth, last year 2.48%, days in the current run of shrinking supply 0 days. Chart [c02](https://gabehamilton.github.io/invest-in-near/index.html#c02).
- Partly: [H2](https://gabehamilton.github.io/invest-in-near/index.html#claim-H2) “Compared to the US dollar, NEAR's inflation rate of 2.5% is approximately 1.5% lower.”: ◐ Partly. Test: Is US CPI-U inflation minus NEAR supply growth within 0.5 pp of 1.5 pp now, and in at least 9 of the last 12 months with data? Measured: how much slower NEAR supply grew than US prices +1.04 pp, US consumer prices, year over year 3.72%, NEAR supply growth, year to the price month 2.67%, months in the last year near the claimed gap 1, months in the last year with data 11. Chart [c11](https://gabehamilton.github.io/invest-in-near/index.html#c11).
- Partly: [H5](https://gabehamilton.github.io/invest-in-near/index.html#claim-H5) “Other currencies have a wider spread.”: ◐ Partly. Test: On both fixed readings of 'spread', is NEAR's latest spread narrower than the latest median of the panel's economies? Measured: readings where NEAR's latest spread is narrower than the panel median 1, readings where NEAR's latest spread is wider than the panel median 1, NEAR's spread, rate minus own inflation +1.65 pp, NEAR's spread, rate minus US rate +0.44 pp, panel median spread, rate minus own inflation 0.85 pp, panel median spread, rate minus US rate 1.38 pp, economies in the panel 10. Chart [c13](https://gabehamilton.github.io/invest-in-near/index.html#c13).
- [H6](https://gabehamilton.github.io/invest-in-near/index.html#claim-H6) “This makes NEAR ideal for sweep funds and other currency storage.”: ✕ Refuted. Test: Does staked NEAR pass every row of the sweep-account checklist as a US dollar vehicle? Measured: sweep tests passed as a dollar vehicle 0, sweep tests 4, deciding sweep tests failed 2. Chart [c14](https://gabehamilton.github.io/invest-in-near/index.html#c14).
- [H9](https://gabehamilton.github.io/invest-in-near/index.html#claim-H9) Assumed: A yield paid in NEAR can be compared with a yield paid in dollars: ✕ Refuted. Test: In what share of the weekly one-year windows that end within the last 365 days did a dollar held in staked NEAR land within 2 pp of fed funds? Measured: one-year windows ending in the last year within two points of fed funds 3.77%, one-year dollar windows ending in the last year 53. Chart [c07](https://gabehamilton.github.io/invest-in-near/index.html#c07).
- [H10](https://gabehamilton.github.io/invest-in-near/index.html#claim-H10) Assumed: Fees burned by usage offset new issuance, so net supply growth is small or negative: ✕ Refuted. Test: What share of the NEAR minted in the last 365 days did fees burn? An upper bound can refute but cannot support. Measured: fees burned as a share of issuance, last year 1.51%, how the burn share was measured exact, epochs behind the burn share n/a, upper bound on the burn share 2.00%, the burn share from one epoch a month 1.52%. Chart [c05](https://gabehamilton.github.io/invest-in-near/index.html#c05).
- [H12](https://gabehamilton.github.io/invest-in-near/index.html#claim-H12) Assumed: Supply growth costs every NEAR holder the same share: ✕ Refuted. Test: Over the latest window, did stakers' and non-stakers' shares of supply change by the same amount (within 0.1 pp a year)? Measured: change in a staker's share of all NEAR, per year 1.61%, change in an unstaked holder's share, per year -2.42%. Chart [c03](https://gabehamilton.github.io/invest-in-near/index.html#c03).
- Staking yield depends on how much NEAR is staked. If the staked share climbs to 60.0%, the protocol formula pays about 3.14%, so the yield compresses as staking gets popular.
- The one demand figure shown rests on one data feed. DefiLlama's NEAR Intents series comes from a Dune table that NEAR maintains, and the independent cross-check needs partner access plus written permission to publish.
- The treasury account's balance dropped sharply in one quarter this year, and I could not find a published reason. It does not change supply growth, but it matters for sell pressure.

### The objections, answered

**[O1](https://gabehamilton.github.io/invest-in-near/index.html#O1) “Deflationary? Supply goes up every epoch.”** Stance: Conceded on the word, answered on discipline. Fair. NEAR's supply grows every epoch, so “deflationary” is the wrong word. The growth is small and it follows the protocol's ceiling: the 2.48% above, against a ceiling of 2.50%.

**[O2](https://gabehamilton.github.io/invest-in-near/index.html#O2) “Staking yield is just dilution. You're paying yourself.”** Stance: Answered for stakers. Only if everyone stakes. Staking pays more than new supply adds, so a liquid staker's share of all NEAR grew 1.61% a year. The cost lands on unstaked holders, whose share changed by -2.42% a year.

**[O3](https://gabehamilton.github.io/invest-in-near/index.html#O3) “Then there's no real yield. Fees don't cover issuance.”** Stance: Conceded. Fees burned 1.51% of the NEAR minted over the last year (exact). Usage does not offset issuance yet, so the staking yield is mostly a transfer from unstaked holders.

**[O4](https://gabehamilton.github.io/invest-in-near/index.html#O4) “So where does long-term value come from? Show me demand.”** Stance: Open. The thesis makes no claim about demand. Intents volume is shown as context for the fee burn: NEAR Intents settled $32.9 billion over the past year, and the latest ninety days ran at 1.84× the ninety days before (the latest 3 days are provisional and may be revised). Value that moves as stablecoins and other tokens holds no NEAR, so it reaches the token through fees, and the burn above is thin.

**[O5](https://gabehamilton.github.io/invest-in-near/index.html#O5) “Your yield is in NEAR. A dollar investor eats all of that volatility.”** Stance: Conceded for dollar holders. Measured in dollars, NEAR's price swung 103.3% annualized over the last year, and at its worst it sat 65.5% below its high within that year. Staking ran 0.32 pp above fed funds for the whole year, while a typical single day moved the price 5.41%.

## What would change my mind

Each tripwire is checked against the latest data, so these tables move when the data does. The supply comparison fails on H1b's own rule, gap_m2 ≤ 0; the staking comparison on H3's, spread_adj_365 ≤ 0.

### What would break it

Each row is a result that would break part of this thesis.

| Tripwire | Metric | Now | Threshold | State | What it would mean | Chart |
|---|---|---|---|---|---|---|
| T1 | NEAR supply growth over the last year | 2.48% | more than 0.5 pp above the issuance ceiling in force | clear | Supply growth breaks from the protocol rule that keeps it below the major currencies' money growth. | [c02](https://gabehamilton.github.io/invest-in-near/index.html#c02) |
| T2 | Change in stakers' share of supply over the last year | 1.61% | at or below zero | clear | Stakers lose share of supply, so staking no longer keeps a holder ahead of issuance. | [c03](https://gabehamilton.github.io/invest-in-near/index.html#c03) |
| T5 | NEAR Intents volume, latest ninety days against the ninety before | 1.84× | below 0.5 times the previous ninety days | clear | Demand for the network, which the supply comparison leaves out, is shrinking. | [c06](https://gabehamilton.github.io/invest-in-near/index.html#c06) |
| T6 | Share of NEAR supply staked | 44.4% | staked share above 60% | clear | The staking yield compresses toward the cell for that staked share in c10. | [c10](https://gabehamilton.github.io/invest-in-near/index.html#c10) |
| T7 | Staking yield minus fed funds over the last ninety days | +0.38 pp | below zero | clear | H3 flips: staking pays less than fed funds. | [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01) |

### What would confirm it

Each row is a result that would strengthen the thesis, or open a test that is closed today.

| Tripwire | Metric | Now | Threshold | State | What it would mean | Chart |
|---|---|---|---|---|---|---|
| T3 | Fees burned as a share of issuance over the last year | 1.51% | 25% or more of issuance | clear | Fees burned start to offset a real share of issuance. | [c05](https://gabehamilton.github.io/invest-in-near/index.html#c05) |
| T4 | Fees burned as a share of issuance over the last year | 1.51% | 100% or more of issuance | clear | NEAR becomes deflationary: burn exceeds issuance. | [c05](https://gabehamilton.github.io/invest-in-near/index.html#c05) |
| T8 | Venues listing NEAR options | 0 | one venue or more lists NEAR options | clear | H8 becomes testable. | [c15](https://gabehamilton.github.io/invest-in-near/index.html#c15) |

## The original hypothesis, audited

| Claim | As stated | Test | Verdict | Measured | Chart |
|---|---|---|---|---|---|
| [H1](https://gabehamilton.github.io/invest-in-near/index.html#claim-H1) | “NEAR is deflationary” | Did NEAR's total supply shrink over the last 365 days, and keep shrinking for at least 90 consecutive days? | ✕ Refuted | NEAR supply growth, last year 2.48%, days in the current run of shrinking supply 0 days | [c02](https://gabehamilton.github.io/invest-in-near/index.html#c02) |
| [H1b](https://gabehamilton.github.io/invest-in-near/index.html#claim-H1b) | “NEAR is deflationary compared to other currencies.” | Over each currency's latest year, did NEAR's supply grow slower than US broad money and than the median broad-money growth of the most-traded currencies? | ✓ Supported | how much slower NEAR supply grew than US money +3.04 pp, median of how much faster each major currency's money grew than NEAR supply +2.12 pp, major currencies whose money grew faster than NEAR supply 9, major currencies with a latest money-growth reading 10, US money supply, year over year 5.71%, NEAR supply growth, year to the money-supply month 2.67% | [c21](https://gabehamilton.github.io/invest-in-near/index.html#c21), [c11](https://gabehamilton.github.io/invest-in-near/index.html#c11) |
| [H2](https://gabehamilton.github.io/invest-in-near/index.html#claim-H2) | “Compared to the US dollar, NEAR's inflation rate of 2.5% is approximately 1.5% lower.” | Is US CPI-U inflation minus NEAR supply growth within 0.5 pp of 1.5 pp now, and in at least 9 of the last 12 months with data? | ◐ Partly | how much slower NEAR supply grew than US prices +1.04 pp, US consumer prices, year over year 3.72%, NEAR supply growth, year to the price month 2.67%, months in the last year near the claimed gap 1, months in the last year with data 11 | [c11](https://gabehamilton.github.io/invest-in-near/index.html#c11) |
| [H3](https://gabehamilton.github.io/invest-in-near/index.html#claim-H3) | “Current staking rewards of 4.13% are approximately 0.25% higher than the effective federal funds rate of 3.88%.” | Over the last 365 days, did liquid-staking yield beat compounded fed funds by more than zero and at most 1 pp (like for like)? | ✓ Supported | staking minus fed funds, like for like, last year +0.32 pp, liquid staking yield, last year 4.13%, fed funds compounded, last year 3.81%, staking minus the average fed funds rate, last year +0.44 pp, average effective fed funds rate, last year 3.69% | [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01), [c12](https://gabehamilton.github.io/invest-in-near/index.html#c12) |
| [H4](https://gabehamilton.github.io/invest-in-near/index.html#claim-H4) | “Though both the inflation rate and fed rate change, they stay in balance over time.” | Under the issuance rule now in force, over at least 8 full quarters that include a Fed move of 100 bp or more, is the quarterly spread's standard deviation below 1 pp with a level correlation above 0.5? | ? Not testable | standard deviation of the quarterly spread, current issuance rule 0.05 pp, correlation of the two rates, quarterly levels, current issuance rule -0.46, full quarters under the current issuance rule 3, how far the Fed's target ceiling moved in those quarters, basis points 25 | [c12](https://gabehamilton.github.io/invest-in-near/index.html#c12), [c01](https://gabehamilton.github.io/invest-in-near/index.html#c01) |
| [H5](https://gabehamilton.github.io/invest-in-near/index.html#claim-H5) | “Other currencies have a wider spread.” | On both fixed readings of 'spread', is NEAR's latest spread narrower than the latest median of the panel's economies? | ◐ Partly | readings where NEAR's latest spread is narrower than the panel median 1, readings where NEAR's latest spread is wider than the panel median 1, NEAR's spread, rate minus own inflation +1.65 pp, NEAR's spread, rate minus US rate +0.44 pp, panel median spread, rate minus own inflation 0.85 pp, panel median spread, rate minus US rate 1.38 pp, economies in the panel 10 | [c13](https://gabehamilton.github.io/invest-in-near/index.html#c13) |
| [H6](https://gabehamilton.github.io/invest-in-near/index.html#claim-H6) | “This makes NEAR ideal for sweep funds and other currency storage.” | Does staked NEAR pass every row of the sweep-account checklist as a US dollar vehicle? | ✕ Refuted | sweep tests passed as a dollar vehicle 0, sweep tests 4, deciding sweep tests failed 2 | [c14](https://gabehamilton.github.io/invest-in-near/index.html#c14) |
| [H7](https://gabehamilton.github.io/invest-in-near/index.html#claim-H7) | “In addition, NEAR is volatile.” | Is NEAR's realized volatility at least 50% a year over the last 365 days and over the last 90 days? | ✓ Supported | NEAR price volatility, last year 103.3%, NEAR price volatility, last ninety days 127.5%, bitcoin price volatility, last year 44.0% | [c08](https://gabehamilton.github.io/invest-in-near/index.html#c08), [c09](https://gabehamilton.github.io/invest-in-near/index.html#c09) |
| [H8](https://gabehamilton.github.io/invest-in-near/index.html#claim-H8) | “Rather than being a disadvantage this is an asset, volatility can be sold.” | Is there a NEAR options market, and has NEAR implied volatility exceeded later realized volatility (mean above zero, t above 2, at least 180 days)? | ? Not testable | venues listing NEAR options 0, options venues checked 3, options venues that could not be checked 2 | [c15](https://gabehamilton.github.io/invest-in-near/index.html#c15) |
| [H9](https://gabehamilton.github.io/invest-in-near/index.html#claim-H9) | Assumed: A yield paid in NEAR can be compared with a yield paid in dollars | In what share of the weekly one-year windows that end within the last 365 days did a dollar held in staked NEAR land within 2 pp of fed funds? | ✕ Refuted | one-year windows ending in the last year within two points of fed funds 3.77%, one-year dollar windows ending in the last year 53 | [c07](https://gabehamilton.github.io/invest-in-near/index.html#c07) |
| [H10](https://gabehamilton.github.io/invest-in-near/index.html#claim-H10) | Assumed: Fees burned by usage offset new issuance, so net supply growth is small or negative | What share of the NEAR minted in the last 365 days did fees burn? An upper bound can refute but cannot support. | ✕ Refuted | fees burned as a share of issuance, last year 1.51%, how the burn share was measured exact, epochs behind the burn share n/a, upper bound on the burn share 2.00%, the burn share from one epoch a month 1.52% | [c05](https://gabehamilton.github.io/invest-in-near/index.html#c05) |
| [H12](https://gabehamilton.github.io/invest-in-near/index.html#claim-H12) | Assumed: Supply growth costs every NEAR holder the same share | Over the latest window, did stakers' and non-stakers' shares of supply change by the same amount (within 0.1 pp a year)? | ✕ Refuted | change in a staker's share of all NEAR, per year 1.61%, change in an unstaked holder's share, per year -2.42% | [c03](https://gabehamilton.github.io/invest-in-near/index.html#c03) |

The rules that decided each verdict are listed at the end.

## Not computed, and why

- [H4](https://gabehamilton.github.io/invest-in-near/index.html#claim-H4) Staking yield and fed funds stay in balance, ? Not testable: fewer than 8 full quarters have passed under the issuance rule now in force. Would make it testable: 8 full quarters under the issuance rule now in force.
- [H8](https://gabehamilton.github.io/invest-in-near/index.html#claim-H8) NEAR's volatility can be sold, ? Not testable: none of the options venues I could check lists NEAR options, so there is no NEAR implied volatility to sell. Would make it testable: a checked venue lists NEAR options. Not computed until then: NEAR implied minus realized volatility, mean (vrp_near_mean), t-statistic of that premium (vrp_near_t), days of NEAR implied volatility (vrp_near_days).

## Desk simulation

A scenario, not a forecast: part of a trading desk's idle dollars and euros moved into liquid-staked NEAR for one year. The default outputs below assume NEAR's price is flat on average, daily moves like the last year's, and no trading or custody costs; the page lets a reader change each assumption.

Each default output, beside the assumption it rests on:

- The desk: $500 million held for 365 days, 76% in dollar cash at the effective fed funds rate (3.88%) and 24% in euro cash at €STR (2.44%). Assumes rates stay where they are today; the split is a benchmark proxy from the two currencies' shares of currency trading, not any desk's holdings.
- The NEAR part: 3.7% of the desk, $18.50 million, liquid-staked at 4.13% and bought with dollars and euros in the cash's own proportions. Assumes the largest share whose bad-year average fits inside one year of the whole desk's interest.
- Expected benefit: $90,000 ahead of dollars and euros only; the yield over cash, before any price change, is $90,000 of it, and $70,000 of that is the euro-dollar rate gap (euro cash earns less than dollar cash), so against dollar cash alone staking adds $20,000. Assumes NEAR's price is flat on average and no trading or custody costs; it is exact (closed form), not simulated.
- The same expected benefit under the other price assumptions: $380,000 behind if demand for NEAR is flat in dollars, $700,000 ahead if the thesis's implication holds (3.15%, which assumes demand for NEAR keeps pace with dollar money).
- Typical year: $8.0 million behind. Assumes the next year's daily moves look like the last year's, a volatility of 103.3%, over 20,000 simulated years.
- Ahead of dollars and euros only in 30 of 100 simulated years, whatever the NEAR share.
- Bad year: 5 in 100 simulated years end worse than $17.1 million behind, and those years average $17.80 million behind, about 1.0 years of the whole desk's interest.
- For the mix to come out ahead in most years, NEAR's price would need to rise about 70% on average; costs above 0.48% of the NEAR part would erase the expected benefit.
- Getting out: about 3.1 days to sell the NEAR part at a whole median day of Coinbase volume, before unstaking.

This is a scenario tool, not investment advice. It shows what the measured yield, volatility and liquidity imply under an assumption you choose; it does not predict NEAR's price.

## Limits and open questions

What this page cannot settle yet, and what would settle each item.

- **Slashing** (unverified). As far as I can tell NEAR does not slash validator stake today, but I have not confirmed it in the protocol spec or the nearcore source. If slashing exists, liquid staking carries a loss path that none of the yield figures include. Would resolve it: Cite the nearcore code path or the Nomicon section that governs slashing, pinned to a commit.
- **LiNEAR instant-exit depth** (not measured). Meta Pool's instant-unstake pool holds 75,887 NEAR, read from its contract. LiNEAR's equivalent was not measured, so the same-day liquidity row of the sweep checklist is marked partial. Would resolve it: Read LiNEAR's instant-unstake liquidity and fee from its contract at the same height as the Meta Pool read.
- **Binance and Bybit options, not checkable from the US** (not checkable). Binance's options API and Bybit's public endpoints refuse requests from US addresses, so the venue table marks them not checked rather than no. Unchecked venues: 2. Would resolve it: A venue check from a non-US network, with each venue's HTTP status.
- **Purpose of the treasury outflow** (unverified). The treasury.near balance, column treasury_near in data/raw/rpc/history_samples.csv, dropped sharply in one quarter this year. I could not find a published purpose. Supply growth is unaffected; sell pressure might not be. Would resolve it: A treasury report or an on-chain trace that names where the NEAR went.
- **Origin of the quoted staking rate** (unverified). The hypothesis quotes a staking rate without saying where it came from. The ledger tests the claim against the measured trailing-year liquid staking yield, 4.13%, and shows both. Would resolve it: Name the dashboard and the date the quoted rate came from.
- **BigQuery freshness** (depends on BigQuery). Exact burn figures need the public NEAR BigQuery dataset, whose latest block date is 2026-11-10; where it has no data the figure is labelled sampled or upper bound. Would resolve it: The BigQuery dataset keeping pace with the chain (the data page shows its lag in days).
- **Permission to publish Intents Explorer aggregates** (open). The NEAR Intents Explorer comparison needs partner access and written permission to publish under its terms. It is not included, so the Intents volume chart rests on DefiLlama alone. Would resolve it: Partner access and a written answer from NEAR Intents.
- **Bank of England money series** (open). Sterling money here is the wide aggregate the Bank publishes as M4; the narrower version that excludes intermediate financial companies is the closer match for other currencies' broad money, but its series code is not confirmed. Would resolve it: The confirmed Bank of England series code for the narrower aggregate.

## Rules, applied in order

A null input gives Not computed; then Not testable, Supported, Refuted; anything else is Partly.

- H1: Supported if g_365 < 0 and g_365_neg_run_days ≥ 90 · Refuted if g_365 > 0 · otherwise Partly. Deciding condition: g_365 > 0.
- H1b: Not computed if n_fiat_money < 7 · Supported if gap_m2 > 0 and h1b_gap_med > 0 · Refuted if gap_m2 ≤ 0 · otherwise Partly. Deciding condition: gap_m2 > 0 and h1b_gap_med > 0.
- H2: Supported if |gap_cpi - 1.5| ≤ 0.5 and h2_months_in_band_12 ≥ 9 · Refuted if gap_cpi ≤ 0 · otherwise Partly. Deciding condition: neither the Supported nor the Refuted rule holds.
- H3: Supported if spread_adj_365 > 0 and spread_adj_365 ≤ 1.0 · Refuted if spread_adj_365 ≤ 0 · Partly if spread_adj_365 > 1.0. Deciding condition: spread_adj_365 > 0 and spread_adj_365 ≤ 1.0.
- H4: Not testable if n_q_rule < 8 · Supported if spread_q_sd_rule < 1.0 and corr_levels_rule > 0.5 and n_q_rule ≥ 8 and fed_range_q_bp_rule ≥ 100 · Refuted if spread_q_sd_rule > 1.5 or corr_levels_rule < 0.2 · otherwise Partly. Deciding condition: n_q_rule < 8.
- H5: Supported if h5_now_narrower = 2 and h5_now_wider = 0 · Refuted if h5_now_wider = 2 · otherwise Partly. Deciding condition: neither the Supported nor the Refuted rule holds.
- H6: Supported if h6_rows_total > 0 and h6_rows_passed = h6_rows_total and h6_critical_failed = 0 · Refuted if h6_critical_failed ≥ 1 · otherwise Partly. Deciding condition: h6_critical_failed ≥ 1.
- H7: Supported if rv_365_near ≥ 50 and rv_90_near ≥ 50 · Refuted if rv_365_near < 25 · otherwise Partly. Deciding condition: rv_365_near ≥ 50 and rv_90_near ≥ 50.
- H8: Not computed if n_option_venues_checked = 0 · Not testable if n_venues_with_near_options = 0 · Supported if vrp_near_mean > 0 and vrp_near_t > 2 and vrp_near_days ≥ 180 · Refuted if vrp_near_mean ≤ 0 · otherwise Partly. Deciding condition: n_venues_with_near_options = 0.
- H9: Not computed if n_windows_usd_1y < 26 · Supported if pct_within_2pp_1y ≥ 50 · Refuted if pct_within_2pp_1y < 20 · Partly if pct_within_2pp_1y ≥ 20. Deciding condition: pct_within_2pp_1y < 20.
- H10: Supported if burn_share_365 ≥ 50 and burn_method ≠ “upper bound” and burn_share_365_ub ≥ 50 · Refuted if burn_share_365 < 10 · otherwise Partly. Deciding condition: burn_share_365 < 10.
- H12: Supported if |staker_share_365 - nonstaker_365| < 0.1 · Refuted if staker_share_365 × nonstaker_365 < 0 and |staker_share_365 - nonstaker_365| ≥ 0.1 · otherwise Partly. Deciding condition: staker_share_365 × nonstaker_365 < 0 and |staker_share_365 - nonstaker_365| ≥ 0.1.
- H6_near: Supported if h6n_rows13_pass = 2 and h6n_row2_fail = 0 · Refuted if h6n_rows13_pass = 0 · otherwise Partly. Deciding condition: h6n_rows13_pass = 2 and h6n_row2_fail = 0.

---

Built with AI assistance: Claude Code wrote the code and the prose, and I directed it. Every number comes from public data and links to its formula.

