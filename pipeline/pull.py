"""Re-pull raw data from every source into data/raw/.

Usage:
    uv run python -m pipeline.pull                 # every source whose credentials are present
    uv run python -m pipeline.pull --only fred,bis # a subset
    uv run python -m pipeline.pull --skip bigquery

Each source module exposes:
    NAME: str               short id, also the data/raw/<NAME>/ directory
    REQUIRES: list[str]     env vars that must be set (empty for public sources)
    pull(args) -> None      fetch and write raw files via pipeline.config helpers

A source with missing credentials is skipped (its cached files are kept), so the
site can always be rebuilt from whatever raw data is on disk.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import traceback

from pipeline.config import env

# source name (module NAME) -> module path; imported lazily so --only works per source
SOURCES = {
    "fred": "pipeline.sources.fred",
    "bis": "pipeline.sources.bis",
    "bis_fx": "pipeline.sources.bis_fx",
    "money": "pipeline.sources.money",
    "prices": "pipeline.sources.prices",
    "derivatives": "pipeline.sources.derivatives",
    "near_rpc": "pipeline.sources.near_rpc",
    "defillama": "pipeline.sources.defillama",
    "intents": "pipeline.sources.intents",
    "bigquery": "pipeline.sources.bigquery",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated source names to run")
    parser.add_argument("--skip", help="comma-separated source names to skip")
    parser.add_argument("--yes", action="store_true", help="BigQuery: run even if the dry-run estimate exceeds BQ_BUDGET_GB")
    args = parser.parse_args(argv)

    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()
    unknown = sorted(((only or set()) | skip) - set(SOURCES))
    if unknown:
        parser.error(f"unknown source {', '.join(unknown)}; the sources are {', '.join(SOURCES)}")

    results: list[tuple[str, str, float]] = []
    for name, module_path in SOURCES.items():
        if (only and name not in only) or name in skip:
            continue
        module = importlib.import_module(module_path)
        missing = [var for var in module.REQUIRES if not env(var)]
        if missing:
            print(f"[{name}] skipped: set {', '.join(missing)} in .env (cached raw files kept)")
            results.append((name, "skipped (no credentials)", 0.0))
            continue
        print(f"[{name}] pulling ...")
        started = time.time()
        try:
            module.pull(args)
            results.append((name, "ok", time.time() - started))
        except Exception as exc:  # keep going so one broken API doesn't block the rest
            # a traceback can quote a credential (a project id or token in a URL): mask every value the source
            # reads from .env before printing
            text = traceback.format_exc()
            message = str(exc)
            for var in module.REQUIRES:
                value = env(var)
                if value:
                    text, message = text.replace(value, f"<{var}>"), message.replace(value, f"<{var}>")
            print(text, file=sys.stderr, end="")
            results.append((name, f"FAILED: {message}", time.time() - started))

    print("\nsource         status")
    for name, status, seconds in results:
        print(f"{name:<14} {status} ({seconds:.0f}s)")
    return 1 if any(status.startswith("FAILED") for _, status, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
