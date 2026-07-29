"""Small, SSRF-resistant public website researcher for company context."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

MAX_PAGE_BYTES = 256_000
MAX_MODEL_CHARS = 18_000
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
ABOUT_PATH_HINTS = ("about", "company", "who-we-are", "our-story")


class WebsiteResearchError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ResearchResult:
    website_url: str
    source_urls: tuple[str, ...]
    summary: str
    facts: dict[str, object]
    content_sha256: str
    model_context: str


@dataclass(frozen=True, slots=True)
class _Page:
    url: str
    title: str
    description: str
    text: str
    links: tuple[str, ...]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.description = ""
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attributes.get("name", "").lower() == "description":
            self.description = _clean_text(attributes.get("content", ""))[:1000]
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._ignored_depth:
            return
        cleaned = _clean_text(data)
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        self.text_parts.append(cleaned)


def _clean_text(value: str) -> str:
    return " ".join(str(value).split())


def _base_domain(hostname: str) -> str:
    hostname = hostname.lower().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def validate_public_url(url: str) -> str:
    """Validate scheme, credentials, port, and every currently resolved address."""

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebsiteResearchError(
            "Company website must be an HTTP or HTTPS URL.",
            code="invalid_website_url",
        )
    if parsed.username or parsed.password:
        raise WebsiteResearchError(
            "Company website credentials are not allowed.",
            code="invalid_website_url",
        )
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WebsiteResearchError(
            "Company website port is invalid.", code="invalid_website_url"
        ) from exc
    if port not in {80, 443}:
        raise WebsiteResearchError(
            "Company website must use a standard web port.",
            code="blocked_website_target",
        )
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise WebsiteResearchError(
            "Company website hostname could not be resolved.",
            code="website_dns_failed",
            retryable=True,
        ) from exc
    try:
        has_non_public_address = any(
            not ipaddress.ip_address(value).is_global for value in addresses
        )
    except ValueError:
        has_non_public_address = True
    if not addresses or has_non_public_address:
        raise WebsiteResearchError(
            "Company website resolves to a non-public network.",
            code="blocked_website_target",
        )
    return parsed.geturl()


class WebsiteResearcher:
    def __init__(self, *, session=None):
        self.session = session or requests.Session()

    def research(
        self,
        website_url: str,
        *,
        max_pages: int,
        timeout_seconds: int,
    ) -> ResearchResult:
        initial_url = validate_public_url(website_url)
        first = self._fetch_with_redirects(initial_url, timeout_seconds)
        pages = [first]
        initial_host = _base_domain(urlparse(first.url).hostname or "")

        candidates = []
        for href in first.links:
            candidate = urljoin(first.url, href)
            parsed = urlparse(candidate)
            if _base_domain(parsed.hostname or "") != initial_host:
                continue
            if not any(hint in parsed.path.lower() for hint in ABOUT_PATH_HINTS):
                continue
            candidate = parsed._replace(fragment="", query="").geturl()
            if candidate not in candidates and candidate != first.url:
                candidates.append(candidate)

        for candidate in candidates[: max(0, min(max_pages, 3) - 1)]:
            try:
                pages.append(self._fetch_with_redirects(candidate, timeout_seconds))
            except WebsiteResearchError:
                continue

        context_parts = []
        for page in pages:
            context_parts.append(
                f"SOURCE URL: {page.url}\nTITLE: {page.title}\n"
                f"DESCRIPTION: {page.description}\nPAGE TEXT:\n{page.text}"
            )
        model_context = "\n\n--- NEXT PAGE ---\n\n".join(context_parts)[
            :MAX_MODEL_CHARS
        ]
        digest = hashlib.sha256(model_context.encode("utf-8")).hexdigest()
        summary = first.description or first.text[:500]
        facts: dict[str, object] = {
            "page_title": first.title,
            "meta_description": first.description,
            "pages_researched": len(pages),
        }
        return ResearchResult(
            website_url=first.url,
            source_urls=tuple(page.url for page in pages),
            summary=summary,
            facts=facts,
            content_sha256=digest,
            model_context=model_context,
        )

    def _fetch_with_redirects(self, url: str, timeout_seconds: int) -> _Page:
        current = url
        initial_host = _base_domain(urlparse(url).hostname or "")
        for _ in range(4):
            current = validate_public_url(current)
            if _base_domain(urlparse(current).hostname or "") != initial_host:
                raise WebsiteResearchError(
                    "Company website redirected to a different domain.",
                    code="blocked_website_redirect",
                )
            try:
                response = self.session.get(
                    current,
                    allow_redirects=False,
                    stream=True,
                    timeout=(min(3, timeout_seconds), timeout_seconds),
                    headers={
                        "User-Agent": "BottleCRM-LeadInspector/1.0",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
            except requests.RequestException as exc:
                raise WebsiteResearchError(
                    "Company website request failed.",
                    code="website_request_failed",
                    retryable=True,
                ) from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "").strip()
                _close_response(response)
                if not location:
                    break
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                _close_response(response)
                raise WebsiteResearchError(
                    f"Company website returned HTTP {response.status_code}.",
                    code="website_http_error",
                    retryable=response.status_code == 429
                    or response.status_code >= 500,
                )
            try:
                return self._parse_response(response, current)
            finally:
                _close_response(response)
        raise WebsiteResearchError(
            "Company website redirected too many times.",
            code="website_redirect_limit",
        )

    @staticmethod
    def _parse_response(response, url: str) -> _Page:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise WebsiteResearchError(
                "Company website did not return HTML.",
                code="unsupported_website_content",
            )
        content_length = response.headers.get("Content-Length")
        try:
            declared_size = int(content_length) if content_length else 0
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > MAX_PAGE_BYTES:
            raise WebsiteResearchError(
                "Company website page is too large.", code="website_page_too_large"
            )
        content = bytearray()
        for chunk in response.iter_content(chunk_size=16_384):
            content.extend(chunk)
            if len(content) > MAX_PAGE_BYTES:
                raise WebsiteResearchError(
                    "Company website page is too large.",
                    code="website_page_too_large",
                )
        encoding = response.encoding or "utf-8"
        parser = _HTMLTextExtractor()
        try:
            decoded = bytes(content).decode(encoding, errors="replace")
        except LookupError:
            decoded = bytes(content).decode("utf-8", errors="replace")
        parser.feed(decoded)
        return _Page(
            url=url,
            title=_clean_text(" ".join(parser.title_parts))[:500],
            description=parser.description,
            text=_clean_text(" ".join(parser.text_parts))[:12_000],
            links=tuple(parser.links),
        )


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
