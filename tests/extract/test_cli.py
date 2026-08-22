"""`python -m pipeline.extract` reads `.env` like the ingest and load CLIs.

Phase 2 debt, paid at the Phase 6 closeout: a manual run finds `ANTHROPIC_API_KEY` in `.env`
without an export. (`rfp-run` already loaded `.env` once for its children; this is for the hand
path.) `load_dotenv` never overrides a variable already set, so an exported value still wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.extract import cli


def test_extract_cli_loads_dotenv_before_doing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    order: list[str] = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: order.append("dotenv"))
    # An empty data root: the CLI stops at "no ingest manifest" (exit 1) before building a
    # client or touching the output root, so the only observable effect is whether `.env`
    # was loaded first — which is the contract.
    code = cli.main([
        "--data-root", str(tmp_path / "raw"), "--output-root", str(tmp_path / "extracted"),
    ])
    assert code == 1
    assert order == ["dotenv"]
    assert "no ingest manifest" in capsys.readouterr().err
    assert not (tmp_path / "extracted").exists()
