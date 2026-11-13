# invest-in-near

NEAR is deflationary relative to the dollar and other currencies. This repo tests that claim against public data and
publishes the result at https://gabehamilton.github.io/invest-in-near/.

The argument is in [THESIS.md](THESIS.md), the charts with their queries in `site/index.html`, and every source and
formula in `site/data.html`.

This is a submission to the 2026 November NearHackathon. See [nearhackathon.md](nearhackathon.md) for the submission
details.

## Rebuild

You need [uv](https://docs.astral.sh/uv/) and `make`.

```sh
uv sync
make charts        # recompute every number, verdict and chart from data/raw/ into site/ and THESIS.md
```

To re-pull the data first:

```sh
make data && make charts
```

FRED, BIS, the NEAR RPC, DefiLlama and the exchange APIs need no keys. BigQuery and the NEAR Intents Explorer need
credentials:

```sh
cp .env.example .env
# set GCP_PROJECT (the project billed for BigQuery) and NEAR_INTENTS_JWT (NEAR Intents partner dashboard)
gcloud auth application-default login
make check-bq      # dry run: what each query would scan
make data && make charts
```

A source without credentials is skipped and shows as not configured on the data page.

The Explorer token comes from NEAR Intents' partner dashboard. Its terms allow storing daily aggregates only, and
this repo publishes them only after NEAR Intents agrees in writing: until then `publish = false` under
`[sources.intents_explorer]` in `thesis/sources.toml` keeps them off the pages, and `.gitignore` keeps the files local.

## Licence

MIT. See [LICENSE](LICENSE).
