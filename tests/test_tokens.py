from standup_board.tokens import make_client_token, read_client_token

SECRET = "s3cr3t-signing-key"
EMAIL = "user@example.com"


def test_round_trip_recovers_email():
    token = make_client_token(SECRET, EMAIL)
    assert read_client_token(SECRET, token) == EMAIL


def test_token_is_opaque_not_plaintext_email():
    # The email must be signed, not just embedded in the clear.
    token = make_client_token(SECRET, EMAIL)
    assert EMAIL not in token


def test_wrong_secret_is_rejected():
    token = make_client_token(SECRET, EMAIL)
    assert read_client_token("different-secret", token) is None


def test_tampered_token_is_rejected():
    token = make_client_token(SECRET, EMAIL)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert read_client_token(SECRET, tampered) is None


def test_garbage_token_is_rejected():
    assert read_client_token(SECRET, "not-a-real-token") is None


def test_empty_token_is_rejected():
    assert read_client_token(SECRET, "") is None


def test_old_salt_token_is_rejected():
    # A token minted under the previous salt must not verify post-rename.
    from itsdangerous import URLSafeSerializer

    legacy = URLSafeSerializer(SECRET, salt="happening-now-client-token").dumps(EMAIL)
    assert read_client_token(SECRET, legacy) is None
