"""Command-line intake through the same local workbench and persistent job receipts as the browser."""
import re
import time
import uuid
from pathlib import Path

import httpx


def _request(client, method, path, **kwargs):
    try:
        response = client.request(method,path,**kwargs)
    except httpx.RequestError as error:
        raise ValueError("无法连接本机工作台，请检查服务地址和端口") from error
    if not response.is_success:
        try:
            detail = response.json().get("detail") or response.reason_phrase
        except ValueError:
            detail = response.reason_phrase
        raise ValueError(f"工作台返回 {response.status_code}，{detail}")
    return response.json()


def prepare_at_workbench(client, files, background="", *, allow_search=True, resume_job=None, progress=print):
    if resume_job:
        if not re.fullmatch(r"[a-f0-9]{32}",resume_job) or files or background.strip():
            raise ValueError("恢复整理时只需原任务编号")
        job = resume_job
    else:
        if not files and not background.strip():
            raise ValueError("请提供一份文稿或一段背景")
        profile = _request(client,"GET","/api/model")
        if not profile["ready"]:
            raise ValueError("请先在工作台右上角配置本次使用的模型")
        sources = []
        for file in map(Path,files):
            with file.open("rb") as stream:
                source = _request(client,"POST","/api/materials",files={"file":(file.name,stream)})["source"]
            sources.append(source)
        job = uuid.uuid4().hex
        progress("整理任务 " + job)
        _request(client,"POST",f"/api/jobs/{job}/prepare",
                 json={"background":background,"sources":sources,"allow_search":allow_search})
    prior = ""
    while True:
        receipt = _request(client,"GET",f"/api/jobs/{job}")
        if receipt["kind"] != "prepare":
            raise ValueError("指定编号不是 v1.1 场景整理任务")
        if receipt["status"] == "completed":
            break
        if receipt["status"] in {"failed","interrupted"}:
            raise ValueError(f"整理未完成（{job}），{receipt.get('error') or '请打开工作台查看记录'}")
        if receipt["message"] != prior:
            prior = receipt["message"]
            progress(prior)
        time.sleep(.5)
    result = _request(client,"GET",f"/api/jobs/{job}/review")
    sources = result["scenario"]["sources"]
    goal = next((s["text"] for s in sources if s["id"].startswith("user_brief") and s["name"]=="用户背景与补充"),"")
    payload = {k:result[k] for k in ("scenario","config","preparation_records")}
    payload["assistant"] = {"summary":result["summary"],"goal":goal,"extra_answers":"",
                            "source_ids":[s["id"] for s in sources if s.get("asset_id")],"overrides":{},
                            "plan_reason":result["plan_reason"],"revision_reason":""}
    project = _request(client,"POST","/api/projects",params={"prepared_job":job},json=payload)
    return {"job_id":job,"project_id":project["id"],"version":project["version"],"summary":result["summary"],
            "questions":result["questions"],"windows":result["config"]["windows"],
            "review_url":str(client.base_url).rstrip("/")+"/#project/"+project["id"]}
