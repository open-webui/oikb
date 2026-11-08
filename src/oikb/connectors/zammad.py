"""Zammad connector — sync knowledge base answers from Zammad.

Auth via ZAMMAD_URL, ZAMMAD_TOKEN env vars (or an `auth:` block with
base_url / token). The token needs the `knowledge_base.reader` permission,
or `knowledge_base.editor` to see drafts and archived answers.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable

STATES = ("published", "internal", "draft", "archived")

_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|h[1-6]|li|ul|ol|tr|blockquote|pre|table)\s*>", re.I)
_CELL = re.compile(r"</t[dh]\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _html_to_text(body: str) -> str:
    text = _BREAK.sub("\n", body or "")
    text = _CELL.sub(" | ", text)
    text = html.unescape(_TAG.sub("", text))
    lines = (_INLINE_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _safe_name(name: str | None) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip() or "Untitled"


def _answer_state(answer: dict[str, Any]) -> str:
    if answer.get("archived_at"):
        return "archived"
    if answer.get("published_at"):
        return "published"
    if answer.get("internal_at"):
        return "internal"
    return "draft"


class ZammadConnector(BaseConnector):
    """Sync answers from a Zammad knowledge base."""

    def __init__(self, base_url: str | None = None, token: str | None = None, states: list[str] | None = None):
        self._base_url = (base_url or os.environ.get("ZAMMAD_URL", "")).rstrip("/")
        self._token = token or os.environ.get("ZAMMAD_TOKEN", "")
        if not self._base_url or not self._token:
            raise ValueError("Zammad credentials required. Set ZAMMAD_URL and ZAMMAD_TOKEN.")
        if not self._base_url.startswith(("http://", "https://")):
            self._base_url = f"https://{self._base_url}"
        self._states = set(states or ["published"])
        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v1",
            headers={"Authorization": f"Token token={self._token}"},
            timeout=30.0,
        )
        # display_path -> (knowledge_base_id, answer_id, translation_id, content_id, title)
        self._cache: dict[str, tuple[int, int, int, int, str]] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        resp = self._http.post("/knowledge_bases/init")
        resp.raise_for_status()
        assets = resp.json()
        categories = assets.get("KnowledgeBaseCategory", {})
        answers = assets.get("KnowledgeBaseAnswer", {})
        kb_locales = assets.get("KnowledgeBaseLocale", {})
        locale_names = self._locale_names(kb_locales) if len(kb_locales) > 1 else {}

        category_titles: dict[tuple[int, int], str] = {}
        for translation in assets.get("KnowledgeBaseCategoryTranslation", {}).values():
            category_titles[(translation["category_id"], translation["kb_locale_id"])] = translation.get("title", "")

        def category_path(category_id: int | None, kb_locale_id: int) -> list[str]:
            parts: list[str] = []
            seen: set[int] = set()
            while category_id and category_id not in seen:
                seen.add(category_id)
                parts.append(_safe_name(category_titles.get((category_id, kb_locale_id))))
                category_id = categories.get(str(category_id), {}).get("parent_id")
            return list(reversed(parts))

        entries: list[ManifestEntry] = []
        for translation in assets.get("KnowledgeBaseAnswerTranslation", {}).values():
            answer = answers.get(str(translation["answer_id"]))
            if not answer or _answer_state(answer) not in self._states:
                continue
            category = categories.get(str(answer["category_id"]), {})
            kb_locale_id = translation["kb_locale_id"]
            parts = category_path(answer["category_id"], kb_locale_id)
            if locale_names:
                parts.insert(0, locale_names.get(kb_locale_id, f"locale-{kb_locale_id}"))
            title = translation.get("title") or "Untitled"
            filename = f"{answer['id']}_{_safe_name(title)[:50]}.txt"
            checksum = hashlib.sha256(
                f"{answer['id']}:{answer.get('updated_at', '')}:{translation.get('updated_at', '')}".encode()
            ).hexdigest()[:16]
            entry = ManifestEntry(filename=filename, path="/".join(parts), checksum=checksum, size=0)
            entries.append(entry)
            self._cache[entry.display_path] = (
                category.get("knowledge_base_id", 1), answer["id"], translation["id"], translation["content_id"], title,
            )
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _locale_names(self, kb_locales: dict[str, Any]) -> dict[int, str]:
        """Map KB locale ids to locale codes (e.g. de-de). The REST API has no locale list, GraphQL does."""
        try:
            resp = self._http.post(f"{self._base_url}/graphql", json={"query": "{ locales { id locale } }"})
            resp.raise_for_status()
            system_locales = {
                int(loc["id"].rsplit("/", 1)[-1]): loc["locale"] for loc in resp.json()["data"]["locales"]
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            system_locales = {}
        return {
            kb_locale["id"]: system_locales[kb_locale["system_locale_id"]]
            for kb_locale in kb_locales.values()
            if kb_locale.get("system_locale_id") in system_locales
        }

    def read_file(self, path: str, filename: str) -> bytes:
        key = f"{path}/{filename}" if path else filename
        cached = self._cache.get(key)
        if not cached:
            raise FileNotFoundError(f"Answer not found: {filename}")
        kb_id, answer_id, translation_id, content_id, title = cached
        resp = self._http.get(f"/knowledge_bases/{kb_id}/answers/{answer_id}", params={"include_contents": translation_id})
        resp.raise_for_status()
        contents = resp.json().get("assets", {}).get("KnowledgeBaseAnswerTranslationContent", {})
        content = contents.get(str(content_id))
        if content is None:
            raise SourceFileUnavailable(f"Zammad returned no content for answer {answer_id}")
        return f"# {title}\n\n{_html_to_text(content.get('body', ''))}".encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_zammad_source(source: str) -> dict[str, Any]:
    """Parse a zammad: source string.

    Examples:
        zammad:
        zammad:https://support.example.com
        zammad:support.example.com?states=published,internal
    """
    source = source.removeprefix("zammad:")
    location, _, query = source.partition("?")
    params = dict(parse_qsl(query, keep_blank_values=True))
    unknown = set(params) - {"states"}
    if unknown:
        raise ValueError(f"Invalid Zammad source. Unknown parameter: {min(unknown)}")
    states = [s.strip() for s in params.get("states", "published").split(",") if s.strip()]
    invalid = [s for s in states if s not in STATES]
    if invalid or not states:
        raise ValueError(f"Invalid Zammad source. states must be a comma-separated subset of: {', '.join(STATES)}")
    base_url = None
    if location:
        parsed = urlsplit(location if "://" in location else f"https://{location}")
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return {"base_url": base_url, "states": states}
