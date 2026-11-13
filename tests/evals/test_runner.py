import json

from app.evals import run_evaluations
from app.evals import runner as evaluation
from app.evals.__main__ import main, write_report


async def test_labeled_synthetic_evaluations_pass_and_have_no_raw_inputs():
    report = await run_evaluations()
    assert report["failed"] == 0 and report["total"] >= 25
    assert report["passed"] == report["total"]
    assert report["synthetic_data_only"] and not report["live_data_used"] and report["network_calls"] == 0
    assert set(report["groups"]) == {"known_injection", "benign_input", "privacy", "retrieval", "tools"}
    raw = json.dumps(report)
    assert "synthetic-secret" not in raw and "567890" not in raw and "Ignore all previous" not in raw
    assert len(raw) < 200_000


async def test_eval_catches_guard_regression_and_does_not_just_mirror_success(monkeypatch):
    monkeypatch.setattr(evaluation, "guard_input", lambda *_a, **_k: None)
    report = await run_evaluations()
    assert report["failed"] >= len(evaluation.ATTACK_CASES)
    assert report["groups"]["known_injection"]["passed"] == 0
    assert report["groups"]["benign_input"]["fixture_pass_ratio"] == 1


def test_cli_writes_atomic_utf8_report_and_returns_success(tmp_path):
    path = tmp_path / "eval" / "report.json"
    assert main(["--output", str(path)]) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["failed"] == 0
    assert not list(path.parent.glob(".eval-*"))


def test_cli_returns_nonzero_for_failed_cases_and_still_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "guard_input", lambda *_a, **_k: None)
    path = tmp_path / "report.json"
    assert main(["--output", str(path)]) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["failed"] > 0


def test_report_limits_reject_oversize_before_overwriting(tmp_path):
    import pytest
    path = tmp_path / "report.json"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="size"):
        write_report(path, {"oversize": "x" * 200_001})
    assert path.read_text(encoding="utf-8") == "original"
