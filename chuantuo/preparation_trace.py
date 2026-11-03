"""Audit model preparation and explicitly reuse previously accepted, byte-matched steps."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from .engine import Journal
from .models import ModelError, measured_call, protect_credentials


def _auditable(messages):
    snapshot = []
    for message in messages:
        content = message["content"]
        if isinstance(content,list):
            blocks = []
            for block in content:
                if block["type"] != "image_url":
                    blocks.append(dict(block))
                    continue
                header, encoded = block["image_url"]["url"].split(",",1)
                raw = base64.b64decode(encoded,validate=True)
                blocks.append({"type":"image_reference","mime":header[5:].split(";")[0],
                               "bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
            content = blocks
        snapshot.append({"role":message["role"],"content":content})
    return snapshot


def accepted(gateway):
    if isinstance(gateway, PreparationGateway):
        gateway.accept()


class PreparationGateway:
    def __init__(self, gateway, directory:Path, job_id:str, parent=None):
        self.gateway = gateway
        self.job_id = job_id
        self.parent_id = parent
        self.journal = Journal(directory/job_id)
        self.journal.append("preparation_created",{"job_id":job_id,"parent_job":parent})
        self.parent = Journal.open(directory/parent,first_kind="preparation_created") if parent and (directory/parent/"events.jsonl").is_file() else None
        records = self.parent.snapshot({"prepare_accepted"}) if self.parent else []
        self.approved = {e["data"]["key"]:e["data"] for e in records}
        self.candidates = {}
        if self.parent:
            for event in self.parent.snapshot({"prepare_output"}):
                previous = self.parent.event(event["data"]["input_id"])
                if previous["kind"] == "prepare_input":
                    key = previous["data"]["key"]
                    self.candidates[key] = {"input_id":previous["id"],"output_id":event["id"],
                                            "raw_sha256":hashlib.sha256(event["data"]["text"].encode()).hexdigest()}
        self.last = None
        self.calls = self.reused = self.rechecked = 0

    def complete(self, config, messages):
        return self._response(config, messages)

    def recheck(self, config, messages):
        return self._response(config, messages, candidate_only=True) if self.parent else None

    def _response(self, config, messages, candidate_only=False):
        safe = _auditable(messages)
        protect_credentials(safe,[config])
        content = safe[-1]["content"]
        payload = json.loads(content if isinstance(content,str) else content[0]["text"])
        settings=config.model_dump(mode="json")
        canonical=json.dumps({"model":settings,"messages":safe},ensure_ascii=False,sort_keys=True,allow_nan=False)
        key=hashlib.sha256(canonical.encode()).hexdigest()
        if candidate_only and (key in self.approved or key not in self.candidates):
            return None
        self.last=None
        before=self.journal.append("prepare_input",{"key":key,"task":payload.get("task"),"config":settings,"messages":safe})
        prior = self.candidates.get(key) if candidate_only else self.approved.get(key)
        if prior:
            previous = self.parent.event(prior["input_id"])
            output = self.parent.event(prior["output_id"])
            if (previous["kind"]!="prepare_input" or previous["data"]["messages"]!=safe or previous["data"]["config"]!=settings
                    or output["kind"]!="prepare_output" or output["data"]["input_id"]!=previous["id"]
                    or hashlib.sha256(output["data"]["text"].encode()).hexdigest()!=prior["raw_sha256"]):
                raise ValueError("上次准备记录无法通过原始输入输出核对，已停止复用")
            raw = output["data"]["text"]
            origin = {"job_id":self.parent_id,"output_id":output["id"]}
            if candidate_only:
                origin["recheck"] = True
                self.rechecked += 1
            else:
                self.reused += 1
        else:
            origin = None
            self.calls += 1
            def receive(receipt):
                self.journal.append("prepare_provider_attempt",{"input_id":before["id"],**receipt})
            try:
                raw = measured_call(self.gateway,config,messages,receive)
            except Exception as error:
                self.journal.append("prepare_failure",{"input_id":before["id"],"error":str(error) if isinstance(error,ModelError) else type(error).__name__})
                raise
        protect_credentials(raw,[config])
        output=self.journal.append("prepare_output",{"input_id":before["id"],"text":raw,"reused_from":origin})
        self.last={"key":key,"input_id":before["id"],"output_id":output["id"],"raw_sha256":hashlib.sha256(raw.encode()).hexdigest()}
        return raw

    def accept(self):
        if self.last:
            self.journal.append("prepare_accepted",self.last)
            self.last=None
