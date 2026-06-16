# -*- coding: utf-8 -*-
"""
Network Helper - bypass TUN mode for MinerU SDK domestic traffic.

Strategy: Application-level monkey-patch on httpx.Client._send_single_request.

How it works:
  TUN mode hijacks all traffic (DNS + TCP) via a virtual NIC, causing MinerU SDK
  httpx requests to be routed through the proxy. MinerU servers are domestic (China),
  so going through the proxy either fails or times out.

  This module:
    1. Resolves mineru.net real IPs via DNS-over-HTTPS (bypass TUN DNS hijack)
    2. Creates an independent direct-connect httpx.HTTPTransport (no proxy)
    3. Monkey-patches httpx.Client._send_single_request:
       - MinerU request -> rewrite URL to IP + SNI -> use direct transport
       - Other requests -> unchanged, go through proxy transport

  Key point: does NOT clear proxy env vars, so Semantic Scholar / arXiv APIs work fine.

Usage:
    import network_helper
    network_helper.install()       # call once at startup

    # MinerU SDK will automatically use direct connect
    client = MinerU(token)
    result = client.extract(pdf_path, ...)
"""

import logging
import ssl
import sys
from contextlib import contextmanager

import httpx

logger = logging.getLogger(__name__)

# -- MinerU domain list --
# API domains (exact match)
MINERU_API_DOMAINS = {
    "mineru.net",
    "openxlab.org.cn",
    "openxlab.com",
}

# CDN/subdomain prefixes (these domains will be auto-detected and DoH-resolved independently)
MINERU_SUBDOMAIN_PREFIXES = {
    "cdn-mineru",    # cdn-mineru.openxlab.org.cn - result ZIP download
}

# Merged: all domains that need direct connect (API + dynamically discovered CDN)
_all_mineru_domains: set[str] = set(MINERU_API_DOMAINS)

# Known IPs (dynamically refreshed via DoH, key is full domain)
_mineru_ips: dict[str, list[str]] = {}

# Patch state
_installed = False
_original_client_init = None

# Direct transport (bypasses proxy, connects to real IP)
_direct_transport: httpx.HTTPTransport | None = None

# Custom SSL context
_ssl_ctx: ssl.SSLContext | None = None


def _get_ssl_ctx() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_REQUIRED
    return _ssl_ctx


def _get_direct_transport() -> httpx.HTTPTransport:
    """Get or create direct transport (does not go through proxy)."""
    global _direct_transport
    if _direct_transport is None:
        _direct_transport = httpx.HTTPTransport(
            verify=_get_ssl_ctx(),
            retries=2,
        )
    return _direct_transport


class _MineruRoutingTransport:
    """Routes MinerU requests to direct IP transport, all others to proxy transport.

    Uses httpx's stable transport interface (handle_request / close) instead of
    monkey-patching internal methods like _send_single_request.
    """

    def __init__(self, proxy_transport):
        self._proxy = proxy_transport
        self._direct = _get_direct_transport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        rewritten = _rewrite_request(request)
        if rewritten is not None:
            logger.info(
                '[route] %s -> %s | %s',
                request.url.host, rewritten.url.host, request.url.path[:80]
            )
            return self._direct.handle_request(rewritten)
        return self._proxy.handle_request(request)

    def close(self):
        self._direct.close()
        self._proxy.close()

    def __enter__(self):
        if hasattr(self._proxy, '__enter__'):
            self._proxy.__enter__()
        if hasattr(self._direct, '__enter__'):
            self._direct.__enter__()
        return self

    def __exit__(self, *args):
        if hasattr(self._proxy, '__exit__'):
            self._proxy.__exit__(*args)
        if hasattr(self._direct, '__exit__'):
            self._direct.__exit__(*args)


# -- DoH resolution --

def resolve_mineru_ips(doh_url: str = "https://doh.pub/dns-query") -> dict[str, list[str]]:
    """
    Resolve MinerU domain real IPs via DNS-over-HTTPS.
    doh.pub itself is overseas and needs proxy - uses regular httpx (through proxy).

    Each domain is resolved independently (including CDN subdomains), no parent domain wildcard.
    """
    global _mineru_ips

    # Resolve all known domains + common CDN subdomains at startup
    domains_to_resolve = list(_all_mineru_domains)
    # Preload CDN subdomains
    for prefix in MINERU_SUBDOMAIN_PREFIXES:
        for parent in ["openxlab.org.cn", "openxlab.com", "mineru.net"]:
            cdn_domain = f"{prefix}.{parent}"
            if cdn_domain not in _all_mineru_domains:
                domains_to_resolve.append(cdn_domain)
                _all_mineru_domains.add(cdn_domain)

    new_ips: dict[str, list[str]] = {}
    try:
        client = httpx.Client(timeout=10)
        try:
            for domain in domains_to_resolve:
                try:
                    resp = client.get(
                        doh_url,
                        params={"name": domain, "type": "A"},
                        headers={"Accept": "application/dns-json"},
                    )
                    ips = []
                    for a in resp.json().get("Answer", []):
                        if a.get("type") == 1:
                            ip = a["data"]
                            if not ip.startswith("198.18."):
                                ips.append(ip)
                    if ips:
                        new_ips[domain] = ips
                except Exception as e:
                    logger.warning(f"DoH resolve {domain} failed: {e}")
        finally:
            client.close()
    except Exception as e:
        logger.warning(f"DoH resolve error: {e}")

    if new_ips:
        _mineru_ips.update(new_ips)
        logger.info(f"DoH resolved: {new_ips}")
    else:
        logger.warning("DoH got no real IPs, keeping cache")

    return _mineru_ips


def get_ips_for_domain(domain: str) -> list[str]:
    if domain not in _mineru_ips:
        resolve_mineru_ips()
    return _mineru_ips.get(domain, [])


# -- Request rewriting --

def _is_mineru_host(hostname: str) -> str | None:
    """Check if hostname is a MinerU-related domain. Returns the SNI domain name if matched."""
    # 1. Exact match on API domains
    if hostname in _all_mineru_domains:
        return hostname

    # 2. Subdomain of known API domains -> use full hostname as SNI
    for d in MINERU_API_DOMAINS:
        if hostname.endswith(f".{d}"):
            _all_mineru_domains.add(hostname)
            logger.info(f"Discovered MinerU subdomain: {hostname} (based on {d})")
            return hostname

    # 3. CDN prefix pattern match (fallback)
    for prefix in MINERU_SUBDOMAIN_PREFIXES:
        if hostname.startswith(prefix + "."):
            _all_mineru_domains.add(hostname)
            logger.info(f"Discovered MinerU CDN subdomain: {hostname}")
            return hostname

    return None


def _rewrite_request(request: httpx.Request) -> httpx.Request | None:
    """MinerU request: rewrite URL domain->IP + sni_hostname. Returns None for non-MinerU."""
    hostname = request.url.host
    matched = _is_mineru_host(hostname)
    if matched is None:
        return None

    # Resolve IP for the full hostname (each subdomain DoH-resolved independently)
    ips = get_ips_for_domain(matched)
    if not ips:
        logger.warning(f"No real IP for {matched}")
        return None

    ip = ips[hash(request.url.path) % len(ips)]
    new_url = request.url.copy_with(host=ip)

    extensions = dict(request.extensions)
    extensions["sni_hostname"] = matched

    new_request = httpx.Request(
        method=request.method,
        url=new_url,
        headers=request.headers,
        content=request.content,
        extensions=extensions,
    )
    logger.debug(f"URL rewrite: {request.url} -> {new_url} (SNI={matched})")
    return new_request


# -- Transport injection (stable httpx public API) --

_original_client_init = None


def install():
    """Inject MinerU routing transport at httpx.Client constructor level.

    Uses httpx's stable transport interface (handle_request/close) instead of
    monkey-patching _send_single_request or other internal methods.

    DoH resolution is deferred until first real MinerU IP is needed.
    """
    global _installed, _original_client_init

    if _installed:
        return

    _original_client_init = httpx.Client.__init__

    def _patched_init(self, *args, **kwargs):
        _original_client_init(self, *args, **kwargs)
        self._transport = _MineruRoutingTransport(self._transport)

    httpx.Client.__init__ = _patched_init
    _installed = True

    logger.info("MinerU direct-connect patch installed (transport-level, DoH lazy-loaded)")


def uninstall():
    """Uninstall patch."""
    global _installed, _original_client_init, _direct_transport

    if not _installed:
        return

    if _original_client_init is not None:
        httpx.Client.__init__ = _original_client_init
        _original_client_init = None

    if _direct_transport is not None:
        _direct_transport.close()
        _direct_transport = None

    _installed = False
    logger.info("MinerU direct-connect patch uninstalled")


# -- Context manager (backward compat) --

@contextmanager
def mineru_bypass():
    """Backward compatibility wrapper. If install() is already global, this is a no-op."""
    was_installed = _installed
    if not was_installed:
        install()
    try:
        yield
    finally:
        if not was_installed and _installed:
            uninstall()


# -- Diagnostics --

def diagnose() -> dict:
    import os as _os
    info: dict = {
        "patch_installed": _installed,
        "cached_ips": dict(_mineru_ips),
        "env_proxy": {
            k: _os.environ.get(k)
            for k in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]
            if _os.environ.get(k)
        },
    }
    try:
        r = httpx.get(
            "https://doh.pub/dns-query",
            params={"name": "mineru.net", "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=5,
        )
        info["doh_available"] = r.status_code == 200
        all_ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        info["doh_ips"] = all_ips
        info["doh_has_fake_ip"] = any(ip.startswith("198.18.") for ip in all_ips)
    except Exception as e:
        info["doh_available"] = False
        info["doh_error"] = str(e)
    return info


# -- CLI --

if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("=" * 60)
        print("MinerU Direct-Connect Diagnostics")
        print("=" * 60)

        info = diagnose()
        print(json.dumps(info, indent=2, ensure_ascii=False))

        install()

        # 1. Test MinerU direct connect
        print("\n[1] Testing MinerU API direct connect...")
        try:
            client = httpx.Client(timeout=15)
            resp = client.get("https://mineru.net/api/v4/open-api/health")
            print(f"  Status: {resp.status_code}")
            print(f"  Body: {resp.text[:200]}")
            client.close()
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {e}")

        # 2. Test proxy still works (Semantic Scholar)
        print("\n[2] Testing Semantic Scholar API (through proxy)...")
        try:
            client = httpx.Client(timeout=15)
            resp = client.get("https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1")
            print(f"  Status: {resp.status_code}")
            print(f"  Body: {resp.text[:200]}")
            client.close()
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {e}")

        uninstall()
    else:
        print("Usage: python network_helper.py test")
