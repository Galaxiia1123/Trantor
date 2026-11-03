"""Local projects and a catalog over existing, unmodified run journals."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .engine import encode


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value, *, create_only=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            os.link(temporary, path)
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class VersionConflict(ValueError):
    pass


class Repository:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.projects = self.root / "_projects"
        self.lock = threading.RLock()
        self._catalog = {}

    def project_folder(self, project_id):
        if not re.fullmatch(r"[a-f0-9]{12}", project_id):
            raise ValueError("项目 ID 无效")
        return self.projects / project_id

    def save_project(self, payload: dict, project_id=None, expected_version=0):
        with self.lock:
            project_id = project_id or uuid.uuid4().hex[:12]
            folder = self.project_folder(project_id)
            versions = sorted(folder.glob("v*.json"))
            latest = json.loads(versions[-1].read_text(encoding="utf-8")) if versions else None
            version = latest["version"] if latest else 0
            if version != expected_version:
                raise VersionConflict("项目已有较新版本。请先重新打开，或保存为另一个项目；当前编辑未被覆盖。")
            if latest and latest["payload"] == payload:
                return latest
            record = {"id": project_id, "version": version + 1, "created_at": latest["created_at"] if latest else now(),
                      "updated_at": now(), "payload": payload}
            scene = payload.get("scenario", {})
            if latest and scene.get("format_version") == "1.1":
                prior = latest["payload"]["scenario"]
                if prior.get("format_version") != "1.1":
                    raise ValueError("请将重新定义文字关系的场景另存为新项目")
                before = {(e["source"], e["target"]):e for e in prior["influences"]}
                after = {(e["source"], e["target"]):e for e in scene["influences"]}
                changes = []
                for edge in sorted(before.keys() | after.keys()):
                    if before.get(edge) != after.get(edge):
                        changes.append({"source":edge[0], "target":edge[1], "before":before.get(edge), "after":after.get(edge)})
                if changes:
                    reason = payload.get("assistant", {}).get("revision_reason", "").strip()
                    if not reason:
                        raise ValueError("请说明本次修改有向关系的依据")
                    record["relation_revision"] = {"reason":reason, "from_version":version, "changes":changes}
            try:
                atomic_json(folder / f"v{version + 1:06d}.json", record, create_only=True)
            except FileExistsError:
                raise VersionConflict("另一份编辑刚刚保存，请重新打开项目或另存副本") from None
            return record

    def save_prepared(self, payload, job_id, project_id=None, expected_version=0):
        """One accepted preparation maps to one version, even after a lost save response."""
        if not re.fullmatch(r"[a-f0-9]{32}",job_id):
            raise ValueError("整理任务编号无效")
        with self.lock:
            target = project_id or uuid.uuid5(uuid.NAMESPACE_URL,"chuantuo:prepared-project:"+job_id).hex[:12]
            existing = sorted(self.project_folder(target).glob("v*.json"),reverse=True)
            for path in existing:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("payload",{}).get("assistant",{}).get("adopted_prepare_job") == job_id:
                    if any(record["payload"].get(k) != payload.get(k) for k in ("scenario","config","preparation_records")):
                        raise VersionConflict("同一整理结果已有不同的保存内容，请先打开已保存的版本")
                    return record
            return self.save_project(payload,target,expected_version)

    def get_project(self, project_id, version=None):
        folder = self.project_folder(project_id)
        paths = sorted(folder.glob("v*.json"))
        if not paths:
            raise FileNotFoundError("项目不存在")
        path = folder / f"v{version:06d}.json" if version is not None else paths[-1]
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def project_summary(record):
        payload = record["payload"]
        scene = payload.get("scenario", {})
        return {key: record[key] for key in ("id", "version", "created_at", "updated_at")} | {
            "title": payload.get("title") or scene.get("title") or "未命名项目",
            "actors": len(scene.get("entities", [])), "sources": len(scene.get("sources", [])),
            "goal": payload.get("assistant", {}).get("goal", ""),
            "relation_change_reason":record.get("relation_revision", {}).get("reason")}

    def list_projects(self):
        result = []
        if self.projects.exists():
            for folder in self.projects.iterdir():
                if folder.is_dir() and re.fullmatch(r"[a-f0-9]{12}", folder.name):
                    result.append(self.project_summary(self.get_project(folder.name)))
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def versions(self, project_id):
        return [self.project_summary(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(self.project_folder(project_id).glob("v*.json"), reverse=True)]

    def catalog(self, *, with_outcomes=False):
        result, errors = [], []
        for path in self.root.glob("*/events.jsonl"):
            if path.parent.name.startswith("_"):
                continue
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            cached = self._catalog.get(str(path))
            if cached and cached[0] == signature:
                result.append({**cached[1], **({"end_state":cached[2]} if with_outcomes and cached[2] is not None else {})})
                continue
            try:
                record = None
                with path.open("rb") as stream:
                    record, end_state = self._summarize(stream, path)
                self._catalog[str(path)] = (signature, record, end_state)
                result.append({**record, **({"end_state":end_state} if with_outcomes and end_state is not None else {})})
            except (ValueError, KeyError, TypeError, OSError):
                recoverable = False
                if path.is_file() and stat.st_size:
                    with path.open("rb") as stream:
                        stream.seek(-1,2)
                        recoverable = stream.read(1) != b"\n"
                errors.append({"directory":path.parent.name, "can_salvage":recoverable,
                               "message":"记录不完整或格式不受支持，原文件保留。"})
        return {"runs": sorted(result, key=lambda item: item["updated_at"], reverse=True), "warnings": errors}

    @staticmethod
    def _summarize(stream, path):
        record = None
        end_state = None
        for index, line in enumerate(stream, 1):
            event = json.loads(line)
            if event["id"] != f"e{index:06d}" or not line.endswith(b"\n"):
                raise ValueError("事件记录不连续或不完整")
            data = event["data"]
            if event["kind"] == "run_created":
                config = data["config"]
                if config.get("format_version") == "1.1":
                    providers = {config["model"]["provider"]}
                else:
                    used = [config["models"]["world"], *config["models"]["layers"].values()]
                    providers = {config["models"]["configs"][key]["provider"] for key in used}
                record = {"run_id": data["run_id"], "title": data["scenario"]["title"],
                          "format_version": config.get("format_version", "1.0"), "created_at": event["recorded_at"], "updated_at": event["recorded_at"],
                          "status": "interrupted", "window": 0, "windows": config["windows"],
                          "actors": len(data["scenario"]["entities"]), "project_id": None,
                          "mode": "demo" if providers == {"demo"} else "mixed" if "demo" in providers else "api",
                          "directory": path.parent.name, "events": 0, "reports": 0}
                if data.get("experiment"):
                    record["experiment"] = data["experiment"]
            if record is None:
                raise ValueError("缺少初始记录")
            record["events"] += 1
            record["updated_at"] = event["recorded_at"]
            if event["kind"] in {"run_started", "run_recovered"}:
                record["status"] = "interrupted"
            elif event["kind"] == "run_status":
                record["status"] = data["status"]
            elif event["kind"] == "stop_requested":
                record["status"] = "stopped"
            elif event["kind"] == "window_committed":
                record["window"] = event["window"]
                if record.get("experiment"):
                    end_state = data["state"]
            elif event["kind"] == "project_linked":
                record["project_id"] = data["project_id"]
            elif event["kind"] == "report_created":
                record["reports"] += 1
        if record is None:
            raise ValueError("空记录")
        if record["window"] == record["windows"]:
            record["status"] = "completed"
        return record, end_state

    def run_directory(self, run_id):
        match = next((item for item in self.catalog()["runs"] if item["run_id"] == run_id), None)
        if match is None:
            raise FileNotFoundError("运行不存在或记录不完整")
        return self.root / match["directory"]
