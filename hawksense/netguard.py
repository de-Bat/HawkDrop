"""Safe outbound fetching for addresses that come from users or web pages.

HawkSense fetches pages it's told about: product links, extra spec pages,
rules sources and the rules feed. Without a guard, anyone who can reach the API
could make the server fetch ``http://192.168.1.1/``, the cloud metadata service
(``169.254.169.254``) or ``file:///etc/passwd``. So those fetches:

* allow only ``http`` and ``https``,
* refuse hosts that resolve to private, loopback, link-local, carrier-grade NAT,
  multicast or reserved addresses, checked before connecting, again on every
  redirect, and against the address actually connected to (so a DNS answer that
  changes between the check and the connection doesn't get through),
* stop reading after ``MAX_BYTES`` (also after decompression).

When an HTTP(S) proxy is configured, the proxy makes the connection and does its
own DNS, so only the up-front check applies (and a name that can't be resolved
locally is left to the proxy).

Set ``HAWKSENSE_ALLOW_PRIVATE_FETCH=1`` to allow local addresses, e.g. to track
a shop on your own network.

Notification channels (webhook, a self-hosted ntfy) are configured by you and may
point at your own network on purpose, so they don't go through this guard.
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import os
import socket
import urllib.request
import zlib
from urllib.parse import urlparse

MAX_BYTES = 8 * 1024 * 1024  # a product page is far smaller; this stops runaway downloads


class BlockedAddress(Exception):
    """The address isn't allowed (not http/https, or not on the public internet)."""


def private_allowed() -> bool:
    return os.environ.get("HAWKSENSE_ALLOW_PRIVATE_FETCH", "").lower() in ("1", "true", "yes", "on")


def _public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _proxied(scheme: str) -> bool:
    return bool(urllib.request.getproxies().get(scheme))


def check_url(url: str) -> None:
    """Raise BlockedAddress unless ``url`` is http(s) on a public address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedAddress(f"only http and https links can be fetched, not {parsed.scheme or 'this'}:")
    host = parsed.hostname
    if not host:
        raise BlockedAddress("the link has no host name")
    if private_allowed():
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        try:  # a literal address never needs DNS
            if not _public(host.strip("[]")):
                raise BlockedAddress(f"{host} is not a public internet address") from None
            return
        except ValueError:
            pass
        if _proxied(parsed.scheme):
            return  # the proxy resolves it
        raise BlockedAddress(f"can't find the address of {host}") from None
    for info in infos:
        if not _public(info[4][0]):
            raise BlockedAddress(f"{host} points to a private or local address ({info[4][0]}) - "
                                 "set HAWKSENSE_ALLOW_PRIVATE_FETCH=1 to allow it")


def _check_peer(sock) -> None:
    if private_allowed():
        return
    peer = sock.getpeername()[0]
    if not _public(peer):
        sock.close()
        raise BlockedAddress(f"connected to a private or local address ({peer})")


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(scheme: str) -> urllib.request.OpenerDirector:
    handlers = [_RedirectHandler()]
    if not _proxied(scheme):  # through a proxy the peer is the proxy itself
        handlers += [_HTTPHandler(), _HTTPSHandler()]
    return urllib.request.build_opener(*handlers)


def _read_limited(resp) -> bytes:
    raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise BlockedAddress(f"the page is larger than {MAX_BYTES // (1024 * 1024)} MB")
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding in ("gzip", "deflate"):
        d = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else -zlib.MAX_WBITS)
        try:
            raw = d.decompress(raw, MAX_BYTES + 1)
        except zlib.error as exc:
            raise gzip.BadGzipFile(str(exc)) from None
        if len(raw) > MAX_BYTES or d.unconsumed_tail:
            raise BlockedAddress(f"the page is larger than {MAX_BYTES // (1024 * 1024)} MB unpacked")
    return raw


def fetch(url: str, headers: dict | None = None, timeout: float = 20.0) -> tuple[bytes, str]:
    """GET ``url`` safely; returns (body, charset). Raises BlockedAddress or urllib/OS errors."""
    check_url(url)
    req = urllib.request.Request(url, headers=headers or {})
    with _opener(urlparse(url).scheme).open(req, timeout=timeout) as resp:
        return _read_limited(resp), resp.headers.get_content_charset() or "utf-8"
