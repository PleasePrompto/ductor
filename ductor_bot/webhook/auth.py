"""Webhook authentication and rate limiting."""

from __future__ import annotations

import base64
import hmac
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.webhook.models import WebhookEntry

logger = logging.getLogger(__name__)

_HASH_ALGORITHMS: dict[str, str] = {
    "sha256": "sha256",
    "sha1": "sha1",
    "sha512": "sha512",
}


def validate_bearer_token(authorization: str, expected_token: str) -> bool:
    """Check ``Authorization: Bearer <token>`` header value.

    Uses constant-time comparison to prevent timing attacks.
    """
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        logger.warning("Auth failed: invalid token")
        return False
    valid = hmac.compare_digest(authorization[len(prefix) :], expected_token)
    if not valid:
        logger.warning("Auth failed: invalid token")
    return valid


def validate_hmac_signature(  # noqa: PLR0913
    body: bytes,
    signature_value: str,
    secret: str,
    *,
    algorithm: str = "sha256",
    encoding: str = "hex",
    sig_prefix: str = "sha256=",
    sig_regex: str = "",
    payload_prefix_regex: str = "",
) -> bool:
    """Validate an HMAC signature with fully configurable parameters.

    Args:
        body: Raw request body bytes.
        signature_value: The full header value containing the signature.
        secret: The shared HMAC secret.
        algorithm: Hash algorithm (``sha256``, ``sha1``, ``sha512``).
        encoding: Signature encoding (``hex`` or ``base64``).
        sig_prefix: Simple prefix to strip from *signature_value* before comparison.
            Ignored when *sig_regex* is set.
        sig_regex: Regex to extract the signature from *signature_value* (group 1).
            Overrides *sig_prefix* when non-empty.
        payload_prefix_regex: Regex applied to *signature_value*; group 1 is prepended
            to *body* with a ``"."`` separator before HMAC computation.
            Used by Stripe/Slack where the signed content is ``"{timestamp}.{body}"``.
    """
    if not signature_value or not secret:
        logger.warning("HMAC auth failed: missing signature or secret")
        return False

    # 1. Extract actual signature from header value
    if sig_regex:
        m = re.search(sig_regex, signature_value)
        if not m or not m.group(1):
            logger.warning("HMAC auth failed: sig_regex did not match")
            return False
        sig = m.group(1)
    elif sig_prefix:
        sig = signature_value.removeprefix(sig_prefix)
    else:
        sig = signature_value

    # 2. Construct payload to sign (optionally prepend extracted prefix)
    signed_payload = body
    if payload_prefix_regex:
        m = re.search(payload_prefix_regex, signature_value)
        if m and m.group(1):
            signed_payload = m.group(1).encode() + b"." + body

    # 3. Compute HMAC with configured algorithm
    algo = _HASH_ALGORITHMS.get(algorithm, "sha256")
    computed = hmac.new(secret.encode(), signed_payload, algo)

    # 4. Encode and compare
    if encoding == "base64":
        expected = base64.b64encode(computed.digest()).decode()
    else:
        expected = computed.hexdigest()

    valid = hmac.compare_digest(sig, expected)
    if not valid:
        logger.warning(
            "HMAC auth failed: signature mismatch (algo=%s, enc=%s)", algorithm, encoding
        )
    return valid


def validate_hook_auth(
    hook: WebhookEntry,
    *,
    authorization: str,
    signature_header_value: str,
    body: bytes,
    global_token: str,
) -> bool:
    """Per-hook authentication dispatcher.

    For ``auth_mode="hmac"``: validates signature using the hook's HMAC configuration.
    For ``auth_mode="bearer"`` (default): validates per-hook token with global fallback.
    """
    if hook.auth_mode == "hmac":
        return validate_hmac_signature(
            body,
            signature_header_value,
            hook.hmac_secret,
            algorithm=hook.hmac_algorithm,
            encoding=hook.hmac_encoding,
            sig_prefix=hook.hmac_sig_prefix,
            sig_regex=hook.hmac_sig_regex,
            payload_prefix_regex=hook.hmac_payload_prefix_regex,
        )

    # Bearer mode (default for unrecognized auth_mode too)
    expected = hook.token or global_token
    if not expected:
        logger.warning("Auth failed: no token configured for hook=%s", hook.id)
        return False
    return validate_bearer_token(authorization, expected)


class RateLimiter:
    """Simple sliding-window rate limiter using a deque of timestamps."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._timestamps: deque[float] = deque()

    def check(self) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > 60:
            self._timestamps.popleft()
        remaining = self._max - len(self._timestamps)
        logger.debug("Rate limit check remaining=%d", remaining)
        if remaining <= 0:
            logger.warning("Rate limit exceeded")
            return False
        self._timestamps.append(now)
        return True

    def reset(self) -> None:
        """Clear all recorded timestamps."""
        self._timestamps.clear()
