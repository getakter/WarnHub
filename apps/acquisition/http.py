"""Polite HTTP client: identifiable User-Agent, timeouts, retries with backoff, per-domain delay,
robots.txt, size limits, env-var credentials and optional JavaScript rendering (Playwright)."""
import logging
import os
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from django.conf import settings as django_settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("ewce.http")

ROBOTS_TTL = 6 * 3600


class FetchError(Exception):
    """A URL could not be fetched (message is shown to operators)."""


class RobotsDisallowed(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str = ""
    headers: dict = field(default_factory=dict)
    rendered_js: bool = False
    flags: list = field(default_factory=list)


class _DomainThrottle:
    """Minimum delay between requests to the same host, shared by all threads in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = {}

    def wait(self, host, delay):
        if delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0))
            self._next[host] = start + delay
        if start > now:
            time.sleep(start - now)


throttle = _DomainThrottle()
_robots_cache = {}
_robots_lock = threading.Lock()


def _host(url):
    return (urlparse(url).hostname or "").lower()


class PoliteClient:
    def __init__(self, fetch_settings=None, *, log=None):
        self.cfg = dict(fetch_settings or {})
        self.log = log or (lambda level, message, **ctx: getattr(logger, level, logger.info)(message))
        self.user_agent = self.cfg.get("user_agent") or django_settings.EWCE_USER_AGENT
        self.session = requests.Session()
        retry = Retry(
            total=int(self.cfg.get("retries", 3)), connect=int(self.cfg.get("retries", 3)),
            backoff_factor=float(self.cfg.get("backoff_seconds", 2)), status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}), respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Language": "bn,en;q=0.8"})
        self.session.headers.update(self.cfg.get("headers") or {})
        for name, value in (self.cfg.get("cookies") or {}).items():
            self.session.cookies.set(name, value)
        self.session.verify = bool(self.cfg.get("verify_ssl", True))
        if not self.session.verify:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        user_env, pass_env = self.cfg.get("auth_user_env"), self.cfg.get("auth_password_env")
        if user_env or pass_env:
            user, password = os.environ.get(user_env or ""), os.environ.get(pass_env or "")
            if user is None or password is None:
                raise FetchError(f"Basic-auth env vars not set in .env: {user_env}, {pass_env}")
            self.session.auth = (user, password)
        self.timeout = int(self.cfg.get("timeout_seconds", 30))
        self.delay = float(self.cfg.get("per_domain_delay_seconds", 2))
        self.max_bytes = int(float(self.cfg.get("max_download_mb", 50)) * 1024 * 1024)

    # -- robots.txt ---------------------------------------------------------------------
    def allowed_by_robots(self, url):
        if not self.cfg.get("respect_robots", True):
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        with _robots_lock:
            cached = _robots_cache.get(root)
        if cached is None or cached[0] < time.time():
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self.session.get(f"{root}/robots.txt", timeout=min(self.timeout, 15))
                if response.status_code in (401, 403):
                    parser.disallow_all = True
                elif response.status_code >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(response.text.splitlines())
            except requests.RequestException as exc:
                self.log("warning", f"robots.txt for {root} unreachable ({exc}); proceeding.")
                parser.allow_all = True
            cached = (time.time() + ROBOTS_TTL, parser)
            with _robots_lock:
                _robots_cache[root] = cached
        return cached[1].can_fetch(self.user_agent, url)

    # -- fetching -----------------------------------------------------------------------
    def get(self, url, *, render_js=None):
        if not url.lower().startswith(("http://", "https://")):
            raise FetchError(f"Not an http(s) URL: {url}")
        if not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {url} (untick 'Respect robots.txt' only with permission)")
        render_js = self.cfg.get("requires_js", False) if render_js is None else render_js
        throttle.wait(_host(url), self.delay)
        if render_js:
            if js_rendering_available():
                return self._render(url)
            self.log("warning", f"{url}: page needs JavaScript but Playwright is not enabled; fetched without it.")
        result = self._download(url)
        if render_js:
            result.flags.append("js_not_rendered")
        return result

    def _download(self, url):
        try:
            response = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
        except requests.exceptions.SSLError as exc:
            raise FetchError(f"SSL error for {url}: {exc}. If the site's certificate is broken, untick "
                             "'Verify SSL certificates' for this feed.") from exc
        except requests.RequestException as exc:
            raise FetchError(f"Could not fetch {url}: {type(exc).__name__}: {exc}") from exc
        try:
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} for {url}")
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > self.max_bytes:
                raise FetchError(f"{url} is {declared // 1024} KB, above the {self.max_bytes // 1024} KB limit")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_bytes:
                    raise FetchError(f"{url} exceeded the {self.max_bytes // 1024} KB download limit")
                chunks.append(chunk)
        finally:
            response.close()
        return FetchResult(url=response.url, status=response.status_code, content=b"".join(chunks),
                           content_type=response.headers.get("Content-Type", ""),
                           headers={k: v for k, v in response.headers.items()})

    def _render(self, url):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=self.user_agent, ignore_https_errors=not self.session.verify)
                response = page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
                html = page.content()
                status = response.status if response else 200
            finally:
                browser.close()
        if status >= 400:
            raise FetchError(f"HTTP {status} for {url} (rendered)")
        return FetchResult(url=url, status=status, content=html.encode("utf-8"),
                           content_type="text/html; charset=utf-8", rendered_js=True)


def js_rendering_available():
    if not django_settings.EWCE_ENABLE_PLAYWRIGHT:
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def clear_robots_cache():
    with _robots_lock:
        _robots_cache.clear()
