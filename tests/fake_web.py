"""Web 取得のテスト用の Fake（外部には接続しない）。記録した応答を返す。"""

from dataclasses import dataclass, field

from ai_business_explorer.tools.web_fetch import HttpResponse, WebFetcher, WebFetchLimits

PUBLIC_IP = "93.184.216.34"


@dataclass
class Page:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "text/html"})
    chunk_delay: float = 0.0  # 1回の読み込みで進める時間（タイムアウトのテスト）


@dataclass
class SentRequest:
    ip: str
    host: str
    path: str
    headers: dict[str, str]
    timeout: float


class FakeWeb:
    """名前解決と HTTPS 応答の Fake。"""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {}
        self.pages: dict[tuple[str, str], Page] = {}
        self.sent: list[SentRequest] = []
        self.now = 0.0
        self.timeouts: list[float] = []

    # Resolver
    def resolve(self, host: str, port: int) -> list[str]:
        return self.dns.get(host, [PUBLIC_IP])

    # Connector
    def request(
        self, ip: str, host: str, path: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        self.sent.append(SentRequest(ip, host, path, dict(headers), timeout))
        page = self.pages.get((host, path))
        if page is None:
            page = Page(status=404, body=b"not found", headers={"content-type": "text/plain"})
        remaining = [page.body[i : i + 64 * 1024] for i in range(0, len(page.body), 64 * 1024)]

        def read(_: int) -> bytes:
            self.now += page.chunk_delay
            return remaining.pop(0) if remaining else b""

        return HttpResponse(
            status=page.status,
            headers=dict(page.headers),
            read=read,
            set_timeout=self.timeouts.append,
        )

    def clock(self) -> float:
        return self.now

    def fetcher(self, limits: WebFetchLimits | None = None) -> WebFetcher:
        return WebFetcher("TestAgent/1.0", limits, resolver=self, connector=self, clock=self.clock)

    def html(self, host: str, path: str, html: str, **headers: str) -> None:
        self.pages[(host, path)] = Page(
            body=html.encode(), headers={"content-type": "text/html; charset=utf-8", **headers}
        )

    def redirect(self, host: str, path: str, location: str, status: int = 302) -> None:
        self.pages[(host, path)] = Page(status=status, headers={"location": location})

    def robots(self, host: str, text: str, status: int = 200) -> None:
        self.pages[(host, "/robots.txt")] = Page(
            status=status, body=text.encode(), headers={"content-type": "text/plain"}
        )
