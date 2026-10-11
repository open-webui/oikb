from __future__ import annotations

import respx

from oikb.cli import _resolve_connector
from oikb.connectors.jira import JiraConnector, parse_jira_source


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def paragraph(*content: dict) -> dict:
    return {"type": "paragraph", "content": list(content)}


def item(value: str) -> dict:
    return {"type": "listItem", "content": [paragraph(text(value))]}


def adf(*content: dict) -> str:
    connector = JiraConnector(project_key="ABC", base_url="https://jira.example", token="t")
    return connector._adf_to_text({"type": "doc", "content": list(content)})


def test_source_parameters_are_url_decoded() -> None:
    assert parse_jira_source("jira:ABC?jql=summary~%22a%20%26%20b%22&limit=5") == {
        "project_key": "ABC",
        "jql": 'summary~"a & b"',
        "limit": "5",
    }


def test_bullet_list_becomes_markdown_list() -> None:
    assert adf({"type": "bulletList", "content": [item("one"), item("two")]}) == "- one\n- two"


def test_nested_list_is_indented_under_its_item() -> None:
    parent = {"type": "listItem", "content": [paragraph(text("parent")), {"type": "bulletList", "content": [item("child")]}]}
    assert adf({"type": "bulletList", "content": [parent]}) == "- parent\n  - child"


def test_table_becomes_markdown_table() -> None:
    def row(cell: str, *values: str) -> dict:
        return {"type": "tableRow", "content": [{"type": cell, "content": [paragraph(text(v))]} for v in values]}

    table = {"type": "table", "content": [row("tableHeader", "Env", "URL"), row("tableCell", "stage", "s.example")]}
    assert adf(table) == "| Env | URL |\n| --- | --- |\n| stage | s.example |"


def test_panel_and_blockquote_keep_their_text() -> None:
    panel = {"type": "panel", "attrs": {"panelType": "warning"}, "content": [paragraph(text("careful"))]}
    quote = {"type": "blockquote", "content": [paragraph(text("said"))]}
    assert adf(panel, quote) == "careful\n> said"


def test_ordered_list_is_numbered() -> None:
    assert adf({"type": "orderedList", "content": [item("one"), item("two")]}) == "1. one\n2. two"


@respx.mock
def test_comments_source_appends_each_comment(monkeypatch) -> None:
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_TOKEN", "t")
    comment = {
        "author": {"displayName": "Ann"},
        "created": "2026-01-02T10:00:00.000+0000",
        "body": {"type": "doc", "content": [paragraph(text("looks good"))]},
    }
    issue = {"key": "ABC-1", "fields": {"summary": "S", "updated": "u", "comment": {"comments": [comment]}}}
    route = respx.get("https://jira.example/rest/api/3/search/jql").respond(200, json={"issues": [issue]})

    with _resolve_connector("jira:ABC?comments=true") as connector:
        connector.build_manifest()
        body = connector.read_file("", "ABC-1.md").decode()

    assert "comment" in route.calls[0].request.url.params["fields"].split(",")
    assert body.endswith("## Comments\n\n**Ann** (2026-01-02): looks good")
