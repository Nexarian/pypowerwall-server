"""window.apiBaseUrl on the Power Flow page: coverage for every branch of the
retired forwarded-header algorithm.

Until 0.9.1 the "/" handler injected an *absolute* API base reconstructed
from X-Forwarded-Proto / X-Forwarded-Host / X-Forwarded-Port (added in
ec2b71c, "Add support for reverse proxy configuration with PROXY_BASE_URL").
It is now the path ``{PROXY_BASE_URL}/api`` and the browser resolves it
against the page URL.  These tests enumerate each decision the old code made
(scheme source, host source, port re-attachment and its standard-port rule,
proxy prefix) and show, per scenario, that

  * the injected value is exactly ``{proxy_base}/api`` with no scheme or host,
  * resolving it against the URL the browser used yields the API origin the
    old code was trying to name, and
  * the retired algorithm (re-implemented verbatim in ``_retired_absolute``)
    only agreed with that answer when the proxy hints were truthful.  Where it
    disagreed is exactly where the animation broke (port lost, http:// base on
    an https:// page behind Cloudflare, internal upstream hostname).
"""
import re
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app

_API_BASE_RE = re.compile(r'window\.apiBaseUrl = "([^"]*)"')


def _retired_absolute(headers, request_scheme, request_netloc, proxy_base):
    """Verbatim port of the algorithm removed in 0.9.1 (for comparison only)."""
    h = {k.lower(): v for k, v in headers.items()}
    scheme = h.get("x-forwarded-proto") or request_scheme
    host = h.get("x-forwarded-host") or request_netloc
    fwd_port = h.get("x-forwarded-port")
    if fwd_port and ":" not in host:
        standard = "443" if scheme == "https" else "80"
        if fwd_port != standard:
            host = f"{host}:{fwd_port}"
    return f"{scheme}://{host}{proxy_base}/api"


def _render(monkeypatch, *, backend_url, headers, proxy_base):
    """GET "/" as the proxy would deliver it to uvicorn; return the page."""
    monkeypatch.setattr(main_mod, "_proxy_base", proxy_base)
    client = TestClient(app, base_url=backend_url)
    resp = client.get("/", headers=headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    return resp.text


def _injected(html):
    m = _API_BASE_RE.search(html)
    assert m, "window.apiBaseUrl not injected"
    return m.group(1)


# One row per branch of the retired code.
#   page_url     what the browser actually loaded (iframe document URL)
#   backend_url  scheme://netloc as uvicorn sees the request (request.url)
#   headers      what the proxy forwarded
#   expected     the API base the browser must end up calling
#   retired_ok   whether the retired absolute algorithm reached `expected`
SCENARIOS = [
    pytest.param(
        # Branches: scheme fallback (no X-Forwarded-Proto), host fallback
        # (no X-Forwarded-Host), no X-Forwarded-Port, empty proxy base.
        "http://testserver/", "http://testserver", {}, "",
        "http://testserver/api", True,
        id="direct-http-no-proxy",
    ),
    pytest.param(
        # Scheme fallback with https request (uvicorn terminating TLS itself).
        "https://pw.lan/", "https://pw.lan", {}, "",
        "https://pw.lan/api", True,
        id="direct-https-no-proxy",
    ),
    pytest.param(
        # Host fallback where request.url.netloc already carries the port.
        "http://pw.lan:8675/", "http://pw.lan:8675", {}, "",
        "http://pw.lan:8675/api", True,
        id="direct-nonstandard-port-no-proxy",
    ),
    pytest.param(
        # All three headers truthful, X-Forwarded-Port == standard 80 for http
        # (branch: fwd_port present, no ":" in host, fwd_port == standard ->
        # not re-attached), proxy prefix set.
        "http://solar.lan/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "solar.lan",
         "X-Forwarded-Port": "80"}, "/pypowerwall",
        "http://solar.lan/pypowerwall/api", True,
        id="nginx-http-standard-port",
    ),
    pytest.param(
        # Same with https and standard 443 (the other half of the standard rule).
        "https://solar.lan/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "solar.lan",
         "X-Forwarded-Port": "443"}, "/pypowerwall",
        "https://solar.lan/pypowerwall/api", True,
        id="nginx-https-standard-port",
    ),
    pytest.param(
        # Re-attach branch: $host stripped the port, X-Forwarded-Port says 8090.
        "http://solar.lan:8090/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "solar.lan",
         "X-Forwarded-Port": "8090"}, "/pypowerwall",
        "http://solar.lan:8090/pypowerwall/api", True,
        id="nginx-reattach-nonstandard-port",
    ),
    pytest.param(
        # Re-attach branch with https: standard is 443, 8443 must be kept.
        "https://solar.lan:8443/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "solar.lan",
         "X-Forwarded-Port": "8443"}, "/pypowerwall",
        "https://solar.lan:8443/pypowerwall/api", True,
        id="nginx-reattach-nonstandard-https-port",
    ),
    pytest.param(
        # ":" already in host ($http_host kept the port): re-attach skipped.
        "http://solar.lan:8090/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "solar.lan:8090",
         "X-Forwarded-Port": "8090"}, "/pypowerwall",
        "http://solar.lan:8090/pypowerwall/api", True,
        id="nginx-host-already-has-port",
    ),
    pytest.param(
        # Hints wrong #1: $host without the port and no X-Forwarded-Port at
        # all (the configuration the re-attach code was written for, minus the
        # header it needs).  Retired result: http://solar.lan/pypowerwall/api,
        # i.e. the wrong port.
        "http://solar.lan:8090/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "solar.lan"},
        "/pypowerwall",
        "http://solar.lan:8090/pypowerwall/api", False,
        id="nginx-port-lost-no-forwarded-port",
    ),
    pytest.param(
        # Hints wrong #2 (the Cloudflare Zero Trust bug): TLS terminated
        # upstream, nginx overwrote X-Forwarded-Proto with $scheme = http.
        # Retired result: http://solar.pitstick.net/pypowerwall/api on an
        # https page -> every API call blocked as mixed content.
        "https://solar.pitstick.net/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "solar.pitstick.net",
         "X-Forwarded-Port": "80"}, "/pypowerwall",
        "https://solar.pitstick.net/pypowerwall/api", False,
        id="cloudflare-tls-terminated-upstream",
    ),
    pytest.param(
        # Hints wrong #3: proto forwarded but host not, so the host fallback
        # names the backend listener (request.url.netloc), unreachable from
        # the browser.
        "https://solar.lan/", "http://localhost:8675",
        {"X-Forwarded-Proto": "https"}, "",
        "https://solar.lan/api", False,
        id="proto-only-host-falls-back-to-backend",
    ),
    pytest.param(
        # Hints wrong #4: proxy forwards its *internal* upstream name.
        "https://solar.lan/pypowerwall/", "http://localhost:8675",
        {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "pypowerwall:8675"},
        "/pypowerwall",
        "https://solar.lan/pypowerwall/api", False,
        id="internal-forwarded-host",
    ),
]


@pytest.mark.parametrize(
    "page_url, backend_url, headers, proxy_base, expected, retired_ok", SCENARIOS
)
def test_api_base_resolves_to_page_origin(
    monkeypatch, page_url, backend_url, headers, proxy_base, expected, retired_ok
):
    html = _render(
        monkeypatch, backend_url=backend_url, headers=headers, proxy_base=proxy_base
    )
    injected = _injected(html)

    # 1. Always a path: no scheme, no host, just the configured prefix + /api.
    assert injected == f"{proxy_base}/api"
    assert "://" not in injected

    # 2. Resolved the way the browser does it, against the iframe's own URL,
    #    it names the API origin the browser can actually reach.
    assert urljoin(page_url, injected) == expected

    # 3. Document where the retired absolute URL agreed and where it did not.
    from urllib.parse import urlsplit

    backend = urlsplit(backend_url)
    retired = _retired_absolute(headers, backend.scheme, backend.netloc, proxy_base)
    assert (retired == expected) is retired_ok, (
        f"retired algorithm gave {retired!r}, expected {expected!r}"
    )


def test_no_absolute_origin_leaks_into_page(monkeypatch):
    """Whatever the proxy claims, the page must not contain an absolute API
    origin built from it (the exact string that used to break Cloudflare)."""
    html = _render(
        monkeypatch,
        backend_url="http://localhost:8675",
        headers={
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "solar.example.net",
            "X-Forwarded-Port": "80",
        },
        proxy_base="/pypowerwall",
    )
    assert "http://solar.example.net" not in html
    assert "localhost:8675" not in html
    assert _injected(html) == "/pypowerwall/api"


def test_neighbouring_replacements_unchanged(monkeypatch):
    """Dropping the absolute base must not disturb the other prefix-aware
    values injected next to it."""
    html = _render(
        monkeypatch, backend_url="http://localhost:8675", headers={},
        proxy_base="/pypowerwall",
    )
    assert 'window.appPrefix = "/pypowerwall/static/powerflow/"' in html
    assert 'var _BASE = "/pypowerwall"' in html            # fetch monkey-patch
    assert "'/pypowerwall/console'" in html                # top-window redirect
    assert "{API_BASE_URL}" not in html and "{PROXY_BASE" not in html

    html_root = _render(
        monkeypatch, backend_url="http://localhost:8675", headers={}, proxy_base=""
    )
    assert 'window.appPrefix = "/static/powerflow/"' in html_root
    assert _injected(html_root) == "/api"
    assert "var _BASE" not in html_root                    # no patch at root
