"""Explicitly reuse a parent's first-round choices only for identical task inputs."""
import hashlib
import json

from .engine import encode
from .models import ModelError, protect_credentials
from .schema import Decision


FILENAME = "first-round-decisions.json"
MAX_BYTES = 16 * 1024 * 1024


def fingerprint(model, messages):
    payload = {"model": model.model_dump(mode="json"), "messages": messages}
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


def reference_fingerprint(event):
    payload = [event["kind"], event["window"], event["data"]]
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


def freeze_choices(parent, window, directory):
    entries = {}
    size = 256
    expressions, _ = parent.journal.window_rows(window, 0, kind="expression")
    for expression in expressions:
        data = expression["data"]
        output_id = data.get("model_output_id")
        if data["round"] != 1 or not output_id or parent.event_status(expression) != "committed":
            continue
        output = parent.journal.event(output_id)
        request = parent.journal.event(output["data"]["input_id"])
        actor = data["actor_id"]
        if (request["kind"] != "model_input" or output["kind"] != "model_output"
                or request["data"].get("actor_id") != actor or output["data"].get("actor_id") != actor
                or request["window"] != window or output["window"] != window
                or request["data"].get("purpose") != "agent"):
            raise ValueError("原判断未能对应到其主体和输入记录")
        model = parent.config.catalog[request["data"]["config_id"]]
        raw = output["data"]["text"]
        protect_credentials(raw, parent.config.catalog.values())
        try:
            decision = Decision.model_validate_json(raw)
        except ValueError:
            continue
        references = {m.reply_to: reference_fingerprint(parent.journal.event(m.reply_to)) for m in decision.messages if m.reply_to}
        entry = {"fingerprint": fingerprint(model, request["data"]["messages"]), "text": raw,
                 "input_id": request["id"], "output_id": output["id"], "action_id": expression["id"],
                 "references": references}
        size += len(actor.encode("utf-8")) + len(encode(entry).encode("utf-8")) + 6
        if size > MAX_BYTES:
            raise ValueError("首轮判断记录超过保存大小，请选择独立判断")
        entries[actor] = entry
    body = encode({"version": 1, "parent_id": parent.run_id, "window": window, "entries": entries}).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("首轮判断记录超过保存大小，请选择独立判断")
    with (directory / FILENAME).open("xb") as stream:
        stream.write(body)
    return {"version": 1, "sha256": hashlib.sha256(body).hexdigest(), "candidates": len(entries)}


def matching_choice(sim, actor, config_id, messages):
    marker = sim.experiment or {}
    if not marker.get("reuse_first_round") or sim.window != marker["window"] or sim.round != 1:
        return None
    with sim._lock:
        entries = getattr(sim, "_first_round_choices", None)
        if entries is None:
            source = marker.get("first_round_source", {})
            try:
                with (sim.journal.directory / FILENAME).open("rb") as stream:
                    body = stream.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != source.get("sha256"):
                    raise ValueError()
                record = json.loads(body)
                if (not isinstance(record, dict) or record.get("version") != 1 or record.get("parent_id") != marker["parent_id"]
                        or record.get("window") != sim.window + 1 or not isinstance(record.get("entries"), dict)):
                    raise ValueError()
            except (OSError, ValueError):
                raise ModelError("首轮原判断记录缺失或损坏，请从原路线重新建立这条对照") from None
            entries = sim._first_round_choices = record["entries"]
        entry = entries.get(actor)
    if not entry or entry["fingerprint"] != fingerprint(sim.config.catalog[config_id], messages):
        return None
    try:
        if any(reference_fingerprint(sim.journal.event(ref)) != expected for ref, expected in entry["references"].items()):
            return None
    except (FileNotFoundError, KeyError, ValueError):
        return None
    protect_credentials(entry["text"], sim.config.catalog.values())
    return {"text": entry["text"], "source": {
        "run_id": marker["parent_id"], "actor_id": actor, "window": sim.window + 1, "round": 1,
        "input_id": entry["input_id"], "output_id": entry["output_id"], "action_id": entry["action_id"],
        "fingerprint": entry["fingerprint"],
    }}
