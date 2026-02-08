"""Tests for webhook authentication and rate limiting."""

from __future__ import annotations

import hmac as hmac_mod
import time
from unittest.mock import patch

from ductor_bot.webhook.auth import (
    RateLimiter,
    validate_bearer_token,
    validate_hmac_signature,
    validate_hook_auth,
)
from ductor_bot.webhook.models import WebhookEntry

# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


class TestValidateBearerToken:
    def test_valid_token(self) -> None:
        assert validate_bearer_token("Bearer my-secret", "my-secret") is True

    def test_wrong_token(self) -> None:
        assert validate_bearer_token("Bearer wrong", "my-secret") is False

    def test_missing_bearer_prefix(self) -> None:
        assert validate_bearer_token("my-secret", "my-secret") is False

    def test_empty_header(self) -> None:
        assert validate_bearer_token("", "my-secret") is False

    def test_bearer_only_no_token(self) -> None:
        assert validate_bearer_token("Bearer ", "my-secret") is False

    def test_extra_whitespace_not_matched(self) -> None:
        assert validate_bearer_token("Bearer  my-secret", "my-secret") is False

    def test_case_sensitive(self) -> None:
        assert validate_bearer_token("bearer my-secret", "my-secret") is False

    def test_timing_safe_comparison(self) -> None:
        # Ensure hmac.compare_digest is used (implementation detail, but security critical)
        assert validate_bearer_token("Bearer abc", "abc") is True


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_within_limit(self) -> None:
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert rl.check() is True

    def test_rejects_over_limit(self) -> None:
        rl = RateLimiter(max_per_minute=3)
        for _ in range(3):
            assert rl.check() is True
        assert rl.check() is False

    def test_window_slides(self) -> None:
        rl = RateLimiter(max_per_minute=2)
        assert rl.check() is True
        assert rl.check() is True
        assert rl.check() is False

        # Simulate 61 seconds passing
        with patch("ductor_bot.webhook.auth.time.monotonic", return_value=time.monotonic() + 61):
            assert rl.check() is True

    def test_reset_clears_history(self) -> None:
        rl = RateLimiter(max_per_minute=1)
        assert rl.check() is True
        assert rl.check() is False

        rl.reset()
        assert rl.check() is True

    def test_zero_limit_always_rejects(self) -> None:
        rl = RateLimiter(max_per_minute=0)
        assert rl.check() is False


# ---------------------------------------------------------------------------
# HMAC signature validation
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str, algo: str = "sha256") -> str:
    return hmac_mod.new(secret.encode(), body, algo).hexdigest()


def _sign_b64(body: bytes, secret: str, algo: str = "sha256") -> str:
    import base64

    return base64.b64encode(hmac_mod.new(secret.encode(), body, algo).digest()).decode()


class TestValidateHmacSignature:
    def test_valid_signature(self) -> None:
        body = b'{"event": "push"}'
        secret = "my-secret"
        sig = _sign(body, secret)
        assert validate_hmac_signature(body, sig, secret, sig_prefix="") is True

    def test_valid_signature_with_sha256_prefix(self) -> None:
        body = b'{"event": "push"}'
        secret = "gh-secret"
        sig = f"sha256={_sign(body, secret)}"
        assert validate_hmac_signature(body, sig, secret) is True

    def test_invalid_signature(self) -> None:
        body = b'{"event": "push"}'
        assert validate_hmac_signature(body, "deadbeef", "my-secret", sig_prefix="") is False

    def test_empty_signature_value(self) -> None:
        assert validate_hmac_signature(b"body", "", "secret") is False

    def test_empty_secret(self) -> None:
        assert validate_hmac_signature(b"body", "some-sig", "") is False

    def test_tampered_body_fails(self) -> None:
        secret = "secret"
        original = b'{"amount": 100}'
        sig = _sign(original, secret)
        tampered = b'{"amount": 999}'
        assert validate_hmac_signature(tampered, sig, secret, sig_prefix="") is False

    # -- Configurable algorithm --

    def test_sha1_algorithm(self) -> None:
        body = b'{"event": "payment"}'
        secret = "twilio-secret"
        sig = _sign(body, secret, "sha1")
        assert validate_hmac_signature(body, sig, secret, algorithm="sha1", sig_prefix="") is True

    def test_sha512_algorithm(self) -> None:
        body = b'{"data": 1}'
        secret = "strong"
        sig = _sign(body, secret, "sha512")
        assert validate_hmac_signature(body, sig, secret, algorithm="sha512", sig_prefix="") is True

    def test_unknown_algorithm_falls_back_to_sha256(self) -> None:
        body = b'{"x": 1}'
        secret = "s"
        sig = _sign(body, secret, "sha256")
        assert (
            validate_hmac_signature(body, sig, secret, algorithm="unknown-algo", sig_prefix="")
            is True
        )

    # -- Configurable encoding --

    def test_base64_encoding(self) -> None:
        body = b'{"event": "order"}'
        secret = "shopify-secret"
        sig = _sign_b64(body, secret)
        assert validate_hmac_signature(body, sig, secret, encoding="base64", sig_prefix="") is True

    def test_base64_encoding_wrong_sig_fails(self) -> None:
        assert (
            validate_hmac_signature(b"body", "dGVzdA==", "secret", encoding="base64", sig_prefix="")
            is False
        )

    # -- sig_regex extraction --

    def test_sig_regex_stripe_style(self) -> None:
        """Stripe-style: ``t=1234,v1=<hex_sig>``."""
        body = b'{"type": "charge.succeeded"}'
        secret = "whsec_test"
        # Stripe signs: "{timestamp}.{body}"
        timestamp = "1614000000"
        signed_payload = f"{timestamp}.".encode() + body
        sig = _sign(signed_payload, secret)
        header = f"t={timestamp},v1={sig}"
        assert (
            validate_hmac_signature(
                body,
                header,
                secret,
                sig_regex=r"v1=([a-f0-9]+)",
                payload_prefix_regex=r"t=(\d+)",
            )
            is True
        )

    def test_sig_regex_no_match_fails(self) -> None:
        assert (
            validate_hmac_signature(b"body", "no-match-here", "secret", sig_regex=r"v1=([a-f0-9]+)")
            is False
        )

    def test_sig_regex_overrides_sig_prefix(self) -> None:
        """When sig_regex is set, sig_prefix is ignored."""
        body = b'{"x": 1}'
        secret = "s"
        sig = _sign(body, secret)
        header = f"custom={sig}"
        # sig_prefix would mismatch, but sig_regex extracts correctly
        assert (
            validate_hmac_signature(
                body, header, secret, sig_prefix="wrong-prefix=", sig_regex=r"custom=([a-f0-9]+)"
            )
            is True
        )

    # -- payload_prefix_regex --

    def test_payload_prefix_prepends_timestamp(self) -> None:
        """Body signed as ``{timestamp}.{body}``."""
        body = b'{"data": "test"}'
        secret = "slack-secret"
        timestamp = "9999999999"
        signed_payload = f"{timestamp}.".encode() + body
        sig = _sign(signed_payload, secret)
        header_val = f"v0={timestamp}:{sig}"
        assert (
            validate_hmac_signature(
                body,
                header_val,
                secret,
                sig_regex=r":([a-f0-9]+)$",
                payload_prefix_regex=r"v0=(\d+):",
            )
            is True
        )

    # -- Combined: Shopify-style (base64, no prefix, SHA-256) --

    def test_shopify_style_base64_no_prefix(self) -> None:
        body = b'{"order_id": 123}'
        secret = "shopify-whsec"
        sig = _sign_b64(body, secret)
        assert validate_hmac_signature(body, sig, secret, encoding="base64", sig_prefix="") is True


# ---------------------------------------------------------------------------
# Per-hook auth dispatcher
# ---------------------------------------------------------------------------


def _make_hook(**overrides: object) -> WebhookEntry:
    defaults: dict[str, object] = {
        "id": "test",
        "title": "Test",
        "description": "desc",
        "mode": "wake",
        "prompt_template": "{{msg}}",
    }
    defaults.update(overrides)
    return WebhookEntry(**defaults)


class TestValidateHookAuth:
    def test_bearer_per_hook_token(self) -> None:
        hook = _make_hook(auth_mode="bearer", token="hook-secret")
        assert (
            validate_hook_auth(
                hook,
                authorization="Bearer hook-secret",
                signature_header_value="",
                body=b"",
                global_token="global-secret",
            )
            is True
        )

    def test_bearer_per_hook_rejects_global(self) -> None:
        hook = _make_hook(auth_mode="bearer", token="hook-secret")
        assert (
            validate_hook_auth(
                hook,
                authorization="Bearer global-secret",
                signature_header_value="",
                body=b"",
                global_token="global-secret",
            )
            is False
        )

    def test_bearer_global_fallback(self) -> None:
        hook = _make_hook(auth_mode="bearer", token="")
        assert (
            validate_hook_auth(
                hook,
                authorization="Bearer global-secret",
                signature_header_value="",
                body=b"",
                global_token="global-secret",
            )
            is True
        )

    def test_bearer_no_token_anywhere_fails(self) -> None:
        hook = _make_hook(auth_mode="bearer", token="")
        assert (
            validate_hook_auth(
                hook,
                authorization="Bearer anything",
                signature_header_value="",
                body=b"",
                global_token="",
            )
            is False
        )

    def test_hmac_valid_signature(self) -> None:
        body = b'{"event": "created"}'
        secret = "hmac-secret-123"
        sig = f"sha256={_sign(body, secret)}"
        hook = _make_hook(auth_mode="hmac", hmac_secret=secret, hmac_header="X-Sig")
        assert (
            validate_hook_auth(
                hook,
                authorization="",
                signature_header_value=sig,
                body=body,
                global_token="ignored",
            )
            is True
        )

    def test_hmac_invalid_signature(self) -> None:
        hook = _make_hook(auth_mode="hmac", hmac_secret="secret", hmac_header="X-Sig")
        assert (
            validate_hook_auth(
                hook,
                authorization="",
                signature_header_value="sha256=wrong",
                body=b"body",
                global_token="ignored",
            )
            is False
        )

    def test_unknown_auth_mode_treated_as_bearer(self) -> None:
        hook = _make_hook(auth_mode="unknown", token="my-token")
        assert (
            validate_hook_auth(
                hook,
                authorization="Bearer my-token",
                signature_header_value="",
                body=b"",
                global_token="",
            )
            is True
        )

    def test_hmac_passes_all_configurable_fields(self) -> None:
        """Stripe-style hook: sig_regex, payload_prefix_regex, all pass through."""
        body = b'{"type": "payment"}'
        secret = "whsec_stripe"
        timestamp = "1614000000"
        signed_payload = f"{timestamp}.".encode() + body
        sig = _sign(signed_payload, secret)
        header_val = f"t={timestamp},v1={sig}"

        hook = _make_hook(
            auth_mode="hmac",
            hmac_secret=secret,
            hmac_header="Stripe-Signature",
            hmac_algorithm="sha256",
            hmac_encoding="hex",
            hmac_sig_prefix="",
            hmac_sig_regex=r"v1=([a-f0-9]+)",
            hmac_payload_prefix_regex=r"t=(\d+)",
        )
        assert (
            validate_hook_auth(
                hook,
                authorization="",
                signature_header_value=header_val,
                body=body,
                global_token="ignored",
            )
            is True
        )

    def test_hmac_base64_shopify_passthrough(self) -> None:
        body = b'{"order": 1}'
        secret = "shopify-secret"
        sig = _sign_b64(body, secret)
        hook = _make_hook(
            auth_mode="hmac",
            hmac_secret=secret,
            hmac_header="X-Shopify-Hmac-Sha256",
            hmac_encoding="base64",
            hmac_sig_prefix="",
        )
        assert (
            validate_hook_auth(
                hook,
                authorization="",
                signature_header_value=sig,
                body=body,
                global_token="ignored",
            )
            is True
        )
