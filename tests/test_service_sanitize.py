"""Tests for ``arango_cypher.service._sanitize_error`` and ``_translate_errors``.

``_sanitize_error`` is the single choke-point between python-arango's
error surface (which happily embeds URLs, credentials, host/port pairs,
and raw ``Authorization`` headers in its exception messages) and the
500-level ``detail`` strings the UI shows to end users. Any regression
here leaks credentials, so we pin the behaviour.

``_translate_errors`` is the context manager that wraps every AQL /
DB operation at the endpoint boundary; we cover its three invariants
here (sanitise + wrap as ``HTTPException``, preserve nested
``HTTPException`` status codes, pass-through on success).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from arango_cypher.service import _sanitize_error, _translate_errors


class TestSanitizeErrorURLs:
    def test_strips_https_url(self):
        msg = "connection refused to https://arangodb.example.com:8529/_db/prod"
        out = _sanitize_error(msg)
        assert "arangodb.example.com" not in out
        assert "<redacted-url>" in out

    def test_strips_http_url(self):
        msg = "failed to reach http://localhost:8001/nl-to-cypher"
        out = _sanitize_error(msg)
        assert "localhost:8001" not in out
        assert "<redacted-url>" in out

    def test_strips_multiple_urls(self):
        msg = "tried https://a.example/ then http://b.example/ both failed"
        out = _sanitize_error(msg)
        assert "a.example" not in out
        assert "b.example" not in out
        assert out.count("<redacted-url>") == 2

    def test_url_match_is_case_insensitive(self):
        msg = "Error at HTTPS://EXAMPLE.COM/path"
        out = _sanitize_error(msg)
        assert "EXAMPLE.COM" not in out


class TestSanitizeErrorHostPort:
    def test_strips_ipv4_with_port(self):
        msg = "could not connect to 10.0.0.5:8529"
        out = _sanitize_error(msg)
        assert "10.0.0.5" not in out
        assert "<redacted-host>" in out

    def test_strips_bare_ipv4(self):
        msg = "route lookup failed for 192.168.1.42"
        out = _sanitize_error(msg)
        assert "192.168.1.42" not in out
        assert "<redacted-host>" in out


class TestSanitizeErrorCredentials:
    @pytest.mark.parametrize(
        "needle",
        [
            "password=hunter2",
            "passwd: topsecret",
            "token=abc123xyz",
            "secret: shhh",
            "api_key=sk-live-123",
            "api-key: ak_live_456",
            "apikey=deadbeef",
            "authorization: Bearer eyJxxx.yyy.zzz",
            "Authorization=Basic dXNlcjpwYXNz",
            "PASSWORD=HUNTER2",  # all-caps
        ],
    )
    def test_strips_credential_like_patterns(self, needle: str):
        msg = f"HTTP 401 for request with {needle} in headers"
        out = _sanitize_error(msg)
        assert "<redacted-credential>" in out
        # The credential value itself must not leak — assert on the
        # trailing value (after ":" or "=") rather than the key name,
        # because the key itself ("password") is still present in the
        # replacement token's context and is not sensitive.
        value = needle.split(":", 1)[-1].split("=", 1)[-1].strip()
        assert value not in out, f"value {value!r} leaked: {out!r}"

    def test_stops_at_whitespace(self):
        # The regex greedily matches \S+, which means it stops at the
        # first whitespace character. This is the contract — we don't
        # want to eat the rest of the line after a credential.
        msg = "password=secret and also some context here"
        out = _sanitize_error(msg)
        assert "secret" not in out
        assert "context here" in out


class TestSanitizeErrorCombined:
    def test_all_three_redactions_in_one_message(self):
        msg = (
            "ClientConnectionError: could not reach "
            "https://arangodb.prod.corp:8529 "
            "(IP 10.0.1.50) with authorization=Bearer abc.def.ghi"
        )
        out = _sanitize_error(msg)
        # No raw host / URL / creds survive.
        assert "arangodb.prod.corp" not in out
        assert "10.0.1.50" not in out
        assert "abc.def.ghi" not in out
        # But the structural diagnostic ("ClientConnectionError", "could
        # not reach") does survive so the operator still has signal.
        assert "ClientConnectionError" in out
        assert "<redacted-url>" in out
        assert "<redacted-credential>" in out

    def test_empty_message_passes_through(self):
        assert _sanitize_error("") == ""

    def test_benign_message_passes_through(self):
        msg = "collection 'Foo' not found"
        assert _sanitize_error(msg) == msg


class TestTranslateErrorsContextManager:
    def test_success_path_is_transparent(self):
        # On a clean block, the context manager must not alter control
        # flow — i.e. variables assigned inside the `with` remain bound
        # after the block, and no exception escapes.
        with _translate_errors("unused"):
            x = 1 + 1
        assert x == 2

    def test_generic_exception_becomes_500_with_prefix(self):
        with pytest.raises(HTTPException) as exc:
            with _translate_errors("AQL execution failed"):
                raise RuntimeError("boom")
        assert exc.value.status_code == 500
        assert exc.value.detail.startswith("AQL execution failed:")
        assert "boom" in exc.value.detail
        assert exc.value.__cause__ is not None, "must chain via `from e`"

    def test_credentials_in_inner_exception_are_sanitised(self):
        with pytest.raises(HTTPException) as exc:
            with _translate_errors("AQL execution failed"):
                raise RuntimeError("401 Unauthorized (token=abc.def.ghi)")
        # The point of the wrapper: we must not leak the token.
        assert "abc.def.ghi" not in exc.value.detail
        assert "<redacted-credential>" in exc.value.detail

    def test_custom_status_code_is_honoured(self):
        with pytest.raises(HTTPException) as exc:
            with _translate_errors("translate failed", status_code=422):
                raise RuntimeError("bad input")
        assert exc.value.status_code == 422

    def test_nested_httpexception_is_preserved(self):
        # A caller upstream may have already raised a 400/404/422 with
        # a carefully composed detail. The wrapper must not mask that
        # into a generic 500.
        with pytest.raises(HTTPException) as exc:
            with _translate_errors("outer"):
                raise HTTPException(status_code=404, detail="not found")
        assert exc.value.status_code == 404
        assert exc.value.detail == "not found"

    def test_keyboard_interrupt_still_escapes(self):
        # We catch `Exception`, not `BaseException` — `KeyboardInterrupt`
        # and `SystemExit` must not be rewrapped into 500s or a CTRL-C
        # mid-query becomes a silent API error.
        with pytest.raises(KeyboardInterrupt):
            with _translate_errors("unused"):
                raise KeyboardInterrupt()
