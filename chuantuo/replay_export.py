"""Export a self-contained, read-only observation file with connection configuration removed."""
import base64
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .models import protect_credentials


MAX_REPLAY_BYTES = 64 * 1024 * 1024
KINDS = {"run_created","scene_parse","observation","expression","decision_deferred","external_event",
         "message_dispatched","message_delivered","window_committed","report_created","run_status","run_error",
         "run_recovered","round_pending","round_resume_requested","agent_result_reused","settlement_rejected",
         "settlement_resume_requested","model_failure","model_validation_error","experiment_forked","inherited_reference",
         "pause_goal_set","pause_goal_reached","pause_goal_cancelled","pause_goal_expired"}
MODEL_FIELDS = {"provider","model","max_tokens","temperature","context_limit_chars","thinking","protocol",
                "timeout_seconds","max_retries","retry_delay_seconds"}


def _model_details(value):
    return {key:deepcopy(val) for key,val in value.items() if key in MODEL_FIELDS}


def _settings(value):
    settings=deepcopy(value)
    if "model" in settings:
        settings["model"]=_model_details(settings["model"])
    if "models" in settings:
        settings["models"]["configs"]={key:_model_details(model) for key,model in settings["models"].get("configs",{}).items()}
    return settings


def _redact_connections(value, literals):
    if isinstance(value,str):
        for text in literals:
            value=value.replace(text,"[接口连接信息已移除]")
        return value
    if isinstance(value,list):
        return [_redact_connections(item,literals) for item in value]
    if isinstance(value,dict):
        return {key:_redact_connections(item,literals) for key,item in value.items()}
    return value


def _portable_numbers(value):
    if type(value) is int and abs(value)>9_007_199_254_740_991:
        return {"exact_integer":str(value)}
    if isinstance(value,list):
        return [_portable_numbers(item) for item in value]
    if isinstance(value,dict):
        return {key:_portable_numbers(item) for key,item in value.items()}
    return value


def replay_route(sim, through):
    with sim._lock:
        if not 0<=through<=sim.window:
            raise ValueError("请选择已经提交的窗口作为导出终点")
        records=sim.journal.selected(transport_details=False)
        discarded=set(sim._discarded)
        model_configs=list(sim.config.catalog.values())
        title=sim._scenario.title
        run_id=sim.run_id
    result=[]
    for original in records:
        if original["window"]>through or original["kind"] not in KINDS:
            continue
        event=deepcopy(original)
        data,kind=event["data"],event["kind"]
        if kind=="run_created":
            data["config"]=_settings(data["config"])
            data.pop("launch_job_id",None)
        elif kind=="scene_parse":
            data.pop("preparation_trace",None)
        elif kind=="report_created":
            for key in ("job_id","config_id","output_id"):
                data.pop(key,None)
        event["discarded"]=event["id"] in discarded
        result.append(event)
    literals={text for model in model_configs for text in (model.base_url,model.api_key_env) if text and len(text)>=8}
    result=_redact_connections(result,sorted(literals,key=len,reverse=True))
    protect_credentials(result,model_configs)
    return {"id":run_id,"title":title,"through":through,"events":result}


def export_replay(sim, through=None, parent=None):
    end=sim.window if through is None else through
    if parent and end>parent.window:
        raise ValueError("对照回放的终点须为两条路线都已提交的窗口")
    routes=[replay_route(s,end) for s in ([parent,sim] if parent else [sim])]
    payload={"version":1,"created":datetime.now(timezone.utc).isoformat(timespec="seconds"),"routes":routes,
             "scope":"本文件收录所选时段的状态、消息、主体观察及已有报告。连接配置在导出时清理。"}
    encoded=json.dumps(_portable_numbers(payload),ensure_ascii=False,separators=(",",":"),allow_nan=False).replace("<","\\u003c").replace("&","\\u0026")
    if len(encoded.encode())>MAX_REPLAY_BYTES:
        raise ValueError("截至这个窗口的回放超过 64 MiB，请选择更早的终点或只导出一条路线")
    assets=Path(__file__).with_name("static")
    style=(assets/"replay.css").read_text("utf-8")
    script=(assets/"observation-text.js").read_text("utf-8")+"\n"+(assets/"replay.js").read_text("utf-8")
    fingerprint=lambda text:base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
    policy="default-src 'none'; img-src data:; style-src 'sha256-"+fingerprint(style)+"'; script-src 'sha256-"+fingerprint(script)+"'; base-uri 'none'; form-action 'none'"
    head='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    head+='<meta http-equiv="Content-Security-Policy" content="'+policy+'"><title>Trantor 推演回放</title><style>'+style+'</style></head>'
    body='<body><header><b>Trantor</b><span>推演回放 · 只读</span></header><main id="root"></main><dialog id="reader"><button id="close">关闭</button><div id="record"></div></dialog>'
    return head+body+'<script id="replay" type="application/json">'+encoded+'</script><script>'+script+'</script></body></html>'
