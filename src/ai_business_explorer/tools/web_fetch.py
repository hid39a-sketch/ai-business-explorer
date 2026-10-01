"""URL を指定して Web ページを取得する Tool（第2回仕様 13章・R-20）。

外部への読み取り専用（external_read）。取得した情報は Evidence 候補（原情報）として返す。

安全のための決まり（SSRF 対策）：
- HTTPS・ポート 443 だけ。URL にユーザー名・パスワードを含めない。IP アドレスを直接指定しない。
- 組織ごとのドメイン許可リスト・禁止リスト（設定ファイル）に従う。許可リストにないドメインは
  取得しない。
- 名前解決したすべての IP アドレスを検査し、1つでもグローバルでない（プライベート・ループバック・
  リンクローカル・クラウドのメタデータ・予約済みなど）なら取得しない。検査した IP にだけ接続する
  （名前解決をやり直さないので、検査と接続の間に宛先が変わる攻撃を防ぐ）。証明書はホスト名で検証する。
- リダイレクトは最大3回。リダイレクト先ごとに、上の検査をすべてやり直す。
- 応答は最大 5MB（Content-Length と実際に読んだ量の両方で確認）。圧縮は受け付けない。
- 1回の取得（リダイレクトを含む）は 15秒まで。
- robots.txt を尊重する（RFC 9309。取得できない 4xx は許可、5xx・接続できない場合は不許可）。
  User-Agent を明示する。利用規約の確認は、ドメインを許可リストに入れる前に人間が行う（運用手順）。
- HTML はテキストを取り出すだけで、スクリプトは実行しない（script・style などは捨てる）。
- 秘密情報（API キーなど）は使わない・送らない。Cookie・認証ヘッダーは送らず、応答ヘッダーも
  AI社員には渡さない。
"""

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import ClassVar, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from pydantic import BaseModel, Field

from ai_business_explorer.tools.base import (
    EvidenceCandidate,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSideEffect,
)

WEB_FETCH_TOOL = "web_fetch"
HTTPS_PORT = 443
ROBOTS_CACHE_EXECUTIONS = 64
# robots.txt の User-agent 行で照合する名前
ROBOTS_AGENT_TOKEN = "AIBusinessExplorer"  # noqa: S105  パスワードではない
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
# 取り出したテキストから除く要素（実行しない・本文ではない）
SKIPPED_ELEMENTS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object"})


class WebFetchError(ToolError):
    """取得を拒否した・失敗した（理由はメッセージに入れる。秘密情報は含めない）。"""


@dataclass(frozen=True)
class WebFetchLimits:
    """R-20 の上限。"""

    max_bytes: int = 5 * 1024 * 1024
    timeout_seconds: float = 15.0
    max_redirects: int = 3
    max_fetches_per_execution: int = 20


@dataclass(frozen=True)
class DomainPolicy:
    """組織ごとのドメインの許可・禁止（第2回は設定ファイル）。

    allowed_domains に "*" を入れると、禁止リスト以外のすべてのドメインを許可する。
    ドメインはそのドメインとサブドメインに一致する（example.com は www.example.com も含む）。
    """

    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()

    def allows(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if any(_matches(host, d) for d in self.blocked_domains):
            return False
        return "*" in self.allowed_domains or any(_matches(host, d) for d in self.allowed_domains)


def _matches(host: str, domain: str) -> bool:
    domain = domain.lower().strip().rstrip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


# ------------------------------------------------------------ 名前解決と接続（差し替え可能）


class Resolver(Protocol):
    def resolve(self, host: str, port: int) -> list[str]: ...


class SocketResolver:
    def resolve(self, host: str, port: int) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise WebFetchError(f"cannot resolve host '{host}'") from exc
        return sorted({str(info[4][0]) for info in infos})


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]  # 小文字のヘッダー名
    read: Callable[[int], bytes]
    close: Callable[[], None] = field(default=lambda: None)
    # 読み込みごとに残り時間を設定する（少しずつ送ってくるサーバーでも 15秒を超えない）
    set_timeout: Callable[[float], None] = field(default=lambda _: None)


class Connector(Protocol):
    def request(
        self, ip: str, host: str, path: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """検査済みの IP にだけ接続し、TLS はホスト名で検証する（名前解決をやり直さない）。"""

    def __init__(self, ip: str, host: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, HTTPS_PORT, timeout=timeout, context=context)
        self._ip = ip
        self._ssl_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, HTTPS_PORT), self.timeout)
        self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)


class HttpsConnector:
    def __init__(self) -> None:
        self._context = ssl.create_default_context()  # 証明書とホスト名を検証する

    def request(
        self, ip: str, host: str, path: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        conn = _PinnedHTTPSConnection(ip, host, timeout, self._context)
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            conn.close()
            raise WebFetchError(f"connection to '{host}' failed: {exc.__class__.__name__}") from exc

        def read(size: int) -> bytes:
            try:
                return response.read1(size)  # 届いた分だけ返す（期限をこまめに確認するため）
            except (OSError, http.client.HTTPException) as exc:
                raise WebFetchError(f"reading from '{host}' failed") from exc

        def set_timeout(seconds: float) -> None:
            if conn.sock is not None:
                conn.sock.settimeout(seconds)

        return HttpResponse(
            status=response.status,
            headers={k.lower(): v for k, v in response.getheaders()},
            read=read,
            close=conn.close,
            set_timeout=set_timeout,
        )


# ---------------------------------------------------------------------- 検査


def check_ip(address: str) -> None:
    """グローバルな（公開された）アドレス以外は拒否する。"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise WebFetchError(f"invalid IP address '{address}'") from exc
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 を埋め込んだ IPv6（::ffff:10.0.0.1 など）は、埋め込まれた IPv4 も検査する
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is not None:
            check_ip(str(embedded))
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise WebFetchError(f"address {address} is not a public address")


@dataclass(frozen=True)
class CheckedUrl:
    url: str
    host: str
    path: str  # パスとクエリ（# 以降は送らない）


def check_url(url: str, policy: DomainPolicy) -> CheckedUrl:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise WebFetchError("only https URLs can be fetched")
    if parts.username is not None or parts.password is not None:
        raise WebFetchError("URLs with credentials cannot be fetched")
    try:
        port = parts.port
    except ValueError as exc:
        raise WebFetchError("invalid port") from exc
    if port not in (None, HTTPS_PORT):
        raise WebFetchError("only port 443 can be used")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise WebFetchError("URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WebFetchError("IP address hosts cannot be fetched; use a domain name")
    if not policy.allows(host):
        raise WebFetchError(f"domain '{host}' is not allowed for this organization")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return CheckedUrl(url=parts._replace(fragment="").geturl(), host=host, path=path)


# ---------------------------------------------------------------------- HTML からテキストへ


class _TextExtractor(HTMLParser):
    """テキストだけを取り出す（スクリプトは実行しない・読まない）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_ELEMENTS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_ELEMENTS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str | None, str]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title_parts).split()) or None
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return title, "\n".join(line for line in lines if line)


def _charset(content_type: str) -> str:
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"'")
    return "utf-8"


# ---------------------------------------------------------------------- 取得


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    redirects: tuple[str, ...]


class WebFetcher:
    """安全な HTTPS GET。リダイレクトのたびに URL・ドメイン・IP を検査し直す。"""

    def __init__(
        self,
        user_agent: str,
        limits: WebFetchLimits | None = None,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.user_agent = user_agent
        self.limits = limits or WebFetchLimits()
        self.resolver = resolver or SocketResolver()
        self.connector = connector or HttpsConnector()
        self.clock = clock

    def fetch(self, url: str, policy: DomainPolicy, deadline: float | None = None) -> FetchedPage:
        deadline = deadline or self.clock() + self.limits.timeout_seconds
        checked = check_url(url, policy)
        redirects: list[str] = []
        while True:
            response = self._get(checked, deadline)
            location = response.headers.get("location")
            if response.status in (301, 302, 303, 307, 308) and location:
                response.close()
                if len(redirects) >= self.limits.max_redirects:
                    raise WebFetchError(f"too many redirects (max {self.limits.max_redirects})")
                checked = check_url(urljoin(checked.url, location), policy)
                redirects.append(checked.url)
                continue
            try:
                body = self._read_body(response, deadline)
            finally:
                response.close()
            return FetchedPage(
                requested_url=url,
                final_url=checked.url,
                status=response.status,
                content_type=response.headers.get("content-type", ""),
                body=body,
                redirects=tuple(redirects),
            )

    def checked_addresses(self, host: str) -> list[str]:
        """名前解決し、すべてのアドレスを検査する（1つでも公開されていなければ拒否）。"""
        addresses = self.resolver.resolve(host, HTTPS_PORT)
        if not addresses:
            raise WebFetchError(f"cannot resolve host '{host}'")
        for address in addresses:
            check_ip(address)
        return addresses

    def _get(self, checked: CheckedUrl, deadline: float) -> HttpResponse:
        addresses = self.checked_addresses(checked.host)
        remaining = self._remaining(deadline)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9",
            "Accept-Encoding": "identity",  # 圧縮爆弾を避ける
            "Connection": "close",
        }
        return self.connector.request(addresses[0], checked.host, checked.path, headers, remaining)

    def _read_body(self, response: HttpResponse, deadline: float) -> bytes:
        length = response.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > self.limits.max_bytes:
            raise WebFetchError(f"response is larger than {self.limits.max_bytes} bytes")
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in ("", "identity"):
            raise WebFetchError(f"compressed responses are not accepted ({encoding})")
        chunks: list[bytes] = []
        total = 0
        while True:
            response.set_timeout(self._remaining(deadline))
            chunk = response.read(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > self.limits.max_bytes:
                raise WebFetchError(f"response is larger than {self.limits.max_bytes} bytes")
            chunks.append(chunk)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise WebFetchError(f"fetch exceeded {self.limits.timeout_seconds} seconds")
        return remaining


class RobotsChecker:
    """robots.txt（RFC 9309）。ホストごとに1回だけ取得してキャッシュする。"""

    def __init__(self, fetcher: WebFetcher, agent_token: str) -> None:
        self.fetcher = fetcher
        self.agent_token = agent_token
        self._cache: dict[str, RobotFileParser | bool] = {}

    def allowed(self, checked: CheckedUrl, policy: DomainPolicy) -> bool:
        rules = self._cache.get(checked.host)
        if rules is None:
            rules = self._load(checked.host, policy)
            self._cache[checked.host] = rules
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.agent_token, checked.url)

    def _load(self, host: str, policy: DomainPolicy) -> RobotFileParser | bool:
        try:
            page = self.fetcher.fetch(f"https://{host}/robots.txt", policy)
        except WebFetchError:
            return False  # 取得できない（接続できない・大きすぎる等）なら不許可
        if 400 <= page.status < 500:
            return True  # robots.txt がない
        if page.status >= 300:
            return False  # サーバーエラーなどは不許可
        parser = RobotFileParser()
        parser.parse(page.body.decode(_charset(page.content_type), errors="replace").splitlines())
        return parser


# ---------------------------------------------------------------------- Tool


class WebFetchInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class WebFetchOutput(BaseModel):
    """AI社員に渡す結果。応答ヘッダー（Cookie など）は含めない。"""

    final_url: str
    status: int
    content_type: str
    title: str | None
    chars: int
    redirects: list[str]


class WebFetchTool(Tool):
    """URL を指定して Web ページを取得し、Evidence 候補（原情報）として返す。"""

    name: ClassVar[str] = WEB_FETCH_TOOL
    version: ClassVar[str] = "1"
    description: ClassVar[str] = (
        "Fetch a public HTTPS web page by URL and return its text as an evidence candidate."
    )
    side_effect: ClassVar[ToolSideEffect] = ToolSideEffect.EXTERNAL_READ
    input_model: ClassVar[type[BaseModel]] = WebFetchInput
    output_model: ClassVar[type[BaseModel]] = WebFetchOutput

    def __init__(
        self,
        fetcher: WebFetcher,
        robots_agent_token: str = ROBOTS_AGENT_TOKEN,
    ) -> None:
        self.fetcher = fetcher
        self.robots_agent_token = robots_agent_token
        self.max_calls_per_execution = fetcher.limits.max_fetches_per_execution
        # robots.txt のキャッシュは実行ごと（古いものから捨てる）
        self._robots: dict[object, RobotsChecker] = {}

    def execute(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        if not isinstance(tool_input, WebFetchInput):
            raise WebFetchError("invalid input")
        policy = DomainPolicy(
            allowed_domains=context.allowed_domains, blocked_domains=context.blocked_domains
        )
        checked = check_url(tool_input.url, policy)
        # robots.txt より前に宛先を検査する（公開されていない宛先には何も送らない）
        self.fetcher.checked_addresses(checked.host)
        robots = self._robots.get(context.execution_id)
        if robots is None:
            if len(self._robots) >= ROBOTS_CACHE_EXECUTIONS:
                self._robots.pop(next(iter(self._robots)))
            robots = RobotsChecker(self.fetcher, self.robots_agent_token)
            self._robots[context.execution_id] = robots
        if not robots.allowed(checked, policy):
            raise WebFetchError(f"robots.txt disallows fetching {checked.url}")
        page = self.fetcher.fetch(checked.url, policy)
        # リダイレクト先も robots.txt を確認する
        if page.final_url != checked.url and not robots.allowed(
            check_url(page.final_url, policy), policy
        ):
            raise WebFetchError(f"robots.txt disallows fetching {page.final_url}")
        if not 200 <= page.status < 300:
            raise WebFetchError(f"server returned status {page.status}")
        media_type = page.content_type.split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_CONTENT_TYPES:
            raise WebFetchError(f"unsupported content type '{media_type or 'unknown'}'")
        text = page.body.decode(_charset(page.content_type), errors="replace")
        title, body = html_to_text(text) if media_type != "text/plain" else (None, text.strip())
        retrieved_at = datetime.now(UTC)
        candidate = EvidenceCandidate(
            source_type="web",
            title=title or page.final_url,
            url=page.final_url,
            snapshot=body,
            retrieved_at=retrieved_at,
            metadata={
                "requested_url": page.requested_url,
                "final_url": page.final_url,
                "redirects": list(page.redirects),
                "http_status": page.status,
                "content_type": media_type,
                "bytes": len(page.body),
                "tool": WEB_FETCH_TOOL,
            },
        )
        return ToolResult(
            output=WebFetchOutput(
                final_url=page.final_url,
                status=page.status,
                content_type=media_type,
                title=title,
                chars=len(body),
                redirects=list(page.redirects),
            ).model_dump(),
            evidence_candidates=[candidate],
        )
