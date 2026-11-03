from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sys
import threading
import types

import httpx
from pydantic import ValidationError
import pytest

from chuantuo.models import ModelConfig, ModelError, ModelGateway


@pytest.mark.parametrize(
    "provider,model,url,token_field",
    [
        ("openai", "user-picked-gpt", "https://api.openai.com/v1/chat/completions", "max_completion_tokens"),
        ("deepseek", "user-picked-deepseek", "https://api.deepseek.com/chat/completions", "max_tokens"),
    ],
)
def test_chat_provider_wire_format(monkeypatch, provider, model, url, token_field):
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-only-in-header")
    requests = []
    messages = [{"role": "system", "content": "private instructions"}, {"role": "user", "content": "question"}]

    def respond(request):
        requests.append(request)
        assert str(request.url) == url
        assert request.headers["authorization"] == "Bearer secret-only-in-header"
        assert json.loads(request.content) == {
            "model": model, "messages": messages, "stream": False, token_field: 83,
        }
        assert b"secret-only-in-header" not in request.content
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    gateway = ModelGateway(transport=httpx.MockTransport(respond))
    config = ModelConfig(provider=provider, model=model, api_key_env="TEST_MODEL_KEY", max_tokens=83)
    assert gateway.complete(config, messages) == "answer"
    assert len(requests) == 1
    assert "secret-only-in-header" not in config.model_dump_json()


def test_anthropic_wire_format(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-header-secret")
    messages = [
        {"role": "system", "content": "first instruction"},
        {"role": "system", "content": "second instruction"},
        {"role": "user", "content": "hello"},
    ]
    original = copy.deepcopy(messages)

    def respond(request):
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["authorization"] == "Bearer anthropic-header-secret"
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert json.loads(request.content) == {
            "model": "user-picked-claude", "max_tokens": 4096, "stream": False,
            "system": "first instruction\n\nsecond instruction", "messages": [messages[-1]],
        }
        return httpx.Response(200, json={"content": [
            {"type": "thinking", "thinking": "not returned"},
            {"type": "text", "text": "one"}, {"type": "text", "text": "two"},
        ]})

    assert ModelGateway(transport=httpx.MockTransport(respond)).complete(
        ModelConfig(provider="anthropic", model="user-picked-claude"), messages,
    ) == "one\ntwo"
    assert messages == original


def test_custom_local_endpoint_and_concurrent_context_isolation():
    barrier = threading.Barrier(2)
    received = []

    def respond(request):
        assert str(request.url) == "http://localhost:8001/v1/chat/completions"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        received.append(body)
        barrier.wait(timeout=5)
        return httpx.Response(200, json={"choices": [{"message": {"content": body["messages"][0]["content"]}}]})

    gateway = ModelGateway(transport=httpx.MockTransport(respond))
    config = ModelConfig(provider="custom", model="org/chosen:exact", base_url="http://localhost:8001/v1/chat/completions")
    inputs = [[{"role": "user", "content": label}] for label in ("agent-one", "agent-two")]
    original = copy.deepcopy(inputs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda messages: gateway.complete(config, messages), inputs))
    assert outputs == ["agent-one", "agent-two"]
    assert inputs == original
    assert len(received) == 2
    assert {body["model"] for body in received} == {"org/chosen:exact"}
    assert sorted((body["messages"] for body in received), key=lambda m: m[0]["content"]) == inputs


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed", "echo"])
def test_failures_are_safe_and_never_retry_or_switch(monkeypatch, failure):
    secret = "do-not-disclose-this-api-key"
    monkeypatch.setenv("TEST_MODEL_KEY", secret)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout(f"Authorization: Bearer {secret}; URL=https://secret.example/?key={secret}")
        if failure == "malformed":
            return httpx.Response(200, content=secret)
        if failure == "echo":
            return httpx.Response(200, json={"choices": [{"message": {"content": secret}}]})
        return httpx.Response(401, json={"error": f"Authorization: Bearer {secret}"})

    gateway = ModelGateway(transport=httpx.MockTransport(respond))
    with pytest.raises(ModelError) as exc:
        gateway.complete(ModelConfig(provider="openai", model="chosen-exactly", api_key_env="TEST_MODEL_KEY"),
                         [{"role": "user", "content": "hello"}])
    error = str(exc.value)
    assert "provider=openai model=chosen-exactly status=" in error
    assert secret not in error and "Authorization" not in error and "https://" not in error
    assert len(calls) == 1 and calls[0]["model"] == "chosen-exactly"


def test_missing_environment_variable_fails_before_http(monkeypatch):
    monkeypatch.delenv("CHUANTUO_TEST_MISSING_KEY", raising=False)
    gateway = ModelGateway(transport=httpx.MockTransport(lambda request: pytest.fail("must not send a request")))
    with pytest.raises(ModelError, match="missing_api_key"):
        gateway.complete(ModelConfig(provider="deepseek", model="selected", api_key_env="CHUANTUO_TEST_MISSING_KEY"),
                         [{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("base_url", [
    "https://user:pasted-secret@example.com/v1", "https://example.com/v1?api_key=pasted-secret",
    "https://example.com/v1#pasted-secret", "http://example.com/v1",
])
def test_unsafe_urls_rejected_without_echoing_input(base_url):
    with pytest.raises(ValidationError) as exc:
        ModelConfig(provider="custom", model="selected", base_url=base_url)
    assert base_url not in str(exc.value) and "pasted-secret" not in str(exc.value)


def test_config_and_message_validation():
    with pytest.raises(ValidationError):
        ModelConfig(provider="openai", model="selected", api_key="not-allowed")
    with pytest.raises(ValidationError):
        ModelConfig(provider="openai", model="")
    with pytest.raises(ValidationError):
        ModelConfig(provider="custom", model="selected")
    gateway = ModelGateway(transport=httpx.MockTransport(lambda request: pytest.fail("must not send a request")))
    config = ModelConfig(provider="demo", model="local", context_limit_chars=5)
    for messages, status in [
        ([], "invalid_messages"), ([{"role": "tool", "content": "hello"}], "invalid_messages"),
        ([{"role": "user", "content": "too long"}], "context_limit"),
    ]:
        with pytest.raises(ModelError, match=status):
            gateway.complete(config, messages)


def test_demo_is_explicit_lazy_and_cannot_mutate_caller_messages(monkeypatch):
    demo = types.ModuleType("chuantuo.demo")

    def respond(messages):
        messages[0]["content"] = "changed inside demo"
        return "demo answer"

    demo.respond = respond
    monkeypatch.setitem(sys.modules, "chuantuo.demo", demo)
    messages = [{"role": "user", "content": "original"}]
    gateway = ModelGateway(transport=httpx.MockTransport(lambda request: pytest.fail("demo must not call HTTP")))
    assert gateway.complete(ModelConfig(provider="demo", model="local-demo"), messages) == "demo answer"
    assert messages[0]["content"] == "original"


def test_transient_failure_retries_same_request_without_replaying_success(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retry-test-secret")
    monkeypatch.setattr("time.sleep", lambda delay: None)
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        if len(received) < 3:
            return httpx.Response(503, json={"error": {"code": "service_unavailable_error"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    config = ModelConfig(provider="deepseek", model="fixture-decision-model", max_retries=None)
    result = ModelGateway(transport=httpx.MockTransport(respond)).complete(
        config, [{"role": "user", "content": "Return JSON."}],
    )
    assert result == '{"ok":true}'
    assert len(received) == 3 and all(request == received[0] for request in received)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_permanent_errors_are_not_retried_even_when_unlimited(monkeypatch, status):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retry-test-secret")
    received = []

    def respond(request):
        received.append(request)
        return httpx.Response(status, json={"error": "permanent failure"})

    with pytest.raises(ModelError, match=f"http_{status}"):
        ModelGateway(transport=httpx.MockTransport(respond)).complete(
            ModelConfig(provider="deepseek", model="fixture-decision-model", max_retries=None),
            [{"role": "user", "content": "Return JSON."}],
        )
    assert len(received) == 1


def test_retry_budget_is_enforced(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retry-test-secret")
    monkeypatch.setattr("time.sleep", lambda delay: None)
    received = []

    def respond(request):
        received.append(request)
        return httpx.Response(503)

    with pytest.raises(ModelError, match="http_503"):
        ModelGateway(transport=httpx.MockTransport(respond)).complete(
            ModelConfig(provider="deepseek", model="fixture-decision-model", max_retries=2),
            [{"role": "user", "content": "Return JSON."}],
        )
    assert len(received) == 3


def test_deepseek_json_output_is_requested_from_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "json-test-secret")

    def respond(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["model"] == "fixture-decision-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"text":"quoted \\\"word\\\"","messages":[]}'}}]})

    result = ModelGateway(transport=httpx.MockTransport(respond)).complete(
        ModelConfig(provider="deepseek", model="fixture-decision-model", json_output=True),
        [{"role": "user", "content": "Return one JSON object."}],
    )
    assert json.loads(result) == {"text": 'quoted "word"', "messages": []}
