"""Shared HTTP client factory for oikb.

Centralises httpx.Client creation and applies project-wide defaults,
including the OIKB_INSECURE_SSL environment variable that disables
TLS certificate verification for self-signed certificates.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Environment variable name used to disable TLS certificate verification.
_ENV_INSECURE_SSL = "OIKB_INSECURE_SSL"


def is_ssl_verification_disabled() -> bool:
    """Return True if TLS certificate verification is disabled via environment variable.

    <p>Reads the {@code OIKB_INSECURE_SSL} environment variable.
    Any value other than an empty string or {@code "0"} is treated as truthy.</p>

    @return True if SSL verification should be skipped, False otherwise.
    """
    raw = os.environ.get(_ENV_INSECURE_SSL, "").strip()
    return raw not in ("", "0", "false", "False", "FALSE", "no", "No", "NO")


def make_http_client(**kwargs: Any) -> httpx.Client:
    """Create an {@link httpx.Client} with project-wide defaults applied.

    <p>If the environment variable {@code OIKB_INSECURE_SSL} is set to a truthy
    value (anything other than empty string, {@code "0"}, {@code "false"}, or
    {@code "no"}), TLS certificate verification is disabled.
    A {@code WARNING} is logged in that case.</p>

    <p>Caller-supplied keyword arguments are merged over the defaults, so
    individual connectors can still customise {@code base_url}, {@code headers},
    {@code timeout}, etc.</p>

    @param kwargs: Additional keyword arguments forwarded to {@link httpx.Client}.
    @return A configured {@link httpx.Client} instance.
    """
    isInsecure = is_ssl_verification_disabled()

    if isInsecure:
        log.warning(
            "TLS certificate verification is DISABLED (OIKB_INSECURE_SSL is set). "
            "Do not use this setting in production environments."
        )

    # Apply verify=False only if not already explicitly set by the caller.
    if isInsecure and "verify" not in kwargs:
        kwargs["verify"] = False

    return httpx.Client(**kwargs)
