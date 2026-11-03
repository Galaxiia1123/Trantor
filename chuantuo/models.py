"""Stateless model requests with explicit provider and image capability selection.

``base_url`` is an API prefix (including ``/v1`` when required), or the full
completion endpoint. Credentials are resolved for each call, never serialized.
"""

from __future__ import annotations

import ipaddress
import copy
import json
import os
import re
import time
import threading
import weakref
from contextvars import ContextVar
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "deepseek": "https://api.deepseek.com",
}
_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_ENDPOINTS = {"chat_completions":"/chat/completions","responses":"/responses","messages":"/messages"}
_receipt_sink = ContextVar("provider_receipt_sink", default=None)
_cancel_retry = ContextVar("cancel_provider_retry", default=None)
_PROFILE_PREFIX = "CHUANTUO_LOCAL_"
_profile_keys = {}
_profiles_lock = threading.RLock()


def endpoint_scope(config):
    return (config.provider, (config.base_url or _BASE_URLS.get(config.provider, "")).rstrip("/"))


def register_key(reference, scope, secret):
    with _profiles_lock:
        _profile_keys[reference] = (tuple(scope), secret)


def configured_key(config):
    name = config.api_key_env or _KEY_ENVS.get(config.provider)
    if name and name.startswith(_PROFILE_PREFIX):
        with _profiles_lock:
            entry = _profile_keys.get(name)
        return entry[1] if entry and entry[0] == endpoint_scope(config) else None
    return os.environ.get(name) if name else None


def protect_credentials(value, configs=()):
    """Reject literal configured credentials before prompts, receipts or stored project data."""
    names = set(_KEY_ENVS.values())
    names.update(c.api_key_env for c in configs if c.api_key_env)
    secrets = {s for name in names if (s := os.environ.get(name))}
    with _profiles_lock:
        secrets.update(secret for _, secret in _profile_keys.values())
    if not secrets:
        return
    def contains(item):
        if isinstance(item,str):
            return any(item == s or (len(s) >= 8 and s in item) for s in secrets)
        if isinstance(item,dict):
            return any(contains(key) or contains(v) for key,v in item.items())
        return isinstance(item,(list,tuple)) and any(contains(v) for v in item)
    if contains(value):
        raise ValueError("输入包含已配置接口密钥的原值。请移除凭据后重新提交")


def _is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def measured_call(gateway, config, messages, receive, cancel_retry=None):
    """Bind a receipt to this one call, including transport retries, across parallel actors."""
    token = _receipt_sink.set(receive)
    stop_token = _cancel_retry.set(cancel_retry)
    try:
        return gateway.complete(config, messages)
    finally:
        _receipt_sink.reset(token)
        _cancel_retry.reset(stop_token)


class ModelConfig(BaseModel):
    """Persist only the environment variable name, never an API key value."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True,
        revalidate_instances="always",
    )

    provider: Literal["openai", "anthropic", "deepseek", "custom", "demo"]
    model: str = Field(min_length=1, max_length=256)
    base_url: str | None = None
    api_key_env: str | None = None
    protocol: Literal["chat_completions", "responses", "messages"] | None = None
    workspace_id_env: str | None = None
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_tokens: int = Field(default=4096, gt=0, strict=True)
    context_limit_chars: int = Field(default=120000, gt=0, strict=True)
    # Zero preserves fail-fast behavior. None explicitly opts into retrying
    # transient failures until recovery or process cancellation.
    max_retries: int | None = Field(default=0, ge=0, strict=True)
    retry_delay_seconds: float = Field(default=2, gt=0, le=30, allow_inf_nan=False)
    json_output: bool = False
    thinking: Literal["default", "disabled", "low", "high", "max"] = "default"
    supports_images: bool = False

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value) or "://" in value:
            raise ValueError("model must be a model identifier, not a URL or credential")
        return value

    @field_validator("api_key_env","workspace_id_env")
    @classmethod
    def validate_env_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must be an environment variable name")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Do not include the supplied URL in errors: it may contain a pasted key.
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("base_url must be an absolute HTTP(S) API URL")
        if any(c in value for c in ("?", "#", "\\")):
            raise ValueError("base_url must not contain a query, fragment or backslash")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise ValueError("base_url is invalid") from None
        if not host or parsed.scheme not in {"https", "http"} or port == 0:
            raise ValueError("base_url must be an absolute HTTP(S) API URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.scheme == "http" and not _is_loopback(host):
            raise ValueError("base_url requires HTTPS except for localhost or loopback")
        return value

    @model_validator(mode="after")
    def validate_provider_config(self) -> ModelConfig:
        protect_credentials([self.model,self.base_url,self.api_key_env], [self])
        if re.match(r"^sk-(?:proj-|ant-|live-)?[A-Za-z0-9_-]{24,}$",self.model):
            raise ValueError("模型名疑似密钥，请填写模型编号")
        if self.provider == "custom" and self.base_url is None:
            raise ValueError("custom provider requires base_url")
        allowed = {"openai":{"responses","chat_completions"}, "anthropic":{"messages"}, "deepseek":{"chat_completions"},
                   "custom":{"responses","chat_completions","messages"}, "demo":{"chat_completions"}}
        if self.wire_protocol not in allowed[self.provider]:
            raise ValueError("所选提供商不支持此协议配置")
        suffix = (self.base_url or "").rstrip("/")
        for protocol, ending in _ENDPOINTS.items():
            if suffix.endswith(ending) and protocol != self.wire_protocol:
                raise ValueError("完整 API 端点与所选协议不一致")
        if self.workspace_id_env and self.wire_protocol != "messages":
            raise ValueError("工作区环境变量只适用于 Messages 协议")
        if self.json_output and self.wire_protocol == "messages":
            raise ValueError("json_output currently requires a Chat Completions provider")
        if self.thinking != "default" and (self.provider != "deepseek" or self.wire_protocol != "chat_completions"):
            raise ValueError("此思考参数适用于 DeepSeek Chat Completions 接口")
        return self

    @property
    def wire_protocol(self):
        if self.protocol:
            return self.protocol
        endpoint = (self.base_url or "").rstrip("/")
        for key, suffix in _ENDPOINTS.items():
            if endpoint.endswith(suffix):
                return key
        return "messages" if self.provider == "anthropic" else "chat_completions"


class ModelError(RuntimeError):
    """A safe error containing provider, requested model and failure status."""


class RetryStopped(ModelError):
    """An operator stopped a window while a temporary provider failure awaited retry."""


def credential_ready(config: ModelConfig) -> bool:
    if config.provider == "demo":
        return True
    name = config.api_key_env or _KEY_ENVS.get(config.provider)
    return (not name or bool(configured_key(config))) and (not config.workspace_id_env or bool(os.environ.get(config.workspace_id_env)))


def _failure(
    config: ModelConfig, status: str, detail: str, *, secret: str | None = None,
) -> ModelError:
    if secret is None:
        secret = configured_key(config)
    model = config.model.replace(secret, "[redacted]") if secret else config.model
    return ModelError(f"provider={config.provider} model={model} status={status}: {detail}")


def _copy_messages(config: ModelConfig, messages: list[dict]) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise _failure(config, "invalid_messages", "messages must be a nonempty list")
    copied: list[dict[str, str]] = []
    total = 0
    roles = {"system", "user", "assistant"}
    if config.provider in {"openai", "custom", "demo"} and config.wire_protocol != "messages":
        roles.add("developer")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise _failure(config, "invalid_messages", "each message needs only role and content")
        role, content = message["role"], message["content"]
        if not isinstance(role, str) or role not in roles:
            raise _failure(config, "invalid_messages", "unsupported message role")
        if isinstance(content, str) and content.strip():
            total += len(content)
        elif isinstance(content, list) and content and role == "user" and config.supports_images:
            for block in content:
                if not isinstance(block, dict):
                    raise _failure(config, "invalid_messages", "invalid image content block")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    total += len(block["text"])
                elif block.get("type") == "image_url" and isinstance(block.get("image_url"), dict):
                    url = block["image_url"].get("url", "")
                    if not isinstance(url, str) or not re.fullmatch(r"data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+", url):
                        raise _failure(config, "invalid_messages", "only inline PNG, JPEG or WebP images are accepted")
                else:
                    raise _failure(config, "invalid_messages", "unsupported image content block")
        else:
            raise _failure(config, "invalid_messages", "message content requires text, or explicit image capability")
        if total > config.context_limit_chars:
            raise _failure(config, "context_limit", "message text exceeds context_limit_chars")
        copied.append({"role": role, "content": copy.deepcopy(content)})
    return copied


class ModelGateway:
    """Independent calls; optional same-request retries, never model fallback."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self._client_lock = threading.Lock()
        self._clients = {}
        self._finalizers = []
        self._closed = False

    def _connection_pool(self, endpoint):
        trust_env = self._transport is None and not _is_loopback(urlsplit(endpoint).hostname)
        with self._client_lock:
            if self._closed:
                raise ModelError("模型接口已关闭")
            if trust_env not in self._clients:
                client = httpx.Client(transport=self._transport, follow_redirects=False,trust_env=trust_env)
                self._clients[trust_env] = client
                self._finalizers.append(weakref.finalize(self, client.close))
            return self._clients[trust_env]

    def close(self):
        with self._client_lock:
            self._closed = True
            for finalize in self._finalizers:
                finalize()

    def _receipt(self, config, attempt, started, response=None, error=None, secret=None):
        sink = _receipt_sink.get()
        if sink is None:
            return
        record = {"provider": config.provider, "protocol":config.wire_protocol, "requested_model": config.model, "attempt": attempt,
                  "transport": "http" if self._transport is None else "mock_http" if isinstance(self._transport, httpx.MockTransport) else "custom_transport",
                  "elapsed_ms": round((time.monotonic() - started) * 1000), "http_status": None, "error": error}
        if response is not None:
            record["http_status"] = response.status_code
            if response.is_success:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if isinstance(data, dict):
                    model = data.get("model")
                    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", model) and not (secret and secret in model):
                        record["response_model"] = model
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        keys = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
                                "cache_read_input_tokens", "cache_creation_input_tokens", "prompt_cache_hit_tokens","prompt_cache_miss_tokens")
                        record["usage"] = {k: usage[k] for k in keys if type(usage.get(k)) is int and usage[k] >= 0}
                        details = usage.get("completion_tokens_details")
                        if isinstance(details,dict) and type(details.get("reasoning_tokens")) is int and details["reasoning_tokens"] >= 0:
                            record["usage"]["reasoning_tokens"] = details["reasoning_tokens"]
        sink(record)

    def _post(self, config, endpoint, headers, payload, secret):
        retries = 0
        client = self._connection_pool(endpoint)
        while True:
            started = time.monotonic()
            try:
                # Raw requests bypass the shared client's cookie jar and default auth.
                request = httpx.Request("POST", endpoint, headers=headers, json=payload,
                    extensions={"timeout": httpx.Timeout(config.timeout_seconds).as_dict()})
                response = client.send(request, auth=None, follow_redirects=False)
            except httpx.TimeoutException:
                self._receipt(config, retries + 1, started, error="timeout")
                error = _failure(config, "timeout", "model request timed out", secret=secret)
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                self._receipt(config, retries + 1, started, error="transport_error")
                error = _failure(config, "transport_error", "model request could not be completed", secret=secret)
            except (httpx.HTTPError, OSError, ValueError, UnicodeError):
                self._receipt(config, retries + 1, started, error="transport_error")
                raise _failure(config, "transport_error", "model request could not be completed", secret=secret) from None
            else:
                self._receipt(config, retries + 1, started, response=response, secret=secret)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response
                error = _failure(config, f"http_{response.status_code}", "provider temporarily unavailable", secret=secret)
            if config.max_retries is not None and retries >= config.max_retries:
                raise error from None
            delay = min(config.retry_delay_seconds * 2 ** min(retries, 10), 30)
            retries += 1
            stop = _cancel_retry.get()
            if stop is None:
                time.sleep(delay)
            elif stop.wait(delay):
                raise RetryStopped("收到结束请求，已停止重试临时失败的模型调用") from None

    def complete(self, config: ModelConfig, messages: list[dict]) -> str:
        if not isinstance(config, ModelConfig):
            raise ModelError("provider=unknown model=unknown status=invalid_config: expected ModelConfig")
        try:
            config = ModelConfig.model_validate(config)
        except ValueError:
            raise ModelError("provider=unknown model=unknown status=invalid_config: invalid ModelConfig") from None
        copied = _copy_messages(config, messages)
        try:
            protect_credentials([part["content"] for part in copied],[config])
        except ValueError:
            raise _failure(config,"credential_in_prompt","configured credential appears in message content") from None

        if config.provider == "demo":
            try:
                from .demo import respond
                if config.model == "letter-desk-v1":
                    from .letter_case import scripted_response
                    answer = scripted_response(json.loads(copied[-1]["content"]))
                    result = respond(copied) if answer is None else json.dumps(answer,ensure_ascii=False)
                else:
                    result = respond(copied)
            except Exception:
                raise _failure(config, "demo_error", "demo response failed") from None
            if not isinstance(result, str) or not result.strip():
                raise _failure(config, "invalid_response", "demo returned no text")
            return result

        env_name = config.api_key_env or _KEY_ENVS.get(config.provider)
        secret = configured_key(config)
        if env_name and not secret:
            raise _failure(config, "missing_api_key", "所选接口的模型凭据不可用")
        if secret and (not secret.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in secret)):
            raise _failure(config, "invalid_api_key", "API key environment variable contains invalid header characters", secret=secret)
        base_url = config.base_url or _BASE_URLS[config.provider]
        if secret and (
            secret in config.model or secret in base_url
            or secret in json.dumps(copied, ensure_ascii=False)
        ):
            raise _failure(config, "credential_in_input", "API credentials may appear only in request headers", secret=secret)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        payload: dict = {"model": config.model, "messages": copied, "stream": False}
        protocol = config.wire_protocol
        if protocol == "messages":
            headers["anthropic-version"] = "2026-10-21"
            if config.workspace_id_env:
                workspace = os.environ.get(config.workspace_id_env, "")
                if not re.fullmatch(r"wrkspc_[A-Za-z0-9_-]+",workspace):
                    raise _failure(config,"invalid_workspace","workspace ID environment variable is unset or invalid")
                headers["anthropic-workspace-id"] = workspace
            payload["max_tokens"] = config.max_tokens
            system: list[str] = []
            conversation: list[dict[str, str]] = []
            for message in copied:
                if message["role"] == "system":
                    if conversation:
                        raise _failure(config, "invalid_messages", "Anthropic system messages must precede the conversation")
                    system.append(message["content"])
                else:
                    if isinstance(message["content"], list):
                        blocks = []
                        for block in message["content"]:
                            if block["type"] == "text":
                                blocks.append(block)
                            else:
                                prefix, data = block["image_url"]["url"].split(",", 1)
                                blocks.append({"type": "image", "source": {"type": "base64",
                                    "media_type": prefix[5:].split(";")[0], "data": data}})
                        conversation.append({"role": message["role"], "content": blocks})
                    else:
                        conversation.append(message)
            if not conversation or conversation[0]["role"] != "user":
                raise _failure(config, "invalid_messages", "Anthropic conversation must start with a user message")
            payload["messages"] = conversation
            if system:
                payload["system"] = "\n\n".join(system)
            suffix = "/messages"
        elif protocol == "responses":
            inputs = []
            for message in copied:
                content = message["content"]
                if isinstance(content,list):
                    content = [({"type":"input_text","text":part["text"]} if part["type"]=="text" else
                                {"type":"input_image","image_url":part["image_url"]["url"], "detail":"auto"}) for part in content]
                inputs.append({"role":message["role"],"content":content})
            payload = {"model":config.model,"input":inputs,"max_output_tokens":config.max_tokens,"store":False,"stream":False}
            if config.json_output:
                payload["text"] = {"format":{"type":"json_object"}}
            suffix = "/responses"
        else:
            token_field = "max_completion_tokens" if config.provider == "openai" else "max_tokens"
            payload[token_field] = config.max_tokens
            if config.json_output:
                payload["response_format"] = {"type": "json_object"}
            if config.thinking != "default":
                payload["thinking"] = {"type": "disabled" if config.thinking == "disabled" else "enabled"}
                if config.thinking != "disabled":
                    payload["reasoning_effort"] = config.thinking
            suffix = "/chat/completions"
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith(suffix):
            endpoint += suffix

        response = self._post(config, endpoint, headers, payload, secret)

        if not 200 <= response.status_code < 300:
            raise _failure(config, f"http_{response.status_code}", "provider rejected the request", secret=secret)
        try:
            data = response.json()
            if protocol == "responses":
                status = data.get("status")
                if status == "incomplete":
                    reason = (data.get("incomplete_details") or {}).get("reason")
                    raise _failure(config, "output_limit" if reason=="max_output_tokens" else "incomplete_response",
                                   "Responses returned an incomplete result", secret=secret)
                if status != "completed":
                    raise _failure(config, "response_not_completed", "Responses did not complete", secret=secret)
                text_parts = []
                for item in data["output"]:
                    if item.get("type") != "message" or item.get("role") != "assistant":
                        continue
                    for part in item["content"]:
                        if part.get("type") == "refusal":
                            raise _failure(config, "model_refusal","provider declined the request",secret=secret)
                        if part.get("type") == "output_text":
                            text_parts.append(part["text"])
                result = "".join(text_parts)
            elif protocol == "messages":
                if data.get("stop_reason") == "max_tokens":
                    raise _failure(config, "output_limit", "provider output reached max_tokens", secret=secret)
                text_parts = [block["text"] for block in data["content"] if block["type"] == "text"]
                result = "\n".join(text_parts)
            else:
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise _failure(config, "output_limit", "provider output reached the token limit", secret=secret)
                result = choice["message"]["content"]
            if not isinstance(result, str) or not result.strip():
                raise ValueError("no text")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise _failure(config, "invalid_response", "provider returned no valid text response", secret=secret) from None
        if secret and secret in result:
            raise _failure(config, "credential_in_response", "provider response contained a credential", secret=secret)
        return result
