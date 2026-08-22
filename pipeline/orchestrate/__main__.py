"""Entry point: `python -m pipeline.orchestrate` — the same as `rfp-run`."""

from pipeline.orchestrate.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
