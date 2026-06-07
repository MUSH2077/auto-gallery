from app.auth import create_access_token, decode_access_token, decode_access_token_payload


def test_access_token_contains_password_rotation_flag() -> None:
    token = create_access_token("admin", must_change_password=True)
    payload = decode_access_token_payload(token)

    assert payload is not None
    assert payload.get("sub") == "admin"
    assert payload.get("pwd_chg_required") is True


def test_decode_access_token_still_returns_username() -> None:
    token = create_access_token("alice", must_change_password=False)

    assert decode_access_token(token) == "alice"
