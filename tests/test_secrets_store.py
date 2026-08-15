"""Key validation. The messages matter as much as the rejection."""

import pytest

from src.secrets_store import normalise_private_key

KEY = "0x" + "a1b2c3d4" * 8  # 64 hex characters
ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"  # 40


def test_a_key_passes_through():
    assert normalise_private_key(KEY) == KEY


def test_the_0x_prefix_is_optional():
    assert normalise_private_key(KEY[2:]) == KEY


def test_surrounding_whitespace_is_forgiven():
    assert normalise_private_key(f"  {KEY}\n") == KEY


def test_an_address_is_named_for_what_it_is():
    """Approving an API wallet shows both its address and its key, and they get
    copied in the wrong order often enough to say so plainly."""
    with pytest.raises(ValueError, match="is a wallet address, not a private key"):
        normalise_private_key(ADDRESS)


def test_anything_else_says_what_was_expected():
    with pytest.raises(ValueError, match="64 hexadecimal"):
        normalise_private_key("hunter2")


def test_an_empty_value_is_refused():
    with pytest.raises(ValueError, match="No private key"):
        normalise_private_key("   ")


def test_the_key_is_never_echoed_back_in_an_error():
    """Error text reaches the UI and the logs; the key must not ride along."""
    for bad in (ADDRESS, "nope", KEY[:-4]):
        with pytest.raises(ValueError) as caught:
            normalise_private_key(bad)
        assert bad not in str(caught.value)
