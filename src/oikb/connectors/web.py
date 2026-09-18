"""Web connector — crawl a website or sitemap and sync pages to a Knowledge Base.

Requires: pip install oikb[web]
Uses sitemap.xml for discovery or same-domain link crawling.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry
from oikb.http import make_http_client

log = logging.getLogger(__name__)


def _html_to_text(html: str) -> str:
    """Extract text from HTML, stripping tags."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style elements.
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        log.debug("BeautifulSoup not available, falling back to regex-based HTML stripping")

        # Fallback: regex strip.
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


class WebConnector(BaseConnector):
    """Crawl a website and produce a manifest of pages.

    Args:
        url:       Root URL or sitemap URL.
        delay:     Delay between requests in seconds (default: 0.5).
        max_pages: Maximum number of pages to crawl (default: 500).
    """

    def __init__(
        self,
        url: str,
        delay: float = 0.5,
        max_pages: int = 500,
    ):
        self.url = url.rstrip("/")
        self.delay = delay
        self.max_pages = max_pages
        self._parsed = urlparse(self.url)
        self._domain = f"{self._parsed.scheme}://{self._parsed.netloc}"

        self._http = make_http_client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "oikb/0.1 (+https://github.com/open-webui/oikb)"},
        )

        # Cache: url -> text content.
        self._cache: dict[str, str] = {}

        log.debug(f"WebConnector initialised: url={self.url}, delay={self.delay}s, max_pages={self.max_pages}")

    def build_manifest(self) -> list[ManifestEntry]:
        """Discover pages via sitemap or crawling, then build manifest."""
        log.info(f"Starting manifest build for {self.url}")

        urls = self._discover_urls()
        pageCount = len(urls)
        log.info(f"Discovered {pageCount} URL(s) for {self.url}")

        if pageCount > self.max_pages:
            log.warning(
                f"Discovered {pageCount} URLs but max_pages={self.max_pages} — "
                f"truncating to {self.max_pages} pages"
            )

        entries: list[ManifestEntry] = []
        skippedEmptyCount = 0
        errorCount = 0

        for url in urls[:self.max_pages]:
            try:
                text = self._fetch_page(url)
                if not text.strip():
                    log.debug(f"Skipping {url} — page yielded no text content")
                    skippedEmptyCount += 1
                    continue

                self._cache[url] = text

                # Convert URL to a filename.
                path_part = urlparse(url).path.strip("/")
                if not path_part:
                    path_part = "index"

                parts = path_part.rsplit("/", 1)
                if len(parts) == 2:
                    dir_path, name = parts
                else:
                    dir_path, name = "", parts[0]

                # Clean up the filename.
                name = re.sub(r"[^\w\-.]", "_", name)
                if not name.endswith(".txt"):
                    name += ".txt"

                checksum = hashlib.sha256(text.encode()).hexdigest()[:16]

                entries.append(
                    ManifestEntry(
                        filename=name,
                        path=dir_path,
                        checksum=checksum,
                        size=len(text.encode()),
                    )
                )
                log.debug(f"Added manifest entry: {dir_path}/{name} ({len(text.encode())} bytes, checksum={checksum})")

                if self.delay > 0:
                    time.sleep(self.delay)

            except Exception as exc:
                log.warning(f"Failed to process page {url}: {exc}")
                errorCount += 1
                continue

        entries.sort(key=lambda e: e.display_path)

        log.info(
            f"Manifest build complete for {self.url}: "
            f"{len(entries)} entries added, {skippedEmptyCount} skipped (empty), {errorCount} failed"
        )
        return entries

    def _discover_urls(self) -> list[str]:
        """Discover URLs from sitemap.xml or by crawling links."""
        # Try sitemap first.
        if self.url.endswith(".xml"):
            log.debug(f"URL ends with .xml, parsing directly as sitemap: {self.url}")
            return self._parse_sitemap(self.url)

        sitemap_url = f"{self._domain}/sitemap.xml"
        log.debug(f"Trying sitemap at {sitemap_url}")

        try:
            urls = self._parse_sitemap(sitemap_url)
            if urls:
                log.info(f"Using sitemap {sitemap_url}: found {len(urls)} URL(s)")
                return urls

            log.debug(f"Sitemap at {sitemap_url} returned no URLs, falling back to link crawl")
        except Exception as exc:
            log.debug(f"Sitemap not available at {sitemap_url} ({exc}), falling back to link crawl")

        # Fall back to crawling.
        log.info(f"Discovering URLs via link crawl starting from {self.url}")
        return self._crawl_links()

    def _parse_sitemap(self, url: str) -> list[str]:
        """Parse a sitemap.xml and return all URLs."""
        log.debug(f"Fetching sitemap: {url}")

        resp = self._http.get(url)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls: list[str] = []

        # Handle sitemap index.
        sitemapIndexEntries = root.findall(".//sm:sitemap/sm:loc", ns)
        if sitemapIndexEntries:
            log.debug(f"Sitemap index detected at {url}: processing {len(sitemapIndexEntries)} sub-sitemap(s)")

        for sitemap in sitemapIndexEntries:
            if sitemap.text:
                subUrls = self._parse_sitemap(sitemap.text)
                log.debug(f"Sub-sitemap {sitemap.text} yielded {len(subUrls)} URL(s)")
                urls.extend(subUrls)

        # Handle regular sitemap.
        for loc in root.findall(".//sm:url/sm:loc", ns):
            if loc.text:
                urls.append(loc.text)

        log.debug(f"Parsed sitemap {url}: {len(urls)} URL(s) total")
        return urls

    def _crawl_links(self) -> list[str]:
        """Crawl same-domain links starting from the root URL."""
        visited: set[str] = set()
        queue = [self.url]
        urls: list[str] = []
        errorCount = 0

        while queue and len(urls) < self.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue

            visited.add(url)
            urls.append(url)
            log.debug(f"Crawling page {len(urls)}/{self.max_pages}: {url}")

            try:
                resp = self._http.get(url)
                resp.raise_for_status()

                # Extract same-domain links.
                newLinkCount = 0
                for match in re.finditer(r'href=["\']([^"\']+)["\']', resp.text):
                    link = urljoin(url, match.group(1))
                    parsed = urlparse(link)

                    # Same domain, no fragments, no query params.
                    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if parsed.netloc == self._parsed.netloc and clean not in visited:
                        queue.append(clean)
                        newLinkCount += 1

                log.debug(f"Found {newLinkCount} new link(s) on {url}")

                if self.delay > 0:
                    time.sleep(self.delay)

            except Exception as exc:
                log.warning(f"Failed to crawl {url}: {exc}")
                errorCount += 1
                continue

        if len(urls) >= self.max_pages:
            log.warning(f"Link crawl reached max_pages limit ({self.max_pages}) — some pages may be omitted")

        log.info(f"Link crawl finished: {len(urls)} URL(s) collected, {errorCount} error(s), domain={self._domain}")
        return urls

    def _fetch_page(self, url: str) -> str:
        """Fetch a page and extract text."""
        if url in self._cache:
            log.debug(f"Cache hit for {url}")
            return self._cache[url]

        log.debug(f"Fetching page: {url}")
        resp = self._http.get(url)
        resp.raise_for_status()

        text = _html_to_text(resp.text)
        log.debug(f"Fetched {url}: HTTP {resp.status_code}, {len(text)} chars extracted")
        return text

    def read_file(self, path: str, filename: str) -> bytes:
        """Return cached page content."""
        # Find matching URL from cache.
        target = f"{path}/{filename}" if path else filename
        target = target.removesuffix(".txt")

        log.debug(f"read_file: looking up cache for target={target}")

        for url, text in self._cache.items():
            url_path = urlparse(url).path.strip("/")
            if not url_path:
                url_path = "index"
            if url_path == target or url_path.endswith(target):
                log.debug(f"read_file: resolved {target} -> {url}")
                return text.encode("utf-8")

        log.error(f"read_file: page not found in cache: {target} (cache size={len(self._cache)})")
        raise FileNotFoundError(f"Page not in cache: {target}")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        log.debug("Closing WebConnector HTTP client")
        self._http.close()


def parse_web_source(source: str) -> dict[str, str | None]:
    """Parse a web:URL source string."""
    url = source.removeprefix("web:")
    if not url.startswith("http"):
        url = f"https://{url}"
    return {"url": url}
