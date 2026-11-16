# One-command entry points. See README.md.
#   make charts  - rebuild every chart + derived number from the cached raw data in data/raw/ (offline, no keys)
#   make data    - re-pull raw data from every source (needs .env for BigQuery + Intents; others are public)
#   make all     - data then charts
#   make serve   - serve the static site at http://localhost:8000
#   make lint    - number lint: no digit reaches a page except through the registry (needs make charts first)
#   make explore THESIS=mine.md [ARGS=--chat]
#                - compare your own thesis with this one on NEAR AI Cloud, enclave attestation checked first
#                  (needs NEAR_AI_API_KEY in .env; see pipeline/explore.py)
#   make verify  - offline rebuild in a temp dir from data/raw only, byte-diff against site/ and THESIS.md,
#                  lint, blocking cross-checks (X1, X3), then the test suite

PY := uv run python

.PHONY: all data charts serve test check-bq lint verify explore

all: data charts

data:
	$(PY) -m pipeline.pull

charts:
	$(PY) -m pipeline.build

check-bq:
	$(PY) -m pipeline.sources.bigquery --dry-run

serve:
	$(PY) -m http.server 8000 --directory site

test:
	uv run pytest -q

lint:
	$(PY) -m pipeline.lint

verify:
	$(PY) -m pipeline.build --verify
	uv run pytest -q

explore:
	@test -n "$(THESIS)" || { echo "usage: make explore THESIS=path/to/your-thesis.md [ARGS=--chat]"; exit 2; }
	$(PY) -m pipeline.explore "$(THESIS)" $(ARGS)
