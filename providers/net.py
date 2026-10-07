"""Hardened HTTP helper used by every provider and by the pricing module.

Compared with calling `requests.get(...).json()` directly it:
  * only talks HTTPS (plain http is accepted solely for a loopback node);
  * refuses redirects that leave HTTPS;
  * caps the *decoded* response size and the total download time, so a
    hostile/buggy endpoint (or a gzip bomb) cannot exhaust memory or hang;
  * turns every network/HTTP/JSON problem into `HttpError`, a
    `requests.RequestException` subclass whose message has already been
    stripped of API keys (requests exceptions embed the full URL, which for
    Etherscan/beaconcha.in contains the key) -- so existing
    `except requests.RequestException` handlers keep working.
TLS certificate verification is never disabled.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urljoin, urlsplit

import requests

from .safe import redact

#: (connect, read) timeouts in seconds.
DEFAULT_TIMEOUT = (6, 20)
DEFAULT_MAX_BYTES = 10_000_000
#: hard wall-clock limit for one download (a slow-drip response would
#: otherwise stay under the per-read timeout forever).
DEADLINE_SECONDS = 60

_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class HttpError(requests.RequestException):
    """Network/HTTP/format error; the message is already credential-free."""


def _https_or_loopback(url: str, allow_loopback_http: bool) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https" and host:
        return True
    return bool(allow_loopback_http and parts.scheme == "http" and host in _LOOPBACK)


def _read_capped(resp, max_bytes: int) -> bytes:
    deadline = time.monotonic() + DEADLINE_SECONDS
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise HttpError(f"API response too large (> {max_bytes // 1_000_000} MB)")
            if time.monotonic() > deadline:
                raise HttpError("API response too slow")
            chunks.append(chunk)
    except requests.RequestException as exc:
        if isinstance(exc, HttpError):
            raise
        raise HttpError(redact(str(exc))) from None
    return b"".join(chunks)


MAX_REDIRECTS = 3


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body=None,
    timeout=DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_loopback_http: bool = False,
):
    """Perform the request and return the decoded JSON document.

    Redirects are followed by hand so that the scheme of every hop is checked
    *before* the next request is sent: a hijacked endpoint answering
    `302 -> http://...` must never receive the request (and its API key) in
    clear text.
    """
    if not _https_or_loopback(url, allow_loopback_http):
        raise HttpError("Refusing non-HTTPS URL")
    resp = None
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            resp = requests.request(
                method, url, params=params, json=json_body, timeout=timeout,
                stream=True, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HttpError(redact(str(exc))) from None
        location = resp.headers.get("Location") if 300 <= resp.status_code < 400 else None
        if not location:
            break
        resp.close()
        url = urljoin(url, location)
        if not _https_or_loopback(url, allow_loopback_http):
            raise HttpError("Refusing redirect to a non-HTTPS URL")
        if resp.status_code in (301, 302, 303):
            method, json_body = "GET", None
        params = None  # the redirect target already carries its own query
    else:
        raise HttpError("Too many redirects")
    try:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise HttpError(redact(str(exc))) from None
        body = _read_capped(resp, max_bytes)
    finally:
        resp.close()
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise HttpError("Invalid JSON in API response") from None
