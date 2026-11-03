import json

import httpx
import pytest
from pydantic import ValidationError

from chuantuo.models import ModelConfig, ModelError, ModelGateway


@pytest.mark.parametrize("base,explicit", [("http://localhost/v1", "responses"),("http://localhost/v1/responses",None)])
def test_responses_isolated_text_and_images_use_exact_endpoint(base,explicit):
    image="data:image/png;base64,QUJD"
    cfg=ModelConfig(provider="custom", model="fixture-choice", base_url=base, protocol=explicit,
                    json_output=True, supports_images=True,max_tokens=150)
    messages=[{"role":"system","content":"规则"},{"role":"user","content":[{"type":"text","text":"返回 JSON"},
              {"type":"image_url","image_url":{"url":image}}]}]
    def handler(request):
        assert str(request.url)=="http://localhost/v1/responses"
        payload=json.loads(request.content)
        assert payload["input"][1]["content"] == [{"type":"input_text","text":"返回 JSON"},
            {"type":"input_image","image_url":image,"detail":"auto"}]
        assert payload["model"]=="fixture-choice" and payload["store"] is False and payload["max_output_tokens"]==150
        assert payload["text"]["format"]=={"type":"json_object"}
        assert not {"previous_response_id","conversation","messages","response_format"} & payload.keys()
        return httpx.Response(200,json={"status":"completed","output":[{"type":"reasoning","content":[
            {"type":"reasoning_text","text":"HIDDEN_REASONING"}]},{"type":"message","role":"assistant","content":[
            {"type":"output_text","text":'{"ok":'},{"type":"output_text","text":"true}"}]}]})
    assert ModelGateway(transport=httpx.MockTransport(handler)).complete(cfg,messages)=='{"ok":true}'
    assert messages[1]["content"][0]["type"]=="text"


@pytest.mark.parametrize("status,items,reason,expected", [
    ("incomplete",[],"max_output_tokens","output_limit"),
    ("incomplete",[],"content_filter","incomplete_response"),
    ("failed",[],None,"response_not_completed"),
    ("completed",[{"type":"message","role":"assistant","content":[{"type":"refusal","refusal":"private rejection"}]}],None,"model_refusal"),
    ("completed",[{"type":"reasoning","content":[]}],None,"invalid_response")])
def test_responses_incomplete_or_nonpublic_text_cannot_become_action(status,items,reason,expected):
    cfg=ModelConfig(provider="custom",model="exact",protocol="responses",base_url="http://localhost")
    def handler(request):
        return httpx.Response(200,json={"status":status,"output":items,"incomplete_details":{"reason":reason}})
    with pytest.raises(ModelError,match=expected):
        ModelGateway(transport=httpx.MockTransport(handler)).complete(cfg,[{"role":"user","content":"hello"}])


def test_explicit_protocol_conflict_rejected_and_workspace_header_server_only(monkeypatch):
    with pytest.raises(ValidationError):
        ModelConfig(provider="openai",model="x",protocol="chat_completions",base_url="https://api.openai.com/v1/responses")
    monkeypatch.setenv("TEST_WORKSPACE","wrkspc_fixture")
    config=ModelConfig(provider="custom",model="named",base_url="http://localhost/v1/messages",workspace_id_env="TEST_WORKSPACE")
    def handler(request):
        assert request.headers["anthropic-workspace-id"]=="wrkspc_fixture" and b"wrkspc_fixture" not in request.content
        return httpx.Response(200,json={"content":[{"type":"text","text":"ok"}]})
    assert ModelGateway(transport=httpx.MockTransport(handler)).complete(config,[{"role":"user","content":"hi"}])=="ok"
