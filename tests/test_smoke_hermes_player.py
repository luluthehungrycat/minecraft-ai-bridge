"""Offline lifecycle checks for the operator's real-profile smoke runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("first_returncode", [0, 124])
def test_smoke_batch_stops_after_unresolved_timeout(
    tmp_path, monkeypatch, capsys, first_returncode
):
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-hermes-player.py"
    spec = importlib.util.spec_from_file_location("smoke_hermes_player", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def run_trial(name, output, profile):
        calls.append(name)
        return {"returncode": first_returncode if name == "wall" else 0, "error": None}

    monkeypatch.setattr(module, "run_trial", run_trial)
    monkeypatch.setattr("sys.argv", [str(path), "--output", str(tmp_path), "wall", "navigation"])
    if first_returncode == 124:
        with pytest.raises(SystemExit, match="1"):
            module.main()
        assert calls == ["wall"]
        assert "reconcile" in capsys.readouterr().err.lower()
    else:
        module.main()
        assert calls == ["wall", "navigation"]
