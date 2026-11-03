"""One local model profile. Windows protects saved credentials with the current user's DPAPI."""
import base64
import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes

from pydantic import SecretStr

from .models import ModelConfig, _BASE_URLS, _PROFILE_PREFIX, credential_ready, endpoint_scope, register_key
from .repository import atomic_json
from .schema import Record


class ProfileInput(Record):
    model: ModelConfig
    key: SecretStr | None = None


def dpapi(data, *, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]
    buffer = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    dest = Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise ValueError("当前系统账户无法保护或解锁此模型凭据")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(dest.data))


class ModelProfile:
    def __init__(self, root):
        self.path = root / "_settings" / "model.local.json"
        self.lock = threading.RLock()
        self.saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.model = ModelConfig.model_validate(self.saved["model"]) if self.saved.get("model") else None
        for ref, record in self.saved.get("keys", {}).items():
            if os.name == "nt":
                try:
                    secret = dpapi(base64.b64decode(record["protected"]), decrypt=True).decode()
                except ValueError:
                    continue
                register_key(ref, record["scope"], secret)

    def view(self):
        with self.lock:
            return {"model":self.model.model_dump(mode="json") if self.model else None,
                    "ready":bool(self.model and credential_ready(self.model)),
                    "key_storage":"windows_user" if os.name == "nt" else "session"}

    def save(self, request):
        model = request.model
        if model.provider == "demo":
            raise ValueError("请选择实际使用的模型接口")
        secret = request.key.get_secret_value() if request.key else ""
        if secret and (not secret.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in secret)):
            raise ValueError("密钥中包含不可用于请求头的字符")
        if secret and any(secret in str(value) for value in model.model_dump().values()):
            raise ValueError("密钥只可填入独立密钥框")
        with self.lock:
            scope = endpoint_scope(model)
            keys = dict(self.saved.get("keys", {}))
            if secret:
                ref = _PROFILE_PREFIX + uuid.uuid4().hex.upper()
                if os.name == "nt":
                    keys[ref] = {"scope":scope, "protected":base64.b64encode(dpapi(secret.encode())).decode()}
                model = model.model_copy(update={"api_key_env":ref})
            elif self.model and self.model.api_key_env and scope == endpoint_scope(self.model) and not model.api_key_env:
                model = model.model_copy(update={"api_key_env":self.model.api_key_env})
            if not model.api_key_env and model.base_url and model.provider in _BASE_URLS and scope[1] != _BASE_URLS[model.provider]:
                raise ValueError("更换 API 地址后请为该地址填写密钥或指定其环境变量")
            saved = {"model":model.model_dump(mode="json"),"keys":keys}
            atomic_json(self.path, saved)
            if secret:
                register_key(ref, scope, secret)
            self.saved, self.model = saved, model
            return self.view()
