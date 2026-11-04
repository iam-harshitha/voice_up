"""Signed, single-use button tokens and pairing codes (pure functions)."""

import pytest

from app.text import tokens

SECRET = "unit-test-secret-" + "z" * 32
OTHER_SECRET = "another-secret-" + "w" * 32


def test_signed_token_round_trips_within_telegrams_64_bytes():
    nonce = tokens.new_nonce()
    for action in tokens.ACTIONS:
        data = tokens.sign(SECRET, action, nonce)
        assert len(data.encode()) <= 64
        assert tokens.verify(SECRET, data) == (action, nonce)


def test_tampered_signature_fails():
    data = tokens.sign(SECRET, tokens.REJECT, tokens.new_nonce())
    flipped = data[:-1] + ("A" if data[-1] != "A" else "B")
    assert tokens.verify(SECRET, flipped) is None


def test_swapping_the_action_breaks_the_signature():
    nonce = tokens.new_nonce()
    reject = tokens.sign(SECRET, tokens.REJECT, nonce)
    swapped = reject.replace(":r:", ":c:", 1)  # try to turn Reject into Confirm approve
    assert tokens.parse(swapped) is not None  # well-formed...
    assert tokens.verify(SECRET, swapped) is None  # ...but not signed for that action


def test_a_token_signed_with_another_secret_fails():
    assert (
        tokens.verify(SECRET, tokens.sign(OTHER_SECRET, tokens.APPROVE, tokens.new_nonce())) is None
    )


@pytest.mark.parametrize(
    "data",
    [
        "",
        "al1:a",
        "al2:a:" + "A" * 22 + ":" + "B" * 22,  # wrong version
        "al1:z:" + "A" * 22 + ":" + "B" * 22,  # unknown action
        "al1:a:short:" + "B" * 22,  # bad nonce
        "al1:a:" + "A" * 22 + ":" + "B" * 22 + ":extra",
        "x" * 65,
    ],
)
def test_malformed_tokens_are_rejected(data):
    assert tokens.verify(SECRET, data) is None


def test_nonces_and_pairing_codes_are_random_and_well_formed():
    assert len({tokens.new_nonce() for _ in range(200)}) == 200
    code = tokens.new_pairing_code()
    assert tokens.is_pairing_code(code) and len(code) == 32
    assert not tokens.is_pairing_code(code + "x") and not tokens.is_pairing_code("bad code")


def test_only_the_hash_of_a_nonce_is_meant_to_be_stored():
    nonce = tokens.new_nonce()
    digest = tokens.sha256_hex(nonce)
    assert len(digest) == 64 and nonce not in digest
