"""Window barriers, private observations and validated single-commit settlement."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .bridge import Memory, act_together, make_agent, resolve
from .continuity import DECISION_EVIDENCE_INSTRUCTION, Notebook, actor_instruction, add_decision_inbox, objective_input, world_instruction
from .locking import writer_lock, writer_active
from .perception import Perception, decision_view
from .pacing import ReviewPlan, revised_deadlines
from .models import ModelError, ModelGateway, RetryStopped, measured_call, protect_credentials
from .schema import Decision, Expression, Influence, RunConfig, RunConfig11, Scenario, Scenario11, Settlement, load_config, load_scenario
from .settlement import actor_evidence, complete_scheduled_reviews, settled_state, verify_commits
from .schema import validate_outbox
from .documents import envelope_scope
from .briefing import runtime_scene
from .replies import envelope_key, validate_reply, validate_requested_time
from .mail_timing import MailTiming


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class InvalidSettlement(ValueError):
    """Only rejected proposals can be corrected; a failed disk write must stop."""


class Journal:
    _TRANSPORT = {"model_input", "model_output", "report_input", "report_output",
                  "analysis_input", "analysis_output", "report_batch_input", "report_batch_output"}

    def __init__(self, directory: Path | None = None):
        self._events: list[dict] = []
        self.comparison_ideas=[]
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self.directory = directory
        self._disk_size = 0
        self._offsets = {}
        self._window_rows = {}
        self._round_observations = {}
        self._pending_rounds = {}
        self._last_boundary_reset = 0
        self._letters_by_envelope={}
        self._letters_by_reader={}
        self._arrivals={}
        self._requested_letters={}
        self._request_mail=[]
        self._reply_events={}
        self._settlements = {}
        self._usage = {}
        self._prep_sizes = {}
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
            if (directory / "events.jsonl").exists():
                raise ValueError("输出目录已有记录，请选择新目录；不会覆盖旧运行")

    @classmethod
    def open(cls, directory: Path, *, first_kind="run_created"):
        journal = cls()
        path = directory / "events.jsonl"
        journal.directory = directory
        with path.open("rb") as stream:
            for index, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                    if not line.endswith(b"\n") or event["id"] != f"e{index:06d}" or not isinstance(event["data"], dict):
                        raise ValueError("事件不完整")
                    event["kind"], event["window"], event["recorded_at"]
                except (ValueError, KeyError, TypeError):
                    raise ValueError(f"运行记录第 {index} 行损坏或不完整，暂不能恢复。") from None
                journal._remember(event, len(line))
        if not journal._events or journal._events[0]["kind"] != first_kind:
            raise ValueError("运行缺少初始场景记录")
        return journal

    def _remember(self, event, size):
        kind, data = event["kind"], event["data"]
        if kind=="scene_parse":
            self.comparison_ideas=deepcopy(data.get("comparison_ideas",[]))
        if kind == "experiment_forked":
            self._usage.clear()
            self._prep_sizes.clear()
        if kind in {"model_input","model_output","prepare_input","prepare_output","provider_attempt","prepare_provider_attempt",
                    "report_input","report_output","report_batch_input","report_batch_output","analysis_input","analysis_output"}:
            stage = ("准备" if kind.startswith("prepare") else "主体" if data.get("purpose") == "agent"
                     else "世界" if data.get("purpose") == "world" else "访谈" if "analysis" in (data.get("purpose") or kind) else "报告")
            usage = self._usage.setdefault(stage, {"calls":0,"attempts":0,"with_usage":0,"input_chars":0,"output_chars":0,
                                                    "input_tokens":0,"output_tokens":0,"cache_read":0,"cache_write":0})
            if kind.endswith("_input"):
                usage["calls"] += 1
                chars = sum(len(m["content"]) for m in data.get("messages", []) if isinstance(m.get("content"), str))
                usage["input_chars"] += chars
                if kind == "prepare_input":
                    self._prep_sizes[event["id"]] = chars
            elif kind.endswith("_output"):
                if kind == "prepare_output" and data.get("reused_from"):
                    usage["calls"] -= 1
                    usage["input_chars"] -= self._prep_sizes.get(data.get("input_id"), 0)
                    usage["local_replays"] = usage.get("local_replays",0) + 1
                else:
                    usage["output_chars"] += len(data.get("text", ""))
            else:
                usage["attempts"] += 1
                counts = data.get("usage", {})
                if counts:
                    usage["with_usage"] += 1
                    usage["input_tokens"] += counts.get("prompt_tokens", counts.get("input_tokens", 0))
                    usage["output_tokens"] += counts.get("completion_tokens", counts.get("output_tokens", 0))
                    usage["cache_read"] += counts.get("cache_read_input_tokens", counts.get("prompt_cache_hit_tokens", 0))
                    usage["cache_write"] += counts.get("cache_creation_input_tokens", 0)
        elif kind == "scene_parse" and data.get("preparation_trace", {}).get("usage"):
            trace = data["preparation_trace"]
            phase = deepcopy(trace["usage"].get("准备", {}))
            if "new_model_calls" in trace:
                phase["calls"] = trace["new_model_calls"]
            self._usage["关联场景准备"] = phase
        if self.directory and event["kind"] in self._TRANSPORT:
            # Keep large model prompts on disk. Polling and recovery need only the header.
            self._offsets[event["id"]] = (self._disk_size, size)
            keys = ("purpose", "actor_id", "config_id", "input_id", "level", "batch")
            event = {**event, "data": {k: event["data"][k] for k in keys if k in event["data"]}}
        self._events.append(event)
        if kind=="round_pending":
            self._pending_rounds[event["window"]]=event
        elif kind=="run_recovered":
            self._last_boundary_reset=int(event["id"][1:])
        if kind=="observation":
            key=(event["window"],data["visible"]["round"],data["actor_id"])
            self._round_observations.setdefault(key,[]).append(event)
        elif kind=="message_delivered":
            self._letters_by_envelope.setdefault(envelope_key(data),[]).append(event)
            self._letters_by_reader.setdefault(data["to"],[]).append(event)
            if data.get("dispatch_id"):
                self._arrivals.setdefault(data["dispatch_id"],[]).append(event)
            if data.get("reply_by") is not None:
                self._requested_letters.setdefault(data["to"],[]).append(event)
        if kind in {"message_delivered","message_dispatched"}:
            if data.get("reply_to"):
                self._reply_events.setdefault(data["reply_to"],[]).append(event)
            if data.get("reply_by") is not None:
                self._request_mail.append(event)
        if event["kind"] in {"expression","decision_deferred","message_dispatched","message_delivered","window_committed","settlement_rejected","model_failure","external_event"}:
            self._window_rows.setdefault(event["window"], []).append(event)
        if event["kind"] == "window_committed":
            self._settlements[event["window"]] = event
        self._disk_size += size

    def window_rows(self, window, offset, limit=None, *, actor=None, kind=None):
        with self._lock:
            items = self._window_rows.get(window, ())
            if actor or kind:
                def matches(item):
                    data = item["data"]
                    if kind and item["kind"] != kind:
                        return False
                    if not actor or actor in (data.get("actor_id"),data.get("from"),data.get("to")):
                        return True
                    if item["kind"]=="external_event":
                        return data["visibility"]["public"] or actor in data["visibility"]["readers"] or any(e["entity_id"]==actor for e in data["assignments"])
                    return item["kind"] == "window_committed" and any(r["entity_id"]==actor for r in data["reviews"])
                items = [e for e in items if matches(e)]
            return items[offset:offset + limit if limit is not None else None], len(items)

    def settlement(self, window):
        with self._lock:
            return self._settlements.get(window)

    def observation_receipt(self,actor,window,turn,delivery,discarded):
        with self._lock:
            for obs in reversed(self._round_observations.get((window,turn,actor),())):
                if obs["id"] in discarded:
                    continue
                if any(all(m.get(k)==delivery.get(k) for k in ("action_id","from","to","text","dispatch_id","enclosures","reply_to","reply_by")) for m in obs["data"]["visible"]["messages"]):
                    return obs["id"]
        return None

    def observation_before(self, action, discarded):
        data=action["data"]
        return self.round_observation(data["actor_id"],action["window"],data["round"],discarded,before=action["id"])

    def round_observation(self,actor,window,turn,discarded,*,before=None):
        with self._lock:
            for obs in reversed(self._round_observations.get((window,turn,actor),())):
                if obs["id"] not in discarded and (before is None or int(obs["id"][1:])<int(before[1:])):
                    return obs
        return None

    def reply_mail(self,obs,discarded,*,recent_windows=3):
        actor=obs["data"]["actor_id"];visible=obs["data"]["visible"]
        before=int(obs["id"][1:]);counts={};messages=[];current=set();recent=[]
        with self._lock:
            for message in visible["messages"]:
                key=envelope_key(message)
                choices=self._letters_by_envelope.get(key,())
                index=counts.get(key,0)
                while index<len(choices) and choices[index]["id"] in discarded:index+=1
                if index==len(choices) or int(choices[index]["id"][1:])>=before:
                    raise ValueError("收件箱与这轮实际送达的信件不符")
                ref=choices[index]["id"];counts[key]=index+1;current.add(ref)
                messages.append({**message,"receipt_id":ref})
            # A few unanswered requests can survive the ordinary recent-history boundary.
            requests=self._requested_letters.get(actor,())
            for letter in reversed(requests[-64:]):
                ref=letter["id"]
                if int(ref[1:])>=before or ref in current or ref in discarded:
                    continue
                if any(int(r["id"][1:])<before and r["id"] not in discarded for r in self._reply_events.get(ref,())):
                    continue
                d=letter["data"]
                recent.append({"receipt_id":ref,"from":d["from"],"window":letter["window"],"excerpt":d["text"][:72],"reply_by":d["reply_by"]})
                current.add(ref)
                if len(recent)==2:break
            for letter in reversed(self._letters_by_reader.get(actor,())):
                if letter["window"]<obs["window"]-recent_windows:break
                ref=letter["id"]
                if int(ref[1:])<before and ref not in current and ref not in discarded:
                    d=letter["data"]
                    recent.append({"receipt_id":ref,"from":d["from"],"window":letter["window"],"excerpt":d["text"][:72]})
                    if len(recent)==4:break
        return {**visible,"messages":messages},recent

    def has_replies(self,ref,through,discarded):
        with self._lock:return any(e["window"]<=through and e["id"] not in discarded for e in self._reply_events.get(ref,()))

    def arrival(self,ref,through,discarded):
        with self._lock:
            return next((e for e in self._arrivals.get(ref,()) if e["window"]<=through and e["id"] not in discarded),None)

    def request_mail(self,through,discarded,*,responses_to=None):
        with self._lock:
            rows=self._request_mail if responses_to is None else self._reply_events.get(responses_to,())
            return [e for e in rows if e["window"]<=through and e["id"] not in discarded]

    def has_requested_mail(self,through,discarded):
        with self._lock:
            return any(e["window"]<=through and e["id"] not in discarded for e in self._request_mail)

    def pending_round(self,window,discarded):
        with self._lock:
            event=self._pending_rounds.get(window)
            if not event or int(event["id"][1:])<=self._last_boundary_reset:
                return None
            turn=event["data"]["round"]
            if any(e["kind"] in {"expression","decision_deferred"} and e["data"]["round"]==turn and e["id"] not in discarded
                   for e in self._window_rows.get(window,())):
                return None
            return deepcopy(event)

    def validate_replies(self,decision,actor,observation_id,discarded):
        for message in decision.messages:
            if not message.reply_to:continue
            try:receipt=self.event(message.reply_to) if message.reply_to not in discarded else None
            except FileNotFoundError:receipt=None
            validate_reply(receipt,actor,message.recipients,observation_id)

    def inbox_before(self, action, discarded):
        if obs:=self.observation_before(action,discarded):
            inbox=obs["data"]["visible"]["messages"]
            return obs["id"],len(inbox),inbox[:3]

    def usage(self):
        with self._lock:
            return deepcopy(self._usage)

    def _full(self, event):
        position = self._offsets.get(event["id"])
        if position is None:
            return deepcopy(event)
        with (self.directory / "events.jsonl").open("rb") as stream:
            stream.seek(position[0])
            return json.loads(stream.read(position[1]))

    def append(self, kind: str, data: dict, window: int = 0) -> dict:
        with self._lock:
            event = {"id": f"e{len(self._events)+1:06d}", "kind": kind,
                     "window": window, "recorded_at": datetime.now(timezone.utc).isoformat(),
                     "data": deepcopy(data)}
            if self.directory:
                raw = (encode(event) + "\n").encode("utf-8")
                mode = "ab" if self._events else "xb"
                with writer_lock(self.directory / ".journal.lock", wait=True), (self.directory / "events.jsonl").open(mode) as stream:
                    if stream.tell() != self._disk_size:
                        raise ValueError("磁盘记录已被另一进程修改，请重新打开运行")
                    stream.write(raw)
                    if kind in {"window_committed", "run_status", "run_recovered"}:
                        stream.flush()
                        os.fsync(stream.fileno())
            self._remember(event, len(raw) if self.directory else 0)
            self._changed.notify_all()
            return deepcopy(event)

    def wait_for_offset(self, offset, timeout):
        with self._changed:
            return self._changed.wait_for(lambda: len(self._events) > offset, timeout)

    def snapshot(self, kinds=None) -> list[dict]:
        return self.selected(kinds)

    def selected(self, kinds=None, *, transport_details=True) -> list[dict]:
        with self._lock:
            copy_event = self._full if transport_details else deepcopy
            return [copy_event(e) for e in self._events if kinds is None or e["kind"] in kinds]

    def event(self, event_id):
        with self._lock:
            if not re.fullmatch(r"e[0-9]{6,12}", event_id):
                raise FileNotFoundError("证据编号无效")
            index = int(event_id[1:]) - 1
            if index < 0 or index >= len(self._events) or self._events[index]["id"] != event_id:
                raise FileNotFoundError("该运行没有这条记录")
            return self._full(self._events[index])

    def private_history(self, actor_id, through=None, *, last_observation=False, exact_window=None):
        with self._lock:
            discarded = {eid for e in self._events if e["kind"]=="run_recovered" for eid in e["data"]["discarded_event_ids"]}
            committed = {e["window"] for e in self._events if e["kind"]=="window_committed" and (through is None or e["window"]<=through)}
            if exact_window is not None:
                committed.intersection_update({exact_window})
            ordered = reversed(self._events) if last_observation else self._events
            records = []
            for event in ordered:
                if event["window"] in committed and event["id"] not in discarded and actor_evidence(event,actor_id):
                    if not last_observation or event["kind"]=="observation":
                        records.append(self._full(event))
                        if last_observation:
                            break
            return records

    def decision_context(self, actor_id, window, turn, discarded):
        """Read the accepted actor input. Historical journals predate direct expression/output links."""
        with self._lock:
            actions = [e for e in self._window_rows.get(window,()) if e["kind"] in {"expression","decision_deferred"} and e["id"] not in discarded
                       and e["data"]["actor_id"]==actor_id]
            rounds = sorted({a["data"]["round"] for a in actions})
            action = next((a for a in reversed(actions) if turn is None or a["data"]["round"]==turn),None)
            if action is None:
                raise ValueError("该窗口或轮次没有这个主体的已提交决定")
            if action["kind"]=="decision_deferred":
                chosen=action["data"]
                for obs in reversed(self._round_observations.get((window,chosen["round"],actor_id),())):
                    if obs["id"] not in discarded and int(obs["id"][1:])<int(action["id"][1:]):
                        return {"actor_id":actor_id,"window":window,"round":chosen["round"],"rounds":rounds,
                                "deferred":True,"plan_record":chosen["plan_record"],"review_at":chosen["review_at"],
                                "observation_id":obs["id"],"deferred_id":action["id"],"observation":decision_view(self._full(obs)["data"]["visible"])}
                raise ValueError("等待轮次缺少当时的观察记录")
            def match_output(event):
                if event["kind"]!="model_output" or event["window"]!=window or event["id"] in discarded or event["data"].get("actor_id")!=actor_id:
                    return None
                output = self._full(event)
                return output if output["data"]["text"] == action["data"]["raw"] else None
            ref = action["data"].get("model_output_id")
            if ref:
                direct = self.event(ref)
                output = match_output(direct) if int(ref[1:]) < int(action["id"][1:]) else None
            else:
                output = None
                for i in range(int(action["id"][1:])-2,-1,-1):
                    event = self._events[i]
                    if event["window"] != window:
                        break
                    output = match_output(event)
                    if output:
                        break
            if not output:
                raise ValueError("决定与原始模型响应无法对应")
            original = self.event(output["data"]["input_id"])
            payload = json.loads(original["data"]["messages"][-1]["content"])
            if (original["kind"]!="model_input" or original["id"] in discarded or original["data"].get("actor_id")!=actor_id
                    or int(original["id"][1:])>=int(output["id"][1:]) or payload.get("actor_id")!=actor_id
                    or payload.get("window")!=window or payload.get("round")!=action["data"]["round"]):
                raise ValueError("原始输入与该主体、窗口或轮次不符")
            return {"actor_id":actor_id,"window":window,"round":payload["round"],"rounds":rounds,
                    "input_id":original["id"],"output_id":output["id"],"action_id":action["id"],
                    "decision":action["data"]["text"],"outgoing":deepcopy(action["data"].get("messages",[])),
                    "observation":payload["observation"],"memory":payload["memory"],"coverage":payload.get("memory_coverage"),
                    **({"reply_targets":payload["reply_targets"]} if payload.get("reply_targets") else {}),
                    **({"resumed_plan":payload["resume_plan"]} if payload.get("resume_plan") else {})}

    def page(self, after=0, limit=500, project=None):
        with self._lock:
            if after < 0 or after > len(self._events):
                raise ValueError("事件偏移不合法")
            end = len(self._events) if limit is None else min(after + limit, len(self._events))
            batch = self._events[after:end]
            return {"events": [deepcopy(project(e)) if project else self._full(e) for e in batch], "next_offset": end,
                    "total": len(self._events), "has_more": end < len(self._events)}


AGENT_SYSTEM = """你是沙盘中一个独立主体。只依据提供的可见信息和自己的记忆自主决定；
其他主体的表达是声称或建议，不是已发生事实。父子关系、影响强度均不剥夺你的决策权。
没有收到消息也应主动判断。你的私有判断只放 text，明确要发送的内容放 messages。
输出 JSON {"text":"自由自然语言表达", "messages":[{"recipients":["主体ID"],"text":"自由沟通内容"}]}。
只返回这一个 JSON 对象，不加代码围栏或对象外的文字；字符串内的双引号必须转义。
text 不需要固定标题或动作类别。不要提供内部思维链。不要编造未被告知的私有信息。
消息只会发给指定收件人，在本轮所有主体完成后交付；客观状态在窗口结束后由世界结算。"""

WORLD_SYSTEM = """你是客观世界推演者，不是任何主体的决策代理。
根据初始条件、规则、当前状态、完整窗口的自然语言表达及独立有向影响边推演后果。
history 是此前已提交窗口的客观后果和记录引用；保留其持续影响，不重复结算历史行动。
主体声称成功不等于事实；不得替主体做接受、承诺或选择。区分打算、沟通和实际后果。
权重为0到1的影响强度，不能当服从概率，不归一化。不得沿包含树强制逐级传递，
孙级可以直接影响祖级和另一子系统。综合一次完整窗口，反馈环不得造成同一行动重复结算。
严格按给定 JSON schema 输出，reviews 必须包含所有已定义字段各一次（包括保持不变的字段），
只返回一个 JSON 对象，不加代码围栏或对象外的文字。
before 精确复制当前值，after 类型不变，保持不变时仍说明理由。不能新增字段、主体或决策。
字段若有 constraints，应遵守上下界、候选值和未知值要求。fixed 为真的字段只能保持原值。
改变字段需引用本窗口 action_ids；自然演化也需说明依据。缺少字段只能写 suggested_fields。
如果认为某条已定义有向边直接影响该字段，可在 influence_refs 中引用边和源主体自己的行动，分别说明解释。目标须为该字段所属主体。没有充分依据可留空，不强行沿层级寻找路径。
consequences 用自然语言描述客观后果并引用行动。它们供观察者审阅，不自动广播私有信息。
不需要内部思维链，不决定运行何时结束。"""


class Simulation:
    def __init__(self, scenario: Scenario, config: RunConfig, gateway=None, directory: Path | None = None,
                 *, _journal=None, _run_id=None, _launch_job_id=None):
        self._scenario=(runtime_scene(scenario) if _journal is None else scenario).model_copy(deep=True)
        self.config = config.model_copy(deep=True)
        self.depths = self._scenario.depths()
        if _journal is None:
            spec=self._scenario.model_dump(mode="json")
            protect_credentials(spec,self.config.catalog.values())
            if isinstance(self._scenario,Scenario11) and isinstance(self.config,RunConfig11):
                from .mail_opening import check_opening_inputs, opening_guards
                check_opening_inputs(self._scenario,self.config,opening_guards(spec["entities"],spec["sources"]))
        if isinstance(scenario, Scenario11) != isinstance(config, RunConfig11):
            raise ValueError("场景与运行方案的版本不一致")
        if isinstance(config, RunConfig) and not set(self.depths.values()) <= config.models.layers.keys():
            raise ValueError("每个显式包含层级都必须配置模型")
        if any(isinstance(edge, Influence) and edge.weight is None for edge in self._scenario.influences):
            raise ValueError("存在未赋权的影响边；请先输入权重或显式请求世界 AI 估算")
        self.run_id = _run_id or uuid.uuid4().hex[:12]
        self.gateway = gateway or ModelGateway()
        self.journal = _journal or Journal(directory)
        self.experiment = _journal.event("e000001")["data"].get("experiment") if _journal else None
        self.mail_timing=MailTiming(self.experiment)
        self._lock = threading.RLock()
        self._runner_lock = threading.Lock()
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._state = scenario.initial_state()
        self._perception = Perception(self._scenario, self.depths, self.config)
        self._last_feedback = None
        self.window = 0
        self.round = 0
        self.status = "ready"
        self.error: str | None = None
        self._observations: dict[str, dict] = {}
        self._plans = {}
        self._wake = {}
        self._waiting_steps = []
        self._current_events = []
        self._in_transit = {}
        self._current_mail = []
        self._deferred_count = 0
        self._inbox: dict[str, list[dict]] = {a.id: [] for a in scenario.entities}
        self._actions: list[dict] = []
        self._accepted_outputs = {}
        self._committed: set[int] = set()
        self._window_marks = {}
        self._idea_cache = None
        self._history: list[dict] = []
        self._call_count = 0
        self._settlement_correction = None
        self._frozen_window = None
        self._resume_round = None
        self._resume_outputs = {}
        self._discarded = set()
        self.agents = self._make_actors()
        self.world = make_agent("world", self._world_callback, char_limit=0)
        if _journal is None:
            self.journal.append("run_created", {"run_id": self.run_id, "launch_job_id":_launch_job_id,
                "scenario": self._scenario.model_dump(mode="json"), "config": self.config.model_dump(mode="json"),
                "concordia_version": "2.4.0", "schedule_policy": "all actors observed each round; honor explicit review plans; deliver after barrier; settle once per window"
                if getattr(config,"max_review_interval",0) else "all actors once per round; deliver after barrier; settle once per window"})

    def _record_decision(self,actor,decision,event):
        view=self._observations[actor]
        ended=view.get("ended_conditions",())
        observed=self.journal.observation_before(event,self._discarded)["id"] if ended else None
        self.notebooks[actor].record(decision,event["id"],event["window"],view["messages"],self.config.recent_records,
                                    view.get("external_events",()),((observed,item) for item in ended))
        self._remember_plan(actor,decision,event["id"])

    def _remember_plan(self, actor, decision, record):
        due=(item["due_window"] for item in self.notebooks[actor].open.values() if "due_window" in item)
        plan=ReviewPlan.from_decision(decision,record,self._observations[actor],self.config.max_review_interval,due)
        if plan:
            self._plans[actor]=plan
        else:
            self._plans.pop(actor,None)

    def _make_actors(self):
        actors = {}
        self.notebooks = {}
        for actor in self._scenario.entities:
            actor_id = actor.id
            key = self.config.model_id("actor", self.depths[actor_id])
            limit = self.config.catalog[key].context_limit_chars if self.config.memory_policy == "budgeted" else None
            if isinstance(self.config, RunConfig11):
                self.notebooks[actor_id] = Notebook()
                limit = 0
            actors[actor_id] = make_agent(actor_id,self._actor_callback(actor_id),limit)
        return actors

    @classmethod
    def restore(cls, directory: Path, gateway=None):
        if writer_active(Path(directory) / ".running.lock"):
            raise ValueError("另一进程正在推演，请在其窗口暂停或运行结束后重新打开")
        journal = Journal.open(Path(directory))
        events = journal.selected(transport_details=False)
        created = events[0]["data"]
        simulation = cls(load_scenario(created["scenario"]), load_config(created["config"]),
                         gateway, _journal=journal, _run_id=created["run_id"])
        verify_commits(simulation._scenario, simulation.config, events)
        simulation._restore_boundary(events)
        last_status = next((event["data"] for event in reversed(events) if event["kind"] == "run_status"), {})
        lifecycle = next((event["kind"] for event in reversed(events) if event["kind"] in {"run_started", "run_status", "stop_requested", "run_recovered"}), None)
        if simulation.window == simulation.config.windows:
            simulation.status = "completed"
        elif lifecycle == "stop_requested":
            simulation.status = "stopped"
        elif lifecycle == "run_status" and last_status.get("status") in {"paused", "stopped", "failed"}:
            simulation.status = last_status["status"]
            simulation.error = last_status.get("error")
        else:
            simulation.status = "interrupted"
            simulation.error = "服务已重启，运行停留在最后一次提交。可显式从该窗口继续；未提交窗口会重新调用模型。"
        return simulation

    def _restore_boundary(self, events, *, frozen=False, resuming_round=None):
        commits = [event for event in events if event["kind"] == "window_committed"]
        cutoff = len(events) if frozen else events.index(commits[-1]) + 1 if commits else 1
        discarded = {eid for event in events if event["kind"] == "run_recovered"
                     for eid in event["data"].get("discarded_event_ids", [])}
        self._discarded = discarded
        self._state = deepcopy(commits[-1]["data"]["state"]) if commits else self._scenario.initial_state()
        self.window = commits[-1]["window"] if commits else 0
        self.round = self.config.rounds_per_window if frozen else 0
        self._frozen_window = self.window + 1 if frozen else None
        self._resume_round = None
        self._resume_outputs = {}
        self._committed = {event["window"] for event in commits}
        self._last_feedback = ({"window":self.window, "commit_id":commits[-1]["id"], "changes":commits[-1]["data"]["changes"]}
                               if commits else None)
        self._history = [{"id": e["id"], "window": e["window"], "consequences": deepcopy(e["data"]["consequences"])} for e in commits]
        inherited = int(self.experiment["parent_event"][1:]) if self.experiment else 0
        self._call_count = sum(e["kind"] == "model_input" for e in events[inherited:])
        self._deferred_count = sum(e["data"].get("deferred_reviews",0) for e in events[inherited:] if e["kind"]=="window_committed")
        if [e["window"] for e in commits] != list(range(1, self.window + 1)) or self.window > self.config.windows:
            raise ValueError("已提交窗口的顺序或数量不合法，无法继续")
        self._inbox = {actor.id: [] for actor in self._scenario.entities}
        self._perception.documents.reset()
        self.agents = self._make_actors()
        memories = {key:agent.get_component("memory",type_=Memory) for key,agent in self.agents.items()}
        self._observations, self._actions = {}, []
        self._plans, self._wake, self._waiting_steps = {}, {}, []
        self._current_events=[]
        self._in_transit, self._current_mail = {}, []
        for event in events[:cutoff]:
            if event["id"] in discarded:
                continue
            data = event["data"]
            if event["kind"] == "observation":
                actor_id = data["actor_id"]
                if not (event["window"]==self._frozen_window and data["visible"]["round"]==resuming_round):
                    memories[actor_id].remember("observation", encode(data["visible"]))
                self._inbox[actor_id] = []
                self._observations[actor_id] = deepcopy(data["visible"])
            elif event["kind"] == "expression":
                memories[data["actor_id"]].remember("own_expression",data["raw"])
                if frozen and event["window"] == self._frozen_window:
                    self._actions.append({"id":event["id"], **deepcopy(data)})
                if self.notebooks:
                    owner = data["actor_id"]
                    decision=Decision.model_validate_json(data["raw"])
                    self._record_decision(owner,decision,event)
            elif event["kind"]=="decision_deferred" and frozen and event["window"]==self._frozen_window:
                self._waiting_steps.append({"actor_id":data["actor_id"],"round":data["round"]})
            elif event["kind"]=="external_event" and frozen and event["window"]==self._frozen_window:
                self._current_events.append(event)
            elif event["kind"]=="message_dispatched":
                self._in_transit[event["id"]]=event
                if frozen and event["window"]==self._frozen_window:
                    self._current_mail.append(event)
            elif event["kind"] == "message_delivered":
                if ref:=data.get("dispatch_id"):
                    self._in_transit.pop(ref)
                if (ref or data.get("enclosures")) and frozen and event["window"]==self._frozen_window:
                    self._current_mail.append(event)
                self._inbox[data["to"]].append(deepcopy(self._perception.documents.received(event)))
        # Rebuild Concordia entities at their idle phase, then restore only their
        # own observations and expressions. No world/private cross-contamination.
        self.world = make_agent("world", self._world_callback, char_limit=0)
        return [event["id"] for event in events[cutoff:] if event["id"] not in discarded and event["kind"] in
                {"observation","expression","decision_deferred","external_event","message_dispatched","message_delivered","model_input","model_output","model_validation_error","model_failure"}]

    def recover(self):
        with self._lock:
            if self.status not in {"failed", "interrupted"}:
                raise ValueError("只有失败或中断的运行需要恢复")
            events = self.journal.selected(transport_details=False)
            from .round_recovery import resume_round
            if not self._resume_settlement(events) and not resume_round(self,events):
                discarded = self._restore_boundary(events)
                self.journal.append("run_recovered", {"from_window": self.window,
                    "discarded_event_ids": discarded, "reason": "用户显式从已提交窗口继续；未提交尝试保留为历史证据，不纳入主体记忆或重复结算。"}, self.window)
                self._discarded.update(discarded)
            self._pause.clear()
            self._stop.clear()
            self.error = None
            self.status = "paused"

    def _resume_settlement(self, events):
        if not isinstance(self.config, RunConfig11):
            return False
        boundary = max((i for i,e in enumerate(events) if e["kind"] in {"window_committed","run_recovered"}), default=0)
        tail = events[boundary+1:]
        rejected = next((e for e in reversed(tail) if e["kind"] == "settlement_rejected"), None)
        world_input = next((e for e in reversed(tail) if e["kind"]=="model_input" and e["data"]["purpose"]=="world"), None)
        if not world_input or not verify_commits(self._scenario, self.config, events)["pending_actions_complete"]:
            return False
        self._restore_boundary(events, frozen=True)
        owners = {a["id"]:a["actor_id"] for a in self._actions}
        receipt = {"world_input_id":world_input["id"],"frozen_action_ids":list(owners),
                   "reason":"完整行动与投递已经重放核对，重新请求尚未提交的世界结算。"}
        if rejected:
            prior = next((e for e in reversed(tail) if e["kind"]=="model_output" and int(e["id"][1:]) < int(rejected["id"][1:])
                          and e["data"]["purpose"]=="world"),None)
            if prior is None:
                raise ValueError("被拒的结算缺少原始世界响应，无法安全重试")
            raw = self.journal.event(prior["id"])["data"]["text"]
            proposal = Settlement.model_validate_json(raw)
            issue = rejected["data"]["reason"]
            try:
                proposal=complete_scheduled_reviews(self._state,proposal,self._current_events,self._scenario)
                settled_state(self._scenario, self._state, proposal, set(owners), owners,self._objective_receipts(),window=self.window+1)
            except ValueError as error:
                issue = str(error)
            self._settlement_correction = {"rejected_proposal":raw,"issue":issue,
                                           "instruction":"保持本窗口行动原文和收件记录，重新提交一份完整的合法结算。"}
            receipt.update(rejection_id=rejected["id"],proposal_id=prior["id"],reason=issue)
        self.journal.append("settlement_resume_requested",receipt,self.window + 1)
        return True

    @property
    def scenario(self):
        return self._scenario.model_copy(deep=True)

    @property
    def state(self):
        with self._lock:
            return deepcopy(self._state)

    def snapshot(self, known_window=None):
        with self._lock:
            result = {"run_id": self.run_id, "status": self.status, "window": self.window,
                    "round": self.round, "windows": self.config.windows,
                    "window_duration": self.config.window_duration, "error": self.error,
                    "model_calls": self._call_count, "model_call_limit": self.config.model_call_limit,
                    "can_resume": self.status in {"paused", "failed", "interrupted"} and self.window < self.config.windows
                                  and (self.config.model_call_limit is None or self._call_count < self.config.model_call_limit),
                    "pause_requested": self._pause.is_set(), "stop_requested": self._stop.is_set()}
            if self.experiment:
                result["experiment"] = deepcopy(self.experiment)
            if getattr(self.config,"max_review_interval",0):
                result.update(max_review_interval=self.config.max_review_interval,deferred_reviews=self._deferred_count)
            if self.window != known_window:
                result.update(state=deepcopy(self._state),depths=dict(self.depths))
            if self.status in {"failed","interrupted"} and (pending:=self.journal.pending_round(self.window+1,self._discarded)):
                result["round_recovery"]={"round":pending["data"]["round"],"saved":len(pending["data"]["outputs"]),
                                          "failed":len(pending["data"]["failed"]),"record":pending["id"]}
            return result

    def event_status(self, event):
        with self._lock:
            if event["id"] in self._discarded:
                return "discarded"
            if event["kind"] == "settlement_rejected":
                return "rejected"
            if event["kind"] in {"observation","expression","decision_deferred","external_event","message_dispatched","message_delivered","model_input","model_output"}:
                return "committed" if event["window"] in self._committed else "uncommitted"
            return None

    def report_snapshot(self, kinds=None):
        # Share the lifecycle/commit lock only while copying. The report model
        # can run after resume without mixing its evidence with later state.
        with self._lock:
            if self.status in ("running", "ready"):
                raise ValueError("请先完成、暂停或结束一次推演")
            status = self.snapshot()
            if isinstance(self.config,RunConfig11) and self.status in {"completed","stopped","paused"}:
                status["round"] = 0  # A closed window has no active round, including after reopening the archive.
            return status, self.journal.selected(kinds) if kinds is not None else self.journal.snapshot()

    def _call(self, config_id: str, purpose: str, payload: dict, system: str,
              actor_id: str | None = None, schema=None):
        retries = self.config.output_retries if isinstance(self.config, RunConfig11) else 2
        for attempt in range(retries + 1):
            instruction = system
            if attempt:
                instruction += "\noutput_correction 给出你上一条响应及格式错误。只修正 JSON 和 schema 错误，保留原有表达含义，不新增行动；返回完整的单个合法 JSON 对象。"
            messages = [{"role": "system", "content": instruction}, {"role": "user", "content": encode(payload)}]
            protect_credentials(messages,self.config.catalog.values())
            from .round_recovery import reusable_output
            output=reusable_output(self,actor_id,config_id,messages) if purpose=="agent" and not attempt else None
            reused=output is not None
            if output:
                raw=output["data"]["text"]
            else:
                with self._lock:
                    limit = self.config.model_call_limit
                    if limit is not None and self._call_count >= limit:
                        raise ModelError(f"本场主体和世界模型调用已达到上限 {limit}。未提交的窗口仍保留记录。")
                    before = self.journal.append("model_input", {"purpose": purpose, "actor_id": actor_id,
                        "config_id": config_id, "messages": messages}, self.window + 1)
                    self._call_count += 1
                raw = self.call_recorded(config_id, messages, before)
                output = self.journal.append("model_output", {"purpose": purpose, "actor_id": actor_id,
                    "config_id": config_id, "input_id": before["id"], "text": raw}, self.window + 1)
            if schema is None:
                return raw

            try:
                result = schema.model_validate_json(raw)
                if schema is Decision:
                    validate_outbox(result,self._perception.delivery_limits)
                    if any(not set(m.recipients)<=self.agents.keys() for m in result.messages):
                        raise ValueError("主体消息的收件人必须在本轮主体名单中")
                    for message in result.messages:
                        validate_requested_time(message.reply_by,self.window+1)
                    self._perception.documents.validate(result,self._observations[actor_id])
                    if any(m.reply_to for m in result.messages):
                        obs=self.journal.round_observation(actor_id,self.window+1,self.round,self._discarded)
                        self.journal.validate_replies(result,actor_id,obs["id"],self._discarded)
            except ValueError as error:
                issues = [{key: issue[key] for key in ("type", "loc", "msg")}
                          for issue in error.errors(include_input=False, include_url=False)] if isinstance(error,ValidationError) else [
                              {"type":"recipient_scope","loc":["messages"],"msg":str(error)}]
                self.journal.append("model_validation_error", {"purpose": purpose, "actor_id": actor_id,
                    "output_id": output["id"], "attempt": attempt + 1, "issues": issues}, self.window + 1)
                if attempt == retries:
                    if not isinstance(error,ValidationError):
                        raise ModelError(f"主体 {actor_id} 的收件范围校验未通过，原消息未投递。{error}") from None
                    raise
                payload = {**payload, "response_schema": schema.model_json_schema(),
                           "output_correction": {"previous_response": raw, "issues": issues}}
            else:
                if purpose == "agent":
                    with self._lock:
                        self._accepted_outputs[actor_id] = output["id"]
                        if reused:
                            self.journal.append("agent_result_reused",{"actor_id":actor_id,"round":self.round,"output_id":output["id"],
                                                "input_id":output["data"]["input_id"]},self.window+1)
                return raw

    def call_recorded(self, config_id, messages, input_event, gateway=None):
        def receive(receipt):
            self.journal.append("provider_attempt", {**receipt, "input_id": input_event["id"], "config_id": config_id,
                                "purpose": input_event["data"].get("purpose", input_event["kind"])},
                                input_event["window"])
        cancel_retry = self._stop if input_event["kind"] == "model_input" else None
        raw = measured_call(gateway or self.gateway, self.config.catalog[config_id], messages, receive, cancel_retry)
        protect_credentials(raw,self.config.catalog.values())
        return raw

    def _actor_callback(self, actor_id):
        def callback(memory):
            config_id = self.config.model_id("actor", self.depths[actor_id])
            if self.notebooks:
                obs=self.journal.round_observation(actor_id,self.window+1,self.round,self._discarded)
                visible,recent=self.journal.reply_mail(obs,self._discarded,recent_windows=self.config.history_windows)
                payload = self.notebooks[actor_id].payload(visible,self.config,self._wake.get(actor_id),recent)
                instruction=actor_instruction(self.config,bool(visible.get("documents")),bool(visible["messages"] or payload.get("reply_targets")))
                return self._call(config_id,"agent",payload,instruction,actor_id,Decision)
            total = memory["total_items"] if isinstance(memory,dict) else len(memory)
            payload = {"task": "agent", "actor_id": actor_id, "window": self.window + 1, "round": self.round,
                       "observation": self._observations[actor_id], "memory": memory["items"] if isinstance(memory,dict) else memory}
            if self.config.memory_policy == "budgeted":
                self._fit_private_memory(payload, self.config.catalog[config_id].context_limit_chars,total)
            return self._call(config_id, "agent", payload, AGENT_SYSTEM, actor_id, Expression)
        return callback

    @staticmethod
    def _fit_private_memory(payload, limit, total=None):
        memory = payload["memory"]
        total = len(memory) if total is None else total
        if total == len(memory) and len(AGENT_SYSTEM) + len(encode(payload)) <= limit:
            return
        # Concordia records the current observation before asking for an action.
        # It is already included verbatim in observation. Retain older records
        # as whole items, without borrowing from any other entity's memory.
        candidates = memory[:-1] if memory and memory[-1]["kind"] == "observation" else memory
        payload["memory"] = []
        coverage = {"policy": "budgeted", "total_items": total, "retained_items": 0,
                    "note": "当前观察单列。历史按时间保留最近能容纳的连续完整记录，较早记录仍在运行日志。"}
        payload["memory_coverage"] = coverage
        minimum = len(AGENT_SYSTEM) + len(encode(payload)) + len(str(len(memory)))
        available = limit - minimum
        if available < 0:
            raise ModelError(f"当前主体的完整观察及必要说明需 {minimum} 字符，配置限额为 {limit}。请缩减场景或提高所选模型的字符限额")
        selected = []
        for item in reversed(candidates):
            size = len(encode(item)) + (2 if selected else 0)
            if size > available:
                break
            selected.append(item)
            available -= size
        payload["memory"] = list(reversed(selected))
        coverage["retained_items"] = len(selected)

    def _world_callback(self, memory):
        with self._lock:
            # Raw world memory includes proposals that may have failed commit
            # validation. Only the commit journal establishes objective facts.
            if self.notebooks:
                payload = objective_input(self._scenario, self.config, self._state, self._actions, self._history,
                                          self.window+1,Settlement.model_json_schema(),self._current_events)
                if routes:=self._perception.known_routes(None,self.window+1):
                    payload["mail_routes"]=routes
                if self._current_mail:
                    payload["mail"]=[]
                    for e in self._current_mail:
                        d=e["data"]
                        row={k:d[k] for k in ("from","to","text","sent_window","due_window","arrived_window") if k in d}
                        if d.get("enclosures"):
                            row["enclosures"]=[{k:doc[k] for k in ("id","title","origin","sha256")} for doc in d["enclosures"]]
                        if e["kind"]=="message_delivered":
                            row.update(status="arrived",record=e["id"])
                        else:
                            row.update(status="in_transit",source_action=d["action_id"])
                        payload["mail"].append(row)
                    payload["this_window_refs"]={"action_ids":[a["id"] for a in self._actions],
                                                 "event_ids":[e["id"] for e in self._objective_receipts()]}
            else:
                payload = {"task": "world", "window": self.window + 1,
                    "window_duration": self.config.window_duration, "scenario": self._scenario.world_view(self.config.world_source_policy == "defined"),
                    "state": deepcopy(self._state), "actions": self._actions, "history": deepcopy(self._history),
                    "response_schema": Settlement.model_json_schema()}
            if self._settlement_correction:
                payload["settlement_correction"] = self._settlement_correction
            if self._waiting_steps:
                payload["continued_waits"] = self._waiting_steps
        instruction=world_instruction(payload["allowed_direct_actions"]) if self.notebooks else WORLD_SYSTEM
        if "review_fields" in payload:
            instruction+="\nreview_fields 按所有者列出本窗请你审查的确切字段。名单外不写 reviews。固定字段由引擎保持，即使主体表态想改也不能记为已经改变。"
        if (self._settlement_correction or {}).get("issue","").startswith("直接影响引用"):
            instruction+="\n上次未通过的是可选的 influence_refs。保持原值或只是确认收到了先前寄出的信时，在该字段上省略 influence_refs，只保留当窗的 action_ids 和实际到件的 event_ids。禁止把早先的寄件重新算成当窗直接行动。"
        if payload.get("mail_routes"):
            instruction+="\nmail_routes 是本窗口权威的通信时效，优先于旧材料里的预计送达时间。核对实际到件记录，再区分信中内容与收件人自己的决定。"
        if self._waiting_steps:
            instruction+="\ncontinued_waits 只表示主体延续此前的等待。本轮没有由此产生的新承诺、行动或消息。结算可保持原值。"
        if payload.get("ended_conditions"):
            instruction+="\nended_conditions 从本窗起不再生效。保留已结算的后果，不因条件到期自动撤回既有安排或替主体作出新决定。"
        if self.notebooks and self._scenario.capacity_pools:
            instruction+="\ncapacity_pools 是按本窗所有字段结算后的共享上限。limit 是容量，members 是实际已配置的占用，合计不能超额。未能安排的申请只记为意向，不能当作新增容量或擅自替主体选择。"
        if self._current_events:
            instruction+="\nexternal_events 是本窗实际发生的预定外部事件。assignments 指定的字段由引擎直接审查，请省略这些字段的 reviews。外部事实可用 event_ids 引用其 record，主体行动仍用 action_ids。"
        elif payload.get("calendar_window"):
            instruction+="\n本窗没有新的日历赋值。预计日期或历史通知不能触发预定赋值。其他字段仍依据原规则和本窗主体实际完成的行动独立审查。"
        if self._current_mail:
            instruction+="\nmail 中 in_transit 表示刚寄出，不能按送达结算。arrived 才是本窗实际送达。用其 record 作 event_ids 引用，用收件人本窗的决定作 action_ids 引用。每处引用都必须在 this_window_refs 的对应列表内，勿复制信里提及的旧行动编号。"
        if self.notebooks:
            room=self.config.model.context_limit_chars-len(instruction)-len(DECISION_EVIDENCE_INSTRUCTION)-len(encode(payload))-120
            if room>=250:
                add_decision_inbox(payload,self.journal,self._discarded,min(14000,room))
                if payload.get("decision_inbox"):instruction+=DECISION_EVIDENCE_INSTRUCTION
        return self._call(self.config.model_id("world"), "world", payload,instruction,schema=Settlement)

    def observation(self, actor_id: str) -> dict:
        return self._perception.observe(actor_id,self._state,self._inbox[actor_id],self.window+1,self.round,self._last_feedback,self._current_events)

    def _objective_receipts(self):
        return self._current_events+[e for e in self._current_mail if e["kind"]=="message_delivered"]

    def _send_message(self, sender, recipient, message, action_id):
        window=self.window+1
        data={"from":sender,"to":recipient,"text":message.text,"action_id":action_id}
        if ref:=getattr(message,"reply_to",None):data["reply_to"]=ref
        if due:=getattr(message,"reply_by",None):data["reply_by"]=due
        if keys:=getattr(message,"enclosures",()):
            data["enclosures"]=self._perception.documents.envelope(keys)
        if scope:=envelope_scope(data,self._perception.delivery_limits):
            data["sharing"]=scope
        delay=self._perception.transit(sender,recipient,window)
        if delay:
            data.update(sent_window=window,sent_round=self.round,due_window=window+delay)
            dispatch=self.journal.append("message_dispatched",data,window)
            self._in_transit[dispatch["id"]]=dispatch
            self._current_mail.append(dispatch)
        else:
            receipt=self.journal.append("message_delivered",data,window)
            if data.get("enclosures"):
                self._current_mail.append(receipt)
            self._inbox[recipient].append(self._perception.documents.received(receipt))

    def _collect_actions(self):
        resumed=self._resume_round
        self._resume_round=None
        if resumed is None:
            self._actions = []
            self._waiting_steps = []
            self._current_events=[self.journal.append("external_event",event.model_dump(mode="json"),self.window+1)
                                  for event in self._perception.calendar.get(self.window+1,())]
            self._current_mail=[]
            due=[e for e in self._in_transit.values() if self.mail_timing.due(e,self.window)==self.window+1]
            for dispatch in due:
                data=self.mail_timing.receipt(dispatch,self.window+1)
                receipt=self.journal.append("message_delivered",data,self.window+1)
                self._current_mail.append(receipt)
                self._inbox[data["to"]].append(self._perception.documents.received(receipt))
                del self._in_transit[dispatch["id"]]
        for round_index in range(resumed or 1, self.config.rounds_per_window + 1):
            self.round = round_index
            due, deferred = {}, {}
            self._wake = {}
            for actor_id, agent in self.agents.items():
                if round_index==resumed:
                    observation=self._observations[actor_id]
                else:
                    observation = self.observation(actor_id)
                    self._observations[actor_id] = observation
                    self.journal.append("observation", {"actor_id": actor_id, "visible": observation}, self.window + 1)
                agent.observe(encode(observation))
                self._inbox[actor_id] = []
                plan=self._plans.get(actor_id)
                reason=plan.wake_reason(observation) if plan else None
                if plan and reason is None:
                    deferred[actor_id]=plan
                else:
                    due[actor_id]=agent
                    if plan:
                        self._wake[actor_id]={"reason":reason,"plan_record":plan.record,"review_at":plan.review_at}
            outputs, errors = act_together(due, self.config.max_parallel) if due else ({},{})
            # Do not settle a partial batch or silently replace a failed actor.
            if errors:
                if self.notebooks:
                    self.journal.append("round_pending",{"round":self.round,
                        "outputs":{actor:self._accepted_outputs[actor] for actor in outputs},
                        "failed":sorted(errors),"waiting":sorted(deferred)},self.window+1)
                details = {actor_id: str(error) if isinstance(error, ModelError) else type(error).__name__
                           for actor_id, error in errors.items()}
                self.journal.append("model_failure", {"errors": details}, self.window + 1)
                if any(isinstance(error, RetryStopped) for error in errors.values()):
                    raise RetryStopped("已按结束请求停止本窗口的网络重试")
                raise ModelError("主体调用失败，当前窗口未结算: " + "; ".join(f"{key}: {value}" for key, value in sorted(details.items())))
            schema = Decision if self.notebooks else Expression
            expressions = {key: schema.model_validate_json(raw) for key, raw in outputs.items()}
            for expression in expressions.values():
                for message in expression.messages:
                    if not set(message.recipients) <= self.agents.keys():
                        raise ValueError("主体消息包含不存在的收件人")
            # Stable record order, independent of HTTP completion speed.
            for actor_id in self.agents:
                if actor_id in deferred:
                    plan=deferred[actor_id]
                    step={"actor_id":actor_id,"round":self.round}
                    self.journal.append("decision_deferred",{**step,"plan_record":plan.record,"review_at":plan.review_at},self.window+1)
                    self._waiting_steps.append(step)
                    continue
                expression = expressions[actor_id]
                next_review=None
                if self.notebooks and self.config.max_review_interval and expression.review_at:
                    prior={k:m["due_window"] for k,m in self.notebooks[actor_id].open.items() if "due_window" in m}
                    due=revised_deadlines(prior,expression.matters)
                    next_review=ReviewPlan.next_window(expression,self.window+1,self.config.max_review_interval,due.values())
                event = self.journal.append("expression", {"actor_id": actor_id, "round": self.round,
                    "text": expression.text, "messages": [m.model_dump(exclude_defaults=True) for m in expression.messages],
                    **({"model_output_id":self._accepted_outputs[actor_id]} if self.notebooks else {}),
                    **({"resumed_plan":self._wake[actor_id]} if actor_id in self._wake else {}),
                    **({"next_review_window":next_review} if next_review else {}),
                    "raw": outputs[actor_id]}, self.window + 1)
                action = {"id": event["id"], **event["data"]}
                self._actions.append(action)
                if self.notebooks:
                    self._record_decision(actor_id,expression,event)
                for message in expression.messages:
                    for recipient in dict.fromkeys(message.recipients):
                        self._send_message(actor_id,recipient,message,event["id"])
    def _play_window(self):
        if self._frozen_window != self.window + 1:
            self._collect_actions()
        # World context is a full objective snapshot each window, never a source
        # of automatically broadcast private prose.
        self.world.observe(encode({"window": self.window + 1, "action_ids": [a["id"] for a in self._actions]}))
        action_ids = {a["id"] for a in self._actions}
        try:
            for attempt in range(self.config.settlement_retries + 1):
                raw = resolve(self.world)
                proposal = Settlement.model_validate_json(raw)
                try:
                    self.commit(self.window + 1, proposal, action_ids)
                    self._frozen_window = None
                    return
                except InvalidSettlement as error:
                    self.journal.append("settlement_rejected", {"reason": str(error), "attempt": attempt + 1}, self.window + 1)
                    if attempt == self.config.settlement_retries:
                        raise
                    self._settlement_correction = {"rejected_proposal": raw, "issue": str(error),
                        "instruction": "基于同一批已记录行动修正提案。被拒结果未生效，不得改写主体的行动。"}
        finally:
            self._settlement_correction = None

    def commit(self, window: int, proposal: Settlement, action_ids: set[str]):
        with self._lock:
            if window in self._committed or window != self.window + 1:
                raise ValueError("窗口已经结算或顺序不正确")
            try:
                owners = {action["id"]:action["actor_id"] for action in self._actions}
                proposal=complete_scheduled_reviews(self._state,proposal,self._current_events,self._scenario)
                next_state, changes = settled_state(self._scenario,self._state,proposal,action_ids,owners,self._objective_receipts(),window=window)
            except ValueError as error:
                raise InvalidSettlement(str(error)) from error
            # Persist the complete commit record before publishing the new state.
            event = self.journal.append("window_committed", {"reviews": [r.model_dump(mode="json") for r in proposal.reviews],
                "changes": changes, "consequences": [c.model_dump() for c in proposal.consequences],
                "suggested_fields": proposal.suggested_fields,
                **({"deferred_reviews":len(self._waiting_steps)} if self._waiting_steps else {}),
                "state": next_state, "logical_time": {"elapsed_windows": window, "duration_per_window": self.config.window_duration}}, window)
            self._state = next_state
            self._committed.add(window)
            self._deferred_count+=len(self._waiting_steps)
            self._waiting_steps=[]
            self._current_events=[]
            self._current_mail=[]
            self._last_feedback = {"window":window, "commit_id":event["id"], "changes":changes}
            self._history.append({"id": event["id"], "window": window, "consequences": event["data"]["consequences"]})
            self.window = window

    def request_pause(self):
        with self._lock:
            self._pause.set()
            self.journal.append("pause_requested", {"effective": "next window boundary"}, self.window)

    def request_stop(self):
        with self._lock:
            if self.status not in {"ready", "running", "paused", "interrupted", "failed"}:
                raise ValueError("运行已经结束")
            self._stop.set()
            self.journal.append("stop_requested", {"effective": "next window boundary"}, self.window)
            if self.status != "running":
                self.status = "stopped"
                self.journal.append("run_status", self.snapshot(), self.window)

    def run(self, *, pause_at_boundary=None):
        if not self._runner_lock.acquire(blocking=False):
            raise ValueError("已有运行中的调度器")
        try:
            directory = self.journal.directory
            with writer_lock(directory / ".running.lock") if directory else nullcontext():
                return self._run_windows(pause_at_boundary)
        finally:
            self._runner_lock.release()

    def _run_windows(self, pause_at_boundary=None):
        with self._lock:
            if self.status not in ("ready", "paused"):
                raise ValueError("只能启动未运行或已暂停的运行")
            if self.status == "paused":
                self._pause.clear()
            self.journal.append("run_started", {}, self.window)
            self.status = "running"
        try:
            while (self.window < self.config.windows and not self._stop.is_set() and not self._pause.is_set()
                   and not (pause_at_boundary and pause_at_boundary.is_set())):
                self._play_window()
            with self._lock:
                self.status = "completed" if self.window == self.config.windows else "stopped" if self._stop.is_set() else "paused"
                self.journal.append("run_status", self.snapshot(), self.window)
        except RetryStopped as error:
            with self._lock:
                self.status = "stopped"
                self.journal.append("run_aborted", {"reason":str(error), "uncommitted_window":self.window + 1}, self.window + 1)
                self.journal.append("run_status", self.snapshot(), self.window)
        except Exception as error:
            with self._lock:
                self.status = "failed"
                # Pydantic errors can echo whole model output; record original
                # outputs separately and expose a concise operational failure.
                self.error = str(error) if isinstance(error, (ModelError, InvalidSettlement)) else f"{type(error).__name__}: 当前窗口的模型输出或结算未通过校验"
                self.journal.append("run_error", {"message": self.error, "uncommitted_window": self.window + 1}, self.window + 1)
                self.journal.append("run_status", self.snapshot(), self.window)
        return self.snapshot()
