"""Web 取得 Tool の安全装置（第2回仕様 13章・R-20）。外部には接続しない。"""

import socket
from typing import Any
from uuid import uuid4

import pytest

from ai_business_explorer.tools.base import ToolContext, ToolSideEffect
from ai_business_explorer.tools.web_fetch import (
    DomainPolicy,
    HttpsConnector,
    WebFetchError,
    WebFetchLimits,
    WebFetchTool,
    check_ip,
    check_url,
    html_to_text,
)
from tests.fake_web import FakeWeb, Page

ANY = DomainPolicy(allowed_domains=("*",))
EXAMPLE = DomainPolicy(allowed_domains=("example.com",))


def _context(allowed: tuple[str, ...] = ("*",), blocked: tuple[str, ...] = ()) -> ToolContext:
    return ToolContext(
        execution_id=uuid4(),
        exploration_id=uuid4(),
        organization_id=uuid4(),
        allowed_domains=allowed,
        blocked_domains=blocked,
    )


# ---------------------------------------------------------------------- URL の検査


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/", "only https"),
        ("ftp://example.com/", "only https"),
        ("https://example.com:8443/", "port 443"),
        ("https://user:pass@example.com/", "credentials"),
        ("https://203.0.113.5/", "IP address"),
        ("https://[2606:4700::1111]/", "IP address"),
        ("https:///path", "no host"),
        ("https://other.org/", "not allowed"),
    ],
)
def test_urls_outside_the_policy_are_rejected(url: str, reason: str) -> None:
    with pytest.raises(WebFetchError, match=reason):
        check_url(url, EXAMPLE)


def test_allowed_urls() -> None:
    checked = check_url("https://WWW.Example.com:443/a/b?x=1#frag", EXAMPLE)
    assert (checked.host, checked.path, checked.url) == (
        "www.example.com",
        "/a/b?x=1",
        "https://WWW.Example.com:443/a/b?x=1",
    )
    # 似た名前の別ドメインは一致しない
    with pytest.raises(WebFetchError):
        check_url("https://badexample.com/", EXAMPLE)
    # 禁止リストは "*" より優先する
    blocked = DomainPolicy(allowed_domains=("*",), blocked_domains=("internal.example.com",))
    with pytest.raises(WebFetchError, match="not allowed"):
        check_url("https://a.internal.example.com/", blocked)
    # 許可リストが空なら何も取得しない（安全側の既定）
    with pytest.raises(WebFetchError, match="not allowed"):
        check_url("https://example.com/", DomainPolicy())


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # クラウドのメタデータ
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104  検査の対象として列挙しているだけ
        "224.0.0.1",
        "192.0.2.10",  # 文書用
        "::1",
        "fe80::1",
        "fd00::1",
        "fd00:ec2::254",  # AWS の IPv6 メタデータ
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "2002:a00:1::1",  # 6to4（10.0.0.1 を埋め込み）
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    with pytest.raises(WebFetchError):
        check_ip(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    check_ip(address)


# ---------------------------------------------------------------------- 取得


def test_fetch_connects_only_to_checked_address_with_safe_headers() -> None:
    web = FakeWeb()
    web.html("example.com", "/r", "<p>hi</p>")
    page = web.fetcher().fetch("https://example.com/r", ANY)
    assert page.status == 200
    [sent] = web.sent
    assert (sent.ip, sent.host, sent.path) == ("93.184.216.34", "example.com", "/r")
    assert sent.headers["User-Agent"] == "TestAgent/1.0"
    assert sent.headers["Accept-Encoding"] == "identity"
    assert not {"Cookie", "Authorization"} & set(sent.headers)
    assert 0 < sent.timeout <= 15


def test_any_private_address_in_dns_rejects_the_host() -> None:
    web = FakeWeb()
    web.dns["example.com"] = ["93.184.216.34", "10.0.0.5"]
    with pytest.raises(WebFetchError, match="not a public address"):
        web.fetcher().fetch("https://example.com/", ANY)
    assert web.sent == []


def test_every_redirect_is_checked_again() -> None:
    web = FakeWeb()
    web.redirect("example.com", "/a", "https://metadata.example.net/latest")
    web.dns["metadata.example.net"] = ["169.254.169.254"]
    with pytest.raises(WebFetchError, match="not a public address"):
        web.fetcher().fetch("https://example.com/a", ANY)
    web.redirect("example.com", "/b", "http://example.com/plain")
    with pytest.raises(WebFetchError, match="only https"):
        web.fetcher().fetch("https://example.com/b", ANY)
    web.redirect("example.com", "/c", "https://other.org/")
    with pytest.raises(WebFetchError, match="not allowed"):
        web.fetcher().fetch("https://example.com/c", EXAMPLE)
    web.redirect("example.com", "/d", "https://10.0.0.1/")
    with pytest.raises(WebFetchError, match="IP address"):
        web.fetcher().fetch("https://example.com/d", ANY)


def test_at_most_three_redirects() -> None:
    web = FakeWeb()
    for i in range(4):
        web.redirect("example.com", f"/{i}", f"/{i + 1}")
    web.html("example.com", "/4", "<p>done</p>")
    with pytest.raises(WebFetchError, match="too many redirects"):
        web.fetcher().fetch("https://example.com/0", ANY)
    page = web.fetcher().fetch("https://example.com/1", ANY)
    assert page.final_url == "https://example.com/4"
    assert len(page.redirects) == 3


def test_response_size_is_limited() -> None:
    web = FakeWeb()
    limit = WebFetchLimits()
    assert limit.max_bytes == 5 * 1024 * 1024
    web.pages[("example.com", "/declared")] = Page(
        body=b"x", headers={"content-type": "text/html", "content-length": str(6 * 1024 * 1024)}
    )
    with pytest.raises(WebFetchError, match="larger than"):
        web.fetcher().fetch("https://example.com/declared", ANY)
    web.pages[("example.com", "/streamed")] = Page(
        body=b"x" * (5 * 1024 * 1024 + 1), headers={"content-type": "text/html"}
    )
    with pytest.raises(WebFetchError, match="larger than"):
        web.fetcher().fetch("https://example.com/streamed", ANY)
    web.pages[("example.com", "/gzip")] = Page(
        body=b"x", headers={"content-type": "text/html", "content-encoding": "gzip"}
    )
    with pytest.raises(WebFetchError, match="compressed"):
        web.fetcher().fetch("https://example.com/gzip", ANY)


def test_fetch_times_out_after_fifteen_seconds() -> None:
    web = FakeWeb()
    assert WebFetchLimits().timeout_seconds == 15
    web.pages[("example.com", "/slow")] = Page(
        body=b"x" * (3 * 64 * 1024), headers={"content-type": "text/html"}, chunk_delay=6
    )
    with pytest.raises(WebFetchError, match="exceeded 15"):
        web.fetcher().fetch("https://example.com/slow", ANY)
    # 読み込みのたびに、残り時間をソケットの待ち時間にする
    assert web.timeouts == [15, 9, 3]


def test_https_connector_connects_to_the_checked_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """名前解決をやり直さず、検査した IP に接続する（DNS rebinding 対策）。"""
    seen: list[Any] = []

    def fake_create_connection(address: Any, timeout: Any = None, *args: Any) -> Any:
        seen.append(address)
        raise OSError("blocked in test")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    with pytest.raises(WebFetchError, match=r"connection to 'example\.com' failed"):
        HttpsConnector().request("93.184.216.34", "example.com", "/", {}, 1.0)
    assert seen == [("93.184.216.34", 443)]


# ---------------------------------------------------------------------- robots.txt と本文


def _tool(web: FakeWeb) -> WebFetchTool:
    return WebFetchTool(web.fetcher())


def test_robots_txt_is_respected() -> None:
    web = FakeWeb()
    web.robots("example.com", "User-agent: *\nDisallow: /private\n")
    web.html("example.com", "/private/a", "<p>secret</p>")
    web.html("example.com", "/public", "<p>ok</p>")
    tool = _tool(web)
    context = _context()
    with pytest.raises(WebFetchError, match=r"robots\.txt disallows"):
        tool.execute(tool.input_model(url="https://example.com/private/a"), context)
    tool.execute(tool.input_model(url="https://example.com/public"), context)
    # robots.txt は実行ごとにホストあたり1回だけ取得する
    assert [s.path for s in web.sent].count("/robots.txt") == 1


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (403, True), (500, False)])
def test_robots_txt_unavailable(status: int, allowed: bool) -> None:
    web = FakeWeb()
    web.robots("example.com", "", status=status)
    web.html("example.com", "/", "<p>ok</p>")
    tool = _tool(web)
    if allowed:
        tool.execute(tool.input_model(url="https://example.com/"), _context())
    else:
        with pytest.raises(WebFetchError, match=r"robots\.txt"):
            tool.execute(tool.input_model(url="https://example.com/"), _context())


def test_html_is_reduced_to_text_without_running_scripts() -> None:
    title, text = html_to_text(
        "<html><head><title> Market  report </title><script>alert(1)</script>"
        "<style>p{color:red}</style></head><body><h1>市場</h1><p>拡大している</p>"
        "<noscript>JS off</noscript><p>2025年</p></body></html>"
    )
    assert title == "Market report"
    assert text == "市場\n拡大している\n2025年"


def test_tool_returns_raw_page_as_a_candidate() -> None:
    web = FakeWeb()
    web.html(
        "example.com",
        "/report",
        "<title>Report</title><p>本文</p><script>x()</script>",
        **{"set-cookie": "session=secret"},
    )
    tool = _tool(web)
    assert tool.side_effect is ToolSideEffect.EXTERNAL_READ
    assert tool.max_calls_per_execution == 20
    result = tool.execute(tool.input_model(url="https://example.com/report"), _context())
    assert result.output == {
        "final_url": "https://example.com/report",
        "status": 200,
        "content_type": "text/html",
        "title": "Report",
        "chars": 2,
        "redirects": [],
    }
    # 応答ヘッダー（Cookie など）は AI社員に渡さない
    assert "secret" not in str(result.model_dump())
    [candidate] = result.evidence_candidates
    assert (candidate.source_type, candidate.title, candidate.url) == (
        "web",
        "Report",
        "https://example.com/report",
    )
    assert candidate.snapshot == "本文"
    assert candidate.retrieved_at is not None
    assert candidate.metadata["tool"] == "web_fetch"


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (Page(status=500, body=b"err", headers={"content-type": "text/html"}), "status 500"),
        (Page(body=b"%PDF", headers={"content-type": "application/pdf"}), "content type"),
    ],
)
def test_unusable_responses_are_errors(page: Page, message: str) -> None:
    web = FakeWeb()
    web.pages[("example.com", "/x")] = page
    tool = _tool(web)
    with pytest.raises(WebFetchError, match=message):
        tool.execute(tool.input_model(url="https://example.com/x"), _context())


def test_domain_policy_comes_from_the_context() -> None:
    web = FakeWeb()
    tool = _tool(web)
    with pytest.raises(WebFetchError, match="not allowed"):
        tool.execute(tool.input_model(url="https://example.com/"), _context(allowed=()))
    assert web.sent == []
