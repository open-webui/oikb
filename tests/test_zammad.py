from __future__ import annotations

import httpx
import pytest
import respx

from oikb.connectors import SourceFileUnavailable
from oikb.connectors.zammad import ZammadConnector, parse_zammad_source

BASE = "https://support.example.com"


def _answer(answer_id: int, category_id: int, **state: str) -> dict[str, object]:
    return {
        "id": answer_id,
        "category_id": category_id,
        "published_at": None,
        "internal_at": None,
        "archived_at": None,
        "updated_at": "2026-01-01T00:00:00Z",
        **state,
    }


def _init(locales: int = 1) -> dict[str, object]:
    kb_locales = {str(i): {"id": i, "system_locale_id": 10 + i} for i in range(1, locales + 1)}
    category_translations: dict[str, object] = {}
    answer_translations: dict[str, object] = {}
    for locale in range(1, locales + 1):
        for category_id, title in ((1, f"Guides {locale}"), (2, "How/To")):
            tid = str(len(category_translations) + 1)
            category_translations[tid] = {"category_id": category_id, "kb_locale_id": locale, "title": title}
        for answer_id, title in ((1, "Reset password"), (2, "Internal notes"), (3, "Draft"), (4, "Old")):
            tid = len(answer_translations) + 1
            answer_translations[str(tid)] = {
                "id": tid,
                "answer_id": answer_id,
                "kb_locale_id": locale,
                "content_id": 100 + tid,
                "title": title,
                "updated_at": "2026-01-02T00:00:00Z",
            }
    return {
        "KnowledgeBaseLocale": kb_locales,
        "KnowledgeBaseCategory": {
            "1": {"id": 1, "knowledge_base_id": 1, "parent_id": None},
            "2": {"id": 2, "knowledge_base_id": 1, "parent_id": 1},
        },
        "KnowledgeBaseCategoryTranslation": category_translations,
        "KnowledgeBaseAnswer": {
            "1": _answer(1, 2, published_at="2026-01-01T00:00:00Z"),
            "2": _answer(2, 1, internal_at="2026-01-01T00:00:00Z"),
            "3": _answer(3, 1),
            "4": _answer(4, 1, published_at="2026-01-01T00:00:00Z", archived_at="2026-01-03T00:00:00Z"),
        },
        "KnowledgeBaseAnswerTranslation": answer_translations,
    }


def test_parse_source() -> None:
    assert parse_zammad_source("zammad:") == {"base_url": None, "states": ["published"]}
    assert parse_zammad_source("zammad:support.example.com/?states=published,internal") == {
        "base_url": BASE,
        "states": ["published", "internal"],
    }


@pytest.mark.parametrize("source", ["zammad:x?states=secret", "zammad:x?states=", "zammad:x?unexpected=1"])
def test_parse_rejects_invalid_source(source: str) -> None:
    with pytest.raises(ValueError):
        parse_zammad_source(source)


@respx.mock
def test_manifest_defaults_to_published_answers() -> None:
    init = respx.post(f"{BASE}/api/v1/knowledge_bases/init").mock(return_value=httpx.Response(200, json=_init()))
    with ZammadConnector(base_url=BASE, token="secret") as connector:
        entries = connector.build_manifest()

    assert [e.display_path for e in entries] == ["Guides 1/How_To/1_Reset password.txt"]
    assert init.calls[0].request.headers["Authorization"] == "Token token=secret"


@respx.mock
def test_manifest_filters_states() -> None:
    respx.post(f"{BASE}/api/v1/knowledge_bases/init").mock(return_value=httpx.Response(200, json=_init()))
    with ZammadConnector(base_url=BASE, token="t", states=["internal", "draft", "archived"]) as connector:
        paths = [e.display_path for e in connector.build_manifest()]

    assert paths == ["Guides 1/2_Internal notes.txt", "Guides 1/3_Draft.txt", "Guides 1/4_Old.txt"]


@respx.mock
def test_manifest_prefixes_locale_for_multilingual_kb() -> None:
    respx.post(f"{BASE}/api/v1/knowledge_bases/init").mock(return_value=httpx.Response(200, json=_init(locales=2)))
    respx.post(f"{BASE}/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"locales": [{"id": "gid://zammad/Locale/11", "locale": "en-us"}]}},
        )
    )
    with ZammadConnector(base_url=BASE, token="t") as connector:
        paths = [e.display_path for e in connector.build_manifest()]

    # Locale 2 is unknown to GraphQL and falls back to its KB locale id.
    assert paths == [
        "en-us/Guides 1/How_To/1_Reset password.txt",
        "locale-2/Guides 2/How_To/1_Reset password.txt",
    ]


@respx.mock
def test_read_file_converts_html() -> None:
    respx.post(f"{BASE}/api/v1/knowledge_bases/init").mock(return_value=httpx.Response(200, json=_init()))
    answer = respx.get(f"{BASE}/api/v1/knowledge_bases/1/answers/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "assets": {
                    "KnowledgeBaseAnswerTranslationContent": {
                        "101": {"id": 101, "body": "<p>Open <b>Profile</b> &amp; click</p><ul><li>Reset</li></ul>"}
                    }
                }
            },
        )
    )
    with ZammadConnector(base_url=BASE, token="t") as connector:
        entry = connector.build_manifest()[0]
        text = connector.read_file(entry.path, entry.filename).decode()

    assert answer.calls[0].request.url.params["include_contents"] == "1"
    assert text == "# Reset password\n\nOpen Profile & click\nReset"


@respx.mock
def test_read_file_without_content_is_unavailable() -> None:
    respx.post(f"{BASE}/api/v1/knowledge_bases/init").mock(return_value=httpx.Response(200, json=_init()))
    respx.get(f"{BASE}/api/v1/knowledge_bases/1/answers/1").mock(return_value=httpx.Response(200, json={"assets": {}}))
    with ZammadConnector(base_url=BASE, token="t") as connector:
        entry = connector.build_manifest()[0]
        with pytest.raises(SourceFileUnavailable):
            connector.read_file(entry.path, entry.filename)


def test_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAMMAD_URL", raising=False)
    monkeypatch.delenv("ZAMMAD_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ZAMMAD_URL"):
        ZammadConnector()
