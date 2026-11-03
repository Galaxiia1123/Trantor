"""Serial repetitions of a sealed experiment, with one shared decision/settlement budget."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

from pydantic import Field, ValidationError

from .engine import encode
from .experiments import ExperimentRequest, prepare_experiment
from .models import protect_credentials
from .observatory import FieldWatch, observe_field
from .repository import VersionConflict, atomic_json, now
from .schema import Record


class StudyRequest(Record):
    experiment: ExperimentRequest
    repetitions: int = Field(default=3, ge=2, le=16)
    call_limit: int = Field(gt=0, le=1_000_000)
    watch: FieldWatch | None = None


def measured(sim, watch, through):
    if watch is None:
        return None
    result = observe_field(sim, watch, through, details=False, close_horizon=True)
    return {k:result[k] for k in ("actor","label","unit","value","numeric_text","record","status","first_hit")}


class Studies:
    def __init__(self, root, get_run, fork, check_parent):
        self.root = Path(root)
        self.get_run = get_run
        self.fork = fork
        self.check_parent = check_parent
        self.lock = threading.RLock()
        self.workers = {}
        self.live = {}

    def path(self, study_id):
        if not re.fullmatch(r"[a-f0-9]{32}", study_id):
            raise ValueError("实验组编号无效")
        return self.root / "_studies" / (study_id + ".json")

    def read(self, study_id):
        return json.loads(self.path(study_id).read_text("utf-8"))

    def save(self, record, *, new=False):
        record["updated_at"] = now()
        atomic_json(self.path(record["id"]), record, create_only=new)

    def create(self, study_id, parent, request):
        payload = request.model_dump(mode="json")
        with self.lock:
            if self.path(study_id).exists():
                existing = self.read(study_id)
                if existing["parent_id"] != parent.run_id or existing["request"] != payload:
                    raise VersionConflict("这个实验组编号已经用于其他条件")
                return self.view(study_id)
            self.check_parent(parent)
            protect_credentials(payload, parent.config.catalog.values())
            _, _, condition = prepare_experiment(parent, request.experiment)
            baseline = measured(parent, request.watch, request.experiment.through)
            members = []
            for index in range(1, request.repetitions + 1):
                key = uuid.uuid5(uuid.NAMESPACE_URL, f"trantor:study:{study_id}:{index}").hex
                members.append({"index":index, "run_id":key[:12], "status":"queued", "calls":0,
                                "window":request.experiment.window, "outcome":None})
            record = {"id":study_id, "parent_id":parent.run_id, "title":parent._scenario.title,
                      "condition":condition, "request":payload, "created_at":now(), "status":"running",
                      "baseline":baseline, "members":members, "error":None}
            self.save(record, new=True)
            self._start(study_id)
            return self.view(study_id)

    def _start(self, study_id):
        signal = threading.Event()
        worker = threading.Thread(target=self._execute, args=(study_id, signal), name="study-"+study_id[:8], daemon=True)
        self.workers[study_id] = (worker, signal)
        worker.start()

    @staticmethod
    def _check_member(sim, record, member):
        marker = sim.experiment or {}
        if marker.get("study") != {"id":record["id"], "index":member["index"]} or marker.get("parent_id") != record["parent_id"]:
            raise ValueError("路线记录与实验组不匹配，请检查本机资料")

    def _capture(self, record, member, sim):
        self._check_member(sim, record, member)
        snapshot = sim.snapshot(known_window=sim.window)
        member.update(status=snapshot["status"], window=snapshot["window"], calls=snapshot["model_calls"])
        if member["status"] == "completed":
            spec = StudyRequest.model_validate(record["request"])
            if member["window"] != spec.experiment.through:
                raise ValueError("实验路线的终点与约定不一致")
            member["outcome"] = measured(sim, spec.watch, member["window"])

    def _refresh(self, record):
        """A child journal may have committed before the group receipt was replaced."""
        study_id = record["id"]
        member = next((m for m in record["members"] if m["status"] not in {"completed","queued"}), None)
        if member:
            sim = self.live.get(study_id)
            if sim is None and study_id not in self.workers and (self.root/member["run_id"]).is_dir():
                sim = self.get_run(member["run_id"])
            if sim:
                self._capture(record, member, sim)
        if all(m["status"]=="completed" for m in record["members"]):
            record.update(status="completed", error=None)
        elif study_id not in self.workers:
            prior = record["status"]
            if prior in {"running","pausing","stopping"}:
                record["status"] = {"running":"interrupted","pausing":"paused","stopping":"stopped"}[prior]
        return record

    def view(self, study_id):
        with self.lock:
            record = self._refresh(self.read(study_id))
            outcomes = {}
            for member in record["members"]:
                value = member["outcome"]
                if member["status"] != "completed" or value is None:
                    continue
                key = encode(value["value"]) if value["status"]=="tracking" else value["status"]
                group = outcomes.setdefault(key, {"example":value,"indices":[]})
                group["indices"].append(member["index"])
            return {**record, "outcomes":list(outcomes.values()),
                    "calls":sum(m["calls"] for m in record["members"]),
                    "completed":sum(m["status"]=="completed" for m in record["members"])}

    def catalog(self):
        with self.lock:
            rows = []
            for path in (self.root/"_studies").glob("*.json"):
                record = self.read(path.stem)
                if record["status"] in {"running","pausing","stopping"}:
                    self._refresh(record)
                rows.append({**{k:record[k] for k in ("id","parent_id","title","condition","status","updated_at")},
                             "count":len(record["members"]), "completed":sum(m["status"]=="completed" for m in record["members"])})
            return sorted(rows, key=lambda r:r["updated_at"], reverse=True)

    def control(self, study_id, action):
        with self.lock:
            record = self._refresh(self.read(study_id))
            status = record["status"]
            if action == "resume":
                if status not in {"paused","interrupted","failed"} or study_id in self.workers:
                    raise VersionConflict("当前实验组不能继续")
                self.check_parent(self.get_run(record["parent_id"]))
                record.update(status="running", error=None)
                self.save(record)
                self._start(study_id)
            elif (action=="stop" and status not in {"completed","stopped","limit"}) or (action=="pause" and status=="running"):
                active = self.workers.get(study_id)
                record["status"] = ("stopping" if active else "stopped") if action=="stop" else ("pausing" if active else "paused")
                self.save(record)
                if active:
                    active[1].set()
            return self.view(study_id)

    def _execute(self, study_id, signal):
        try:
            while True:
                with self.lock:
                    record = self.read(study_id)
                    if signal.is_set():
                        record["status"] = "stopped" if record["status"]=="stopping" else "paused"
                        self.save(record)
                        return
                    member = next((m for m in record["members"] if m["status"]!="completed"), None)
                    if member is None:
                        record.update(status="completed", error=None)
                        self.save(record)
                        return
                    spec = StudyRequest.model_validate(record["request"])
                    prior_calls = sum(m["calls"] for m in record["members"] if m["status"]=="completed")
                    remaining = spec.call_limit - prior_calls
                    if remaining <= member["calls"]:
                        record.update(status="limit", error="实验组的调用上限已用完。")
                        self.save(record)
                        return
                    member["status"] = "opening"
                    self.save(record)
                parent = self.get_run(record["parent_id"])
                self.check_parent(parent)
                rid, index = member["run_id"], member["index"]
                if (self.root/rid).is_dir():
                    sim = self.get_run(rid)
                else:
                    if member.get("created"):
                        raise ValueError("已开始路线的本地记录缺失，无法继续计数。请先恢复原记录。")
                    sim = self.fork(parent, spec.experiment, rid, None, study={"id":study_id,"index":index}, call_limit=remaining)
                self._check_member(sim, record, member)
                with self.lock:
                    record = self.read(study_id)
                    record["members"][index-1]["created"] = True
                    self.save(record)
                    self.live[study_id] = sim
                if sim.status in {"interrupted","failed"} and sim.snapshot(known_window=sim.window)["can_resume"] and not signal.is_set():
                    sim.recover()
                if sim.status in {"ready","paused"}:
                    sim.run(pause_at_boundary=signal)
                with self.lock:
                    record = self.read(study_id)
                    member = record["members"][index-1]
                    self._capture(record, member, sim)
                    self.live.pop(study_id, None)
                    if all(m["status"]=="completed" for m in record["members"]):
                        record.update(status="completed", error=None)
                    elif sim.status=="failed":
                        exhausted = not sim.snapshot(known_window=sim.window)["can_resume"]
                        record.update(status="limit" if exhausted else "failed", error=sim.error)
                    elif signal.is_set() or sim.status in {"stopped","paused","interrupted"}:
                        record["status"] = "stopped" if record["status"]=="stopping" or sim.status=="stopped" else "paused"
                    self.save(record)
                    if record["status"]!="running":
                        return
        except Exception as exc:
            if isinstance(exc, ValidationError):
                message = "；".join(e["msg"] for e in exc.errors(include_input=False)[:4])
            else:
                message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}，当前实验路线未完成"
            with self.lock:
                record = self.read(study_id)
                record.update(status="failed", error=message)
                self.save(record)
        finally:
            with self.lock:
                self.live.pop(study_id, None)
                self.workers.pop(study_id, None)

    def shutdown(self):
        with self.lock:
            active = list(self.workers.items())
            for study_id, (_, signal) in active:
                record = self.read(study_id)
                if record["status"]=="running":
                    record["status"]="pausing"
                    self.save(record)
                signal.set()
        for _, (worker, _) in active:
            worker.join()
