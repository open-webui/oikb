import httpx
import respx

from oikb.connectors.notion import NotionConnector


@respx.mock
def test_notion_reports_page_url_and_last_edited_time():
    respx.post("https://api.notion.com/v1/databases/root/query").mock(return_value=httpx.Response(200, json={
        "results": [{
            "id": "page-1",
            "url": "https://www.notion.so/Guide-page1",
            "last_edited_time": "2026-11-20T09:15:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Guide"}]}},
        }],
        "has_more": False,
    }))
    connector = NotionConnector("root", token="token")
    try:
        [entry] = connector.build_manifest()
    finally:
        connector.close()
    assert entry.source_url == "https://www.notion.so/Guide-page1"
    assert entry.last_modified == "2026-11-20T09:15:00.000Z"
