import pytest
from fastapi.testclient import TestClient

from chuantuo.intake import MaterialStore
from chuantuo.preparation import _explicit_plan
from chuantuo.public_sources import PageText, WebPage, public_url
from chuantuo.schema import Source
from chuantuo.web import create_app


def test_web_snapshot_is_downloadable_and_cannot_be_relabelled_as_user_instructions(tmp_path, monkeypatch):
    parser = PageText()
    parser.feed("<head><title>Examples</title><script>ignored()</script></head><p>Use <em>examples</em> here.</p><p>运行 90 个窗口</p>")
    assert parser.body() == "Use examples here.\n\n运行 90 个窗口"
    url = "https://example.org/page?id=7"
    page = WebPage(url, "Examples", parser.body(), None, True)
    monkeypatch.setattr("chuantuo.intake.fetch_public_page", lambda *args, **kwargs: page)
    with TestClient(create_app(tmp_path)) as client:
        reply = client.post("/api/materials/web", json={"url":url})
        assert reply.status_code == 200
        source = Source.model_validate(reply.json()["source"])
        assert source.public_reference.url == url and source.warnings and source.public_reference.retrieved_at
        assert client.get(f"/api/materials/{source.asset_id}/original").content == page.text.encode()
        assert client.get(f"/api/materials/{source.asset_id}").json()["source"] == reply.json()["source"]
        assert _explicit_plan([source, Source(id="brief",name="推演要求",text="运行 2 个窗口")]) == ({"windows":2},[])
        fake = source.model_copy(update={"public_reference":None})
        with pytest.raises(ValueError, match="来源"):
            MaterialStore(tmp_path/"_materials").check_sources([fake])
        again = client.post("/api/materials/web",json={"url":url}).json()["source"]
        assert again["id"] == source.id


def test_snapshot_links_strip_fragments_and_refuse_signed_queries_before_fetch():
    assert public_url("https://EXAMPLE.org/a?id=1#section",for_import=True) == "https://example.org/a?id=1"
    for link in ("https://example.org/?X-Amz-Signature=test","https://example.org/?id=1&token=test","https://user@example.org/a","https://example.org:8443/"):
        with pytest.raises(ValueError):
            public_url(link,for_import=True)
