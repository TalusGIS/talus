"""Talus Field f4 (decision-talus-field-f4.md §4.2): the pure decision for a PAT on a native write
door. Three keys in the order a crew member can fix them; None = allowed."""
from app.auth import AUTH_CODE_HEADER, BASIC_CHALLENGE, pat_denied, rw_pat_gate


def test_device_token_on_its_own_layer_is_allowed():
    assert rw_pat_gate("read_write", ["tal_0000000a", "tal_0000000b"], "tal_0000000a") is None


def test_read_only_is_refused_first():
    msg = rw_pat_gate("read_only", ["tal_0000000a"], "tal_0000000a")
    assert msg and "read-only" in msg


def test_general_purpose_pat_is_refused_even_when_read_write():
    msg = rw_pat_gate("read_write", None, "tal_0000000a")
    assert msg and "device" in msg and "GeoServices" in msg


def test_another_packs_token_is_refused():
    msg = rw_pat_gate("read_write", ["tal_0000000b"], "tal_0000000a")
    assert msg and "another pack" in msg


def test_empty_allowlist_refuses_everything():
    assert rw_pat_gate("read_write", [], "tal_0000000a") is not None


def test_no_scope_at_all_is_refused():
    assert rw_pat_gate(None, ["tal_0000000a"], "tal_0000000a") is not None


# --- Talus Field f5 (decision-talus-field-f5.md §4.1): the reason header on the PAT path's 401 ---


def test_a_once_valid_token_learns_why_it_was_refused():
    for code in ("token_revoked", "token_expired"):
        exc = pat_denied(code)
        assert exc.status_code == 401
        assert exc.headers[AUTH_CODE_HEADER] == code
        assert exc.headers["WWW-Authenticate"] == BASIC_CHALLENGE  # the challenge is unchanged
        assert exc.detail == "Invalid or missing credentials"  # and so is the body


def test_an_unknown_credential_carries_no_reason():
    exc = pat_denied(None)
    assert exc.status_code == 401
    assert AUTH_CODE_HEADER not in exc.headers
    assert exc.headers["WWW-Authenticate"] == BASIC_CHALLENGE


def test_the_field_app_can_opt_out_of_the_browser_challenge():
    """WebKit turns a 401 + Basic challenge into a native sign-in sheet."""
    exc = pat_denied("token_revoked", challenge=False)
    assert exc.status_code == 401
    assert "WWW-Authenticate" not in exc.headers
    assert exc.headers[AUTH_CODE_HEADER] == "token_revoked"  # the reason still rides
