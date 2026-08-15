"""Secrets must never reach data/app.log — that file gets emailed to support."""

import logging

from src.logging_setup import redact, setup_logging

FAKE_KEY = "0x" + "a1b2c3d4" * 8  # 64 hex chars, same shape as a real private key
ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"


def test_redact_removes_private_keys():
    assert FAKE_KEY not in redact(f"signing with {FAKE_KEY} now")
    assert "<redacted-private-key>" in redact(FAKE_KEY)


def test_redact_shortens_addresses_but_keeps_them_recognisable():
    assert redact(f"account {ADDRESS}") == "account 0x5eA3...c41b"


def test_redact_leaves_ordinary_text_alone():
    message = "LONG 0.05 BTC @ 63018.5 (stop 62400, risk 5.00 USDC)"
    assert redact(message) == message


def _read_log(path):
    logging.shutdown()
    return path.read_text(encoding="utf-8")


def test_private_key_never_reaches_the_log_file(tmp_path):
    log_file = tmp_path / "app.log"
    setup_logging(to_console=False, file_path=log_file)

    logging.getLogger("test").info("using key %s for %s", FAKE_KEY, ADDRESS)

    contents = _read_log(log_file)
    assert FAKE_KEY not in contents
    assert "<redacted-private-key>" in contents
    assert ADDRESS not in contents


def test_a_key_inside_a_traceback_is_redacted(tmp_path):
    """Exception text is formatted separately from the message — cover it too."""
    log_file = tmp_path / "app.log"
    setup_logging(to_console=False, file_path=log_file)

    try:
        raise ValueError(f"bad key {FAKE_KEY}")
    except ValueError:
        logging.getLogger("test").exception("order failed")

    contents = _read_log(log_file)
    assert "Traceback" in contents
    assert FAKE_KEY not in contents
