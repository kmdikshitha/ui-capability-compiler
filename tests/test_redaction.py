"""Nothing sensitive reaches disk, and the redactor does not eat the payload."""

from __future__ import annotations

import json

from src.evidence.recorder import Recorder
from src.guardrails.redaction import (
    RedactionFilter, clear_sensitive, redact, redact_text, register_sensitive,
)


def test_sensitive_input_is_masked_but_correlatable() -> None:
    assert register_sensitive("member_id", "12345") == "MEMB_****45"
    assert redact_text("servicing member 12345 now") == "servicing member MEMB_****45 now"


def test_money_is_redacted() -> None:
    assert redact_text("balance 8,915.20 available") == "balance *** available"
    assert redact_text("opened with $250.00") == "opened with ***"


def test_secret_keys_are_never_written() -> None:
    out = redact({"Authorization": "Bearer abc", "cookie": "sess=1",
                  "ANTHROPIC_API_KEY": "sk-ant-xyz", "safe": "kept"})
    assert out["Authorization"] == "[REDACTED]"
    assert out["cookie"] == "[REDACTED]"
    assert out["ANTHROPIC_API_KEY"] == "[REDACTED]"
    assert out["safe"] == "kept"


def test_redaction_recurses_through_structures() -> None:
    register_sensitive("member_id", "12345")
    out = redact({"steps": [{"note": "member 12345"}, {"n": {"deep": "12345"}}]})
    assert "12345" not in json.dumps(out)


def test_nothing_sensitive_lands_in_a_run_directory(tmp_path) -> None:
    """The property the definition of done actually asserts."""
    register_sensitive("member_id", "12345")
    recorder = Recorder("run-under-test", base=tmp_path)
    recorder.meta(inputs={"member_id": "12345"}, goal="open a sub-account for 12345")
    recorder.transcript({"tool": "type", "args": {"text": "12345"},
                         "observation": {"text_digest": "Member 12345 balance 1,240.55"}})
    recorder.step({"step_id": "s4", "note": "typed 12345"})
    recorder.result({"outputs": {"balance": "1,240.55"}})

    written = "".join(p.read_text() for p in recorder.dir.rglob("*") if p.is_file())
    assert "12345" not in written
    assert "1,240.55" not in written
    assert "MEMB_****45" in written


def test_logging_filter_redacts_records() -> None:
    import logging
    register_sensitive("member_id", "12345")
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                              "servicing 12345", None, None)
    RedactionFilter().filter(record)
    assert record.msg == "servicing MEMB_****45"


def test_clearing_between_runs() -> None:
    register_sensitive("member_id", "12345")
    clear_sensitive()
    assert redact_text("member 12345") == "member 12345"
