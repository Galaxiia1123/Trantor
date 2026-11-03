"""Persistent task receipts; explicit submission, never automatic restart calls."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pydantic import ValidationError

from .models import ModelError
from .repository import atomic_json, now, VersionConflict


def fingerprint_for(kind, payload, parent=None):
    identity = {"kind":kind,"payload":payload}
    if parent:
        identity["parent"] = parent
    canonical = json.dumps(identity,sort_keys=True,ensure_ascii=False,allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Jobs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.active = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preparation")

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("任务 ID 无效")
        return self.root / f"{job_id}.json"

    def get(self, job_id, *, with_result=True):
        with self.lock:
            path = self.path(job_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            sidecar = path.with_suffix(".result.json")
            if sidecar.exists() and data["status"] in {"running","queued","completed"}:
                data.update(status="completed", stage="completed", message="处理完成", error=None)
                if with_result:
                    result = json.loads(sidecar.read_text(encoding="utf-8"))
                    if result["fingerprint"] != data["fingerprint"]:
                        raise ValueError("任务结果与请求凭据不一致")
                    data["result"] = result["result"]
            if data["status"] in {"queued", "running"} and job_id not in self.active:
                data.update(status="interrupted", error="服务已重启，任务结果未完整保存。不会自动重发请求；之前的模型调用可能已计费。")
            if not with_result:
                data.pop("result",None)
            return data

    def reconcile(self,job_id,result):
        """Seal an application result that reached durable storage before the receipt did."""
        with self.lock:
            current = self.get(job_id)
            if current["status"] != "interrupted":
                return current
            path = self.path(job_id)
            atomic_json(path.with_suffix(".result.json"),{"fingerprint":current["fingerprint"],"result":result},create_only=True)
            current.update(status="completed",stage="completed",message="已从原始记录找回完成结果",error=None,updated_at=now(),result=None)
            atomic_json(path,current)
            return self.get(job_id)

    def catalog(self, limit):
        entries = sorted((p for p in self.root.glob("*.json") if re.fullmatch(r"[a-f0-9]{32}",p.stem)),
                         key=lambda p:p.stat().st_mtime_ns,reverse=True)
        result = []
        for path in entries[:limit]:
            try:
                data = self.get(path.stem,with_result=False)
                result.append({k:v for k,v in data.items() if k != "fingerprint"})
            except (ValueError, OSError, KeyError):
                result.append({"id":path.stem,"kind":"unknown","status":"failed","error":"本地任务凭据不完整"})
        return result

    def submit(self, job_id, kind, payload, callback, *, parent=None):
        fingerprint = fingerprint_for(kind,payload,parent)
        with self.lock:
            path = self.path(job_id)
            if path.exists():
                existing = self.get(job_id)
                if existing["fingerprint"] != fingerprint:
                    raise VersionConflict("相同任务 ID 对应不同内容，请创建新任务")
                return existing
            if len(self.active) >= 16:
                raise VersionConflict("本地任务队列已满，请等待已有任务完成")
            context = {key:payload[key] for key in ("run_id","project_id","name","goal","actor_id") if payload.get(key)}
            if kind == "launch":
                context["title"] = payload["scenario"]["title"]
            if kind in {"definition","prepare"}:
                context["parent_job"] = parent
                request_path = path.with_suffix(".request.json")
                if request_path.exists():
                    receipt = json.loads(request_path.read_text(encoding="utf-8"))
                    if fingerprint != receipt["fingerprint"]:
                        raise VersionConflict("任务申请与已有原始请求不同")
                else:
                    atomic_json(request_path,{"fingerprint":fingerprint,"payload":payload},create_only=True)
            for key in ("goal","title","name"):
                if key in context:
                    context[key] = context[key][:200]
            data = {"id": job_id, "kind": kind, "fingerprint": fingerprint, "status": "queued",
                    "created_at": now(), "updated_at": now(), "stage": "queued", "message": "等待处理",
                    "result": None, "error": None, "context":context}
            atomic_json(path, data, create_only=True)
            self.active[job_id] = True
            self.pool.submit(self._execute, job_id, callback)
            return data

    def _execute(self, job_id, callback):
        def progress(stage, message):
            with self.lock:
                data = self.get(job_id)
                data.update(status="running", stage=stage, message=message, updated_at=now())
                atomic_json(self.path(job_id), data)
        try:
            progress("starting", "正在准备任务输入")
            result = callback(progress)
            data = self.get(job_id,with_result=False)
            atomic_json(self.path(job_id).with_suffix(".result.json"), {"fingerprint":data["fingerprint"],"result":result}, create_only=True)
            changes = {"status": "completed", "stage": "completed", "message": "处理完成", "result": None}
            if data["kind"] == "launch":
                changes["context"] = {**data["context"],"run_id":result["run_id"]}
        except Exception as error:
            if isinstance(error, ValidationError):
                detail = "；".join(e["msg"] for e in error.errors(include_input=False,include_url=False)[:4])
            else:
                detail = str(error) if isinstance(error,(ModelError,ValueError)) else f"{type(error).__name__}：任务未完成，请检查服务日志。"
            changes = {"status":"failed","stage":"failed","error":detail}
        with self.lock:
            try:
                data = self.get(job_id,with_result=False)
                data.update(changes, updated_at=now())
                atomic_json(self.path(job_id), data)
            finally:
                self.active.pop(job_id, None)
