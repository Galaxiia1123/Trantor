"""Single-user loopback workbench. No external hosting or account service."""
from __future__ import annotations

import threading
import uuid
import hashlib
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import Field, ValidationError, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from .demo import demo_config, demo_scenario, example_scenario
from .engine import Simulation, encode
from .models import ModelConfig, ModelError, ModelGateway, protect_credentials
from .preflight import preflight
from .preparation_trace import PreparationGateway
from .preparation import Seed, prepare
from .source_contract import confirm_named_fields, reconcile
from .profile import ModelProfile, ProfileInput
from .models import credential_ready
from .schema import Influence, Record, RunConfig, RunConfig11, Scenario, Scenario11, Source
from .services import estimate_weights, generate_report, parse_scenario, plan_report
from .definition import DefinitionRequest, prepare_definition
from .intake import MaterialStore, MAX_FILE_BYTES
from .jobs import Jobs, fingerprint_for
from .locking import writer_lock
from .live import stream_events
from .repository import Repository, VersionConflict
from .project_io import export_project, import_project
from . import __version__
from .experiments import Adoption, ExperimentRequest, adopt_suggestion, branch_payload, compare, experiment_comparison, fork_experiment, outcome_groups
from .recovery import salvage
from .observatory import FieldWatch, observe_field, observation_notes, ongoing_matters
from .interactions import interaction_map, conversation, dispatch_ledger
from .studies import Studies, StudyRequest, study_notes
from .story import field_reason, window_strip
from .documents import document_history
from .ideas import useful_ideas
from .briefing import runtime_scene
from .perception import Perception, decision_view
from .insights import Question, actor_observation, answer_question, event_preview, search_evidence, timeline, watch_window, window_history


class SceneBundle(Record):
    scenario: Scenario | Scenario11
    config: RunConfig | RunConfig11
    preparation_records: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def known_preparation(self):
        if isinstance(self.scenario, Scenario11) != isinstance(self.config, RunConfig11):
            raise ValueError("场景与运行方案的版本不一致")
        protect_credentials(self.model_dump(mode="json"), self.config.catalog.values())
        if any(item.get("kind") not in {"scene_parse", "weight_estimation"} for item in self.preparation_records):
            raise ValueError("无法识别的准备阶段记录")
        return self


class OpeningPreview(SceneBundle):
    actor_id: str


class RunRequest(SceneBundle):
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12}$")
    project_version: int | None = Field(default=None, gt=0)


class ProjectRequest(SceneBundle):
    expected_version: int = Field(default=0, ge=0)
    assistant: dict = Field(default_factory=dict)


class ParseRequest(Record):
    text: str
    model: ModelConfig
    sources: list[Source] = Field(default_factory=list)


class EstimateRequest(Record):
    scenario: Scenario
    model: ModelConfig


class BranchRequest(Record):
    window: int = Field(ge=0)
    title: str = Field(default="", max_length=250)


class ReportOptions(Record):
    max_calls: int | None = Field(default=32,gt=0)
    reuse: bool = True
    expected_window: int | None = Field(default=None,ge=0)


def create_app(runs_directory: Path | None = None, gateway=None, *, public_search=None):
    root = Path(runs_directory or "runs")
    owns_gateway = gateway is None
    gateway = gateway or ModelGateway()
    @asynccontextmanager
    async def lifespan(app):
        with writer_lock(root / ".workbench.lock"):
            try:
                yield
            finally:
                studies.shutdown()
                jobs.pool.shutdown(wait=True)
                for worker in list(workers.values()):
                    worker.join()
                if owns_gateway:
                    gateway.close()
    app = FastAPI(title="川陀本地操作台", version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    runs = weakref.WeakValueDictionary()
    recent = OrderedDict()
    workers: dict[str, threading.Thread] = {}
    guard = threading.RLock()
    letter_lock = threading.Lock()
    materials = MaterialStore(root / "_materials")
    repository = Repository(root)
    profile = ModelProfile(root)
    jobs = Jobs(root / "_jobs")
    app.state.runs = runs

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        # A local UI can spend API credits: reject cross-origin browser writes.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "只接受本地操作台的同源请求"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request, error):
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in error.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    @app.exception_handler(ValidationError)
    async def validation_error(request, error):
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in error.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_resource(request, error):
        return JSONResponse({"detail": "未找到这份本地记录"}, status_code=404)

    @app.exception_handler(VersionConflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ModelError)
    async def model_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=502)

    def remember(simulation):
        key = simulation.run_id
        runs[key] = recent[key] = simulation
        recent.move_to_end(key)
        if len(recent) > 8:
            recent.popitem(last=False)
        return simulation

    def get_run(run_id):
        with guard:
            live = runs.get(run_id)
            return remember(live or Simulation.restore(repository.run_directory(run_id), gateway))

    def start(simulation, recover=False):
        with guard:
            prior = workers.get(simulation.run_id)
            if prior and prior.is_alive():
                raise HTTPException(status_code=409, detail="运行中，请等待窗口边界")
            if recover:
                if simulation.status not in {"failed", "interrupted"}:
                    raise HTTPException(status_code=409, detail="当前运行无需重试窗口")
                if not simulation.snapshot()["can_resume"]:
                    raise HTTPException(status_code=409, detail="本场调用预算已用完，请在新运行中调整上限")
                simulation.recover()
            if simulation.status not in ("ready", "paused"):
                raise HTTPException(status_code=409, detail="当前状态不可继续")
            def run_worker():
                try:
                    simulation.run()
                finally:
                    with guard:
                        workers.pop(simulation.run_id, None)
            worker = threading.Thread(target=run_worker, name=f"run-{simulation.run_id}", daemon=True)
            workers[simulation.run_id] = worker
            worker.start()

    @app.get("/api/health")
    def health():
        return {"app":"chuantuo", "version":__version__, "status":"ok","store":str(root.resolve()),"assets":revision,"build":build_id,
                "active_runs":sum(w.is_alive() for w in list(workers.values())),"active_jobs":len(jobs.active)+len(studies.workers),
                "active_studies":len(studies.workers)}

    @app.post("/api/opening-view")
    def preview_opening(request:OpeningPreview):
        scene=runtime_scene(request.scenario)
        if request.actor_id not in {a.id for a in scene.entities}:
            raise ValueError("请选择开局中已定义的主体")
        p=Perception(scene,scene.depths(),request.config)
        due=[{"id":e.id,"data":e.model_dump(mode="json")} for e in p.calendar.get(1,())]
        view=decision_view(p.observe(request.actor_id,scene.initial_state(),[],1,1,None,due))
        for notice in view.get("external_events",()):
            notice.pop("record",None)
        return view

    @app.get("/api/demo")
    def demo():
        return {"scenario": demo_scenario().model_dump(mode="json"), "config": demo_config().model_dump(mode="json")}

    @app.get("/api/model")
    def get_model():
        return profile.view()

    @app.get("/api/case")
    def tidal_case():
        return {"name":"潮汐港扩建听证.md", "text":case_assets["tidal-port.md"].decode("utf-8")}

    @app.get("/api/case/replay")
    def tidal_replay():
        return Response(case_assets["tidal-replay.json"],media_type="application/json",headers={"Cache-Control":"no-cache"})

    @app.get("/api/case/recording")
    def verified_replay():
        return Response(case_assets["verified-replay.json"],media_type="application/json",headers={"Cache-Control":"no-cache"})

    @app.get("/api/case/rehearsal")
    def rehearsal_material():
        return {"name":"rehearsal-room.md","text":case_assets["rehearsal-room.md"].decode("utf-8")}

    @app.get("/api/case/materials")
    def sample_materials():
        return [{"id":key,"title":v[1],"replay":v[2]} for key,v in material_templates.items()]

    @app.get("/api/case/materials/{key}")
    def sample_material(key:str):
        if key not in material_templates:
            raise FileNotFoundError("没有这份案例文稿")
        filename,title,_=material_templates[key]
        return {"id":key+"_brief","name":title+".md","text":case_assets[filename].decode("utf-8")}

    def playable_case(run_example,choice):
        # This sample uses the built-in script independently of the user's current profile.
        with letter_lock:
            sample = ModelGateway()
            try:
                return run_example(root,choice,sample)
            finally:
                sample.close()

    @app.post("/api/case/letter/{choice}")
    def letter_experiment(choice: Literal["notified","unseen","repeat"]):
        from .letter_case import run_example
        return playable_case(run_example,choice)

    @app.post("/api/case/post/{choice}")
    def post_experiment(choice: Literal["express","notice","repeat"]):
        from .mountain_case import run_example
        return playable_case(run_example,choice)

    @app.put("/api/model")
    def save_model(request: ProfileInput):
        return profile.save(request)

    def submit_preparation(job_id, request, model, parent=None):
        if not model or (owns_gateway and not credential_ready(model)):
            raise ValueError("请先配置本次使用的模型与凭据")
        protect_credentials(request.model_dump(mode="json"), [model])
        def run(progress):
            trace = PreparationGateway(gateway, jobs.root/"evidence", job_id, parent)
            result = prepare(request, model, trace, materials, progress, public_search)
            result["preparation_records"][0]["preparation_trace"] = {"job_id":job_id, "parent_job":parent,
                                                                    "new_model_calls":trace.calls,"reused_calls":trace.reused,
                                                                    "rechecked_candidates":trace.rechecked,
                                                                    "usage":trace.journal.usage()}
            return result
        return jobs.submit(job_id, "prepare", {"seed":request.model_dump(mode="json"), "model":model.model_dump(mode="json")}, run, parent=parent)

    @app.post("/api/jobs/{job_id}/prepare")
    def prepare_scene(job_id: str, request: Seed):
        return submit_preparation(job_id,request,profile.model)

    @app.get("/api/jobs/{job_id}/review")
    def review_preparation(job_id: str):
        receipt = jobs.get(job_id)
        if receipt["kind"] != "prepare" or receipt["status"] != "completed":
            raise ValueError("场景整理完成后才能核对草案")
        return reconcile(receipt["result"])

    @app.get("/api/examples/{name}")
    def example(name: str):
        return {"scenario":example_scenario(name).model_dump(mode="json"), "config":demo_config().model_dump(mode="json")}

    @app.post("/api/validate")
    def validate(request: RunRequest):
        depths = request.scenario.depths()
        if isinstance(request.config, RunConfig) and not set(depths.values()) <= request.config.models.layers.keys():
            raise ValueError("每个显式包含层级都必须配置模型")
        warnings = []
        warnings.extend(f"「{name}」只有提取文字，本机缺少原件" for name in materials.check_sources(request.scenario.sources))
        if any(isinstance(e, Influence) and e.weight is None for e in request.scenario.influences):
            warnings.append("存在缺失权重，启动前需填写或估算")
        if request.scenario.missing_information:
            warnings.append("场景仍有缺失信息，请审阅其对推演的影响")
        config = request.config
        estimate = preflight(request.scenario, config, depths)
        warnings.extend(estimate["blocking_context"])
        missing = estimate["missing_credentials"]
        if missing:
            warnings.append("运行模型尚未配置密钥：" + "、".join(missing))
        if config.model_call_limit is not None and config.model_call_limit < estimate["baseline_calls"]:
            warnings.append("调用上限低于跑完全部窗口的基础次数，到达上限时将停止")
        return {"valid": True, "depths": depths, "warnings": warnings, "preflight": estimate}

    @app.post("/api/parse")
    def parse(request: ParseRequest):
        return parse_scenario(request.text, request.model, gateway, sources=request.sources)

    @app.post("/api/materials")
    def upload_material(file: UploadFile = File(...)):
        raw = file.file.read(MAX_FILE_BYTES + 1)
        source = materials.ingest(file.filename or "material", raw, credential_configs=[profile.model] if profile.model else [])
        return {"source": source.model_dump(mode="json")}

    @app.get("/api/materials/{asset_id}/original")
    def original_material(asset_id: str):
        source, folder = materials.load(asset_id)
        with (folder / "original").open("rb") as stream:
            if source.sha256 and hashlib.file_digest(stream,"sha256").hexdigest() != source.sha256:
                raise ValueError("本机原件与导入时的摘要不符")
        return FileResponse(folder / "original", filename=source.name, media_type=source.mime_type)

    @app.get("/api/materials/{asset_id}")
    def material_text(asset_id: str):
        source, _ = materials.load(asset_id)
        return {"source":source.model_dump(mode="json")}

    @app.post("/api/jobs/{job_id}/material")
    def queue_material(job_id: str, file: UploadFile = File(...)):
        name = (file.filename or "material").replace("\\","/").rsplit("/",1)[-1]
        raw = file.file.read(MAX_FILE_BYTES + 1)
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise ValueError("材料不能为空，单文件上限为 32 MiB")
        fingerprint = {"name":name,"sha256":hashlib.sha256(raw).hexdigest()}
        def extract(progress):
            source = materials.ingest(name, raw, progress,asset_id=job_id, credential_configs=[profile.model] if profile.model else [])
            return {"source":source.model_dump(mode="json")}
        return jobs.submit(job_id, "material", fingerprint, extract)

    @app.post("/api/definition")
    def define(request: DefinitionRequest):
        return prepare_definition(request, gateway, materials)

    def submit_definition(job_id, request, parent=None):
        def run_definition(progress):
            trace = PreparationGateway(gateway,jobs.root/"evidence",job_id,parent)
            result = prepare_definition(request,trace,materials,progress=progress)
            coverage = {"job_id":job_id,"parent_job":parent,"new_model_calls":trace.calls,"reused_calls":trace.reused}
            result["records"][0]["preparation_trace"] = coverage
            result["preparation_trace"] = coverage
            result["request_context"] = {"goal":request.goal,"constraints":request.constraints,"granularity":request.granularity,
                                         "batches":request.max_source_batches}
            return result
        return jobs.submit(job_id,"definition",request.model_dump(mode="json"),run_definition,parent=parent)

    @app.post("/api/jobs/{job_id}/definition")
    def queue_definition(job_id: str, request: DefinitionRequest):
        return submit_definition(job_id,request)

    @app.post("/api/jobs/{parent}/retry/{job_id}")
    def retry_definition(parent:str,job_id:str):
        prior = jobs.get(parent,with_result=False)
        if prior["kind"] not in {"definition","prepare"} or prior["status"] not in {"failed","interrupted"}:
            raise HTTPException(status_code=409,detail="仅失败或中断的场景整理任务可按原请求重试")
        path = jobs.path(parent).with_suffix(".request.json")
        if not path.is_file():
            raise ValueError("这份早期任务没有保存完整请求，请从原草稿重新整理")
        import json
        original = json.loads(path.read_text(encoding="utf-8"))
        expected = fingerprint_for(prior["kind"],original["payload"],prior.get("context",{}).get("parent_job"))
        if original["fingerprint"] != prior["fingerprint"] or expected != prior["fingerprint"]:
            raise ValueError("原请求与任务凭据不一致")
        if prior["kind"] == "prepare":
            model = ModelConfig.model_validate(original["payload"]["model"])
            if model != profile.model:
                raise ValueError("本机模型配置已改变。请按新配置重新整理，或恢复原配置后按原请求重试")
            return submit_preparation(job_id, Seed.model_validate(original["payload"]["seed"]), model, parent)
        return submit_definition(job_id,DefinitionRequest.model_validate(original["payload"]),parent)

    @app.get("/api/jobs/{job_id}/trace")
    def preparation_export(job_id:str):
        jobs.path(job_id)
        path = jobs.root/"evidence"/job_id/"events.jsonl"
        if not path.is_file():
            raise FileNotFoundError("任务没有准备记录")
        return FileResponse(path,media_type="application/x-ndjson",filename=f"chuantuo-definition-{job_id}.jsonl")

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        receipt = jobs.get(job_id)
        if receipt["status"] != "interrupted":
            return receipt
        context = receipt.get("context",{})
        kind = receipt["kind"]
        if kind == "material" and (materials.directory/job_id/"source.json").is_file():
            original,_ = materials.load(job_id)
            return jobs.reconcile(job_id,{"source":original.model_dump(mode="json")})
        rid = context.get("run_id")
        if not rid or kind not in {"launch","experiment","report","question"}:
            return receipt
        try:
            sim = get_run(rid)
        except FileNotFoundError:
            return receipt
        if kind in {"launch","experiment"} and sim.journal.event("e000001")["data"].get("launch_job_id")==job_id:
            return jobs.reconcile(job_id,sim.snapshot())
        record_kind = "report_created" if kind=="report" else "analysis_answer"
        for e in sim.journal.snapshot({record_kind}):
            if e["data"].get("job_id")==job_id:
                result = saved_report(rid,e["id"]) if kind=="report" else {**e["data"],"record_id":e["id"]}
                return jobs.reconcile(job_id,result)
        return receipt

    @app.get("/api/jobs")
    def recent_jobs(limit: int = Query(default=25,ge=1,le=100)):
        receipts=jobs.catalog(limit)
        for item in receipts:
            if item["kind"] in {"definition","prepare"}:
                item["has_trace"]=(jobs.root/"evidence"/item["id"]/"events.jsonl").is_file()
                item["can_retry"]=jobs.path(item["id"]).with_suffix(".request.json").is_file() and item["status"] in {"failed","interrupted"}
        return receipts

    @app.post("/api/estimate")
    def estimate(request: EstimateRequest):
        return estimate_weights(request.scenario, request.model, gateway)

    def create_run(request: RunRequest, run_id, launch_id=None):
        suggested = next((r.get("observation_questions",[]) for r in reversed(request.preparation_records) if r.get("kind")=="scene_parse"),[])
        if isinstance(request.config, RunConfig11):
            if any(record.get("blocking_questions") for record in request.preparation_records):
                raise ValueError("开局仍有关键问题，请补充回答并重新整理场景")
            if profile.model is None or request.config.model != profile.model:
                raise ValueError("项目的接口配置与本机当前选择不同。请在高级编辑中绑定本机配置，或用当前模型重新整理。")
        if bool(request.project_id) != bool(request.project_version):
            raise ValueError("关联项目需要同时提供项目编号和版本")
        if request.project_id:
            project = repository.get_project(request.project_id, request.project_version)
            suggested = project["payload"].get("assistant",{}).get("observation_questions",suggested)
            saved = SceneBundle.model_validate({k: project["payload"][k] for k in SceneBundle.model_fields}).model_dump(mode="json")
            if any(saved[key] != getattr(request, key).model_dump(mode="json") for key in ("scenario", "config")) or saved["preparation_records"] != request.preparation_records:
                raise VersionConflict("请先保存当前配置为新版本，再关联这次推演")
        readiness = validate(request)["preflight"]
        if readiness["blocking_context"]:
            raise ValueError("；".join(readiness["blocking_context"][:4]))
        if owns_gateway and readiness["missing_credentials"]:
            raise ValueError("运行模型的密钥尚未配置，请在启动服务的环境中设置")
        root.mkdir(parents=True,exist_ok=True)
        target = root/run_id
        with TemporaryDirectory(dir=root,prefix="_creating-") as temporary:
            stage = Path(temporary)/"run"
            simulation = Simulation(request.scenario, request.config, gateway,stage,_run_id=run_id,_launch_job_id=launch_id)
            if request.project_id:
                simulation.journal.append("project_linked", {"project_id": request.project_id, "version": request.project_version})
            for record in request.preparation_records:
                simulation.journal.append(record["kind"], record)
            for data in suggested if isinstance(suggested,list) else []:
                watch=FieldWatch.model_validate(data)
                watch.resolve(simulation._scenario,simulation.config.catalog.values())
                wid=uuid.uuid5(uuid.NAMESPACE_URL,encode(watch.model_dump(mode="json"))).hex
                repository.field_watches(stage,wid,watch.model_dump(mode="json"))
            stage.rename(target)
            simulation.journal.directory = target
        with guard:
            remember(simulation)
        start(simulation)
        return simulation.snapshot()

    @app.post("/api/runs")
    def create(request: RunRequest):
        return create_run(request,uuid.uuid4().hex[:12])

    @app.get("/api/projects")
    def list_projects():
        return repository.list_projects()

    @app.post("/api/jobs/{job_id}/launch")
    def launch_job(job_id: str, request: RunRequest):
        run_id = uuid.uuid5(uuid.NAMESPACE_URL,"chuantuo:launch:"+job_id).hex[:12]
        return jobs.submit(job_id, "launch", {**request.model_dump(mode="json"),"run_id":run_id},
                           lambda progress: create_run(request,run_id,job_id))

    @app.post("/api/projects")
    def new_project(request: ProjectRequest, prepared_job: str | None = Query(default=None,pattern=r"^[a-f0-9]{32}$")):
        return persist_project(request,None,prepared_job)

    def persist_project(request, project_id, prepared_job):
        payload = request.model_dump(mode="json", exclude={"expected_version"})
        if not prepared_job:
            confirm_named_fields(payload)
            payload["assistant"].pop("adopted_prepare_job",None)
            return repository.save_project(payload,project_id,request.expected_version)
        receipt = jobs.get(prepared_job,with_result=False)
        if receipt["kind"] != "prepare" or receipt["status"] != "completed" or not any(
            r.get("preparation_trace",{}).get("job_id")==prepared_job for r in request.preparation_records
        ):
            raise ValueError("自动保存需要对应一份已经完成的场景整理")
        payload["assistant"]["adopted_prepare_job"] = prepared_job
        return repository.save_prepared(payload,prepared_job,project_id,request.expected_version)

    @app.get("/api/projects/{project_id}/package")
    def package(project_id: str, version: int | None = Query(default=None,ge=1)):
        path = export_project(repository, materials, project_id, version)
        return FileResponse(path, media_type="application/zip", filename=f"chuantuo-project-{project_id}.zip",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.post("/api/project-import")
    def unpack(file: UploadFile = File(...)):
        def normalize(payload):
            return ProjectRequest.model_validate(payload).model_dump(mode="json", exclude={"expected_version"})
        return import_project(file.file, repository, materials, normalize)

    @app.get("/api/projects/{project_id}")
    def read_project(project_id: str, version: int | None = Query(default=None, ge=1)):
        return repository.get_project(project_id, version)

    @app.get("/api/projects/{project_id}/versions")
    def project_versions(project_id: str):
        return repository.versions(project_id)

    @app.post("/api/projects/{project_id}")
    def save_project(project_id: str, request: ProjectRequest, prepared_job: str | None = Query(default=None,pattern=r"^[a-f0-9]{32}$")):
        repository.get_project(project_id)
        return persist_project(request,project_id,prepared_job)

    @app.get("/api/runs")
    def catalog():
        result = repository.catalog()
        with guard:
            for item in result["runs"]:
                live = runs.get(item["run_id"])
                if live:
                    item["status"] = live.snapshot()["status"]
        return result

    @app.post("/api/archive/{directory}/salvage")
    def rescue(directory: str):
        return salvage(repository,directory)

    @app.get("/api/runs/{run_id}")
    def status(run_id: str):
        return get_run(run_id).snapshot()

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0, limit: int | None = Query(default=None, ge=1, le=500), preview: bool = False):
        sim = get_run(run_id)
        page = sim.journal.page(after, (limit or 200) if preview else limit, event_preview if preview else None)
        if preview:
            for e in page["events"]:
                e["commit_status"] = sim.event_status(e)
        return page

    @app.get("/api/runs/{run_id}/events/{event_id}")
    def event_detail(run_id: str, event_id: str):
        sim = get_run(run_id)
        event = sim.journal.event(event_id)
        return {**event, "commit_status":sim.event_status(event)}

    @app.get("/api/runs/{run_id}/stream")
    def live_events(run_id:str, request:Request, after:int=Query(default=0,ge=0)):
        sim = get_run(run_id)
        resume_id = request.headers.get("last-event-id")
        if resume_id:
            if not resume_id.isascii() or not resume_id.isdecimal() or len(resume_id)>12:
                raise ValueError("实时记录游标无效")
            after = max(after,int(resume_id))
        sim.journal.page(after,0)
        return StreamingResponse(stream_events(sim,after,request), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.get("/api/runs/{run_id}/timeline")
    def replay(run_id: str):
        return timeline(get_run(run_id))

    @app.get("/api/runs/{run_id}/interactions")
    def interaction_focus(run_id:str,start:int=Query(default=0,ge=0),through:int|None=Query(default=None,ge=0),focus:str|None=None):
        return interaction_map(get_run(run_id),start,through,focus)

    @app.get("/api/runs/{run_id}/conversation")
    def read_conversation(run_id:str,one:str,two:str,start:int=Query(default=0,ge=0),through:int|None=Query(default=None,ge=0),
                          q:str=Query(default="",max_length=200),offset:int=Query(default=0,ge=0),limit:int=Query(default=30,ge=1,le=80)):
        return conversation(get_run(run_id),one,two,start,through,q,offset,limit)

    @app.get("/api/runs/{run_id}/documents")
    def document_copies(run_id:str,through:int|None=Query(default=None,ge=0),document:str|None=None,
                        offset:int=Query(default=0,ge=0),limit:int=Query(default=30,ge=1,le=100)):
        return document_history(get_run(run_id),through,document,offset,limit)

    @app.get("/api/runs/{run_id}/mail")
    def historical_mail(run_id:str,through:int|None=Query(default=None,ge=0),actor:str|None=None,stage:Literal["pending","arrived"]|None=None,
                        offset:int=Query(default=0,ge=0),limit:int=Query(default=30,ge=1,le=100)):
        return dispatch_ledger(get_run(run_id),through,actor,stage,offset,limit)

    @app.get("/api/runs/{run_id}/watch")
    def observe_window(run_id: str, window: int | None = Query(default=None,ge=0),
                       offset: int = Query(default=0,ge=0), limit: int = Query(default=100,ge=1,le=300),
                       actor: str | None = None, kind: Literal["expression","decision_deferred","external_event","message_dispatched","message_delivered","window_committed","settlement_rejected","model_failure"] | None = None):
        return watch_window(get_run(run_id), window, offset, limit, actor, kind)

    @app.get("/api/runs/{run_id}/window-strip")
    def strip(run_id:str,start:int=Query(default=1,ge=1),through:int|None=Query(default=None,ge=0),slots:int=Query(default=32,ge=2,le=64)):
        return window_strip(get_run(run_id),start,through,slots)

    @app.get("/api/runs/{run_id}/field-reason")
    def explain_field(run_id:str,window:int=Query(ge=0),actor:str=Query(),field:str=Query(),
                      offset:int=Query(default=0,ge=0),limit:int=Query(default=12,ge=1,le=40)):
        return field_reason(get_run(run_id),window,actor,field,offset,limit)

    @app.get("/api/runs/{run_id}/calendar")
    def event_calendar(run_id:str):
        sim=get_run(run_id)
        with sim._lock:
            recorded={e["data"]["id"]:e for e in sim.journal.snapshot({"external_event"}) if e["id"] not in sim._discarded}
            return [{"event":event.model_dump(mode="json"),"record":recorded[event.id]["id"] if event.id in recorded else None,
                     "status":"committed" if event.window<=sim.window else "pending" if event.id in recorded else
                              "beyond_run" if sim.status in {"completed","stopped"} else "scheduled"}
                    for event in sorted(getattr(sim._scenario,"scheduled_events",[]),key=lambda e:e.window)]

    @app.get("/api/runs/{run_id}/windows")
    def committed_history(run_id: str, offset: int = Query(default=0,ge=0), limit: int = Query(default=40,ge=1,le=100),
                          changed_only: bool=False, newest_first: bool=False):
        return window_history(get_run(run_id),offset,limit,changed_only=changed_only,newest_first=newest_first)

    @app.post("/api/runs/{run_id}/field-observation")
    def field_observation(run_id:str, request:FieldWatch, through:int|None=Query(default=None,ge=0)):
        sim=get_run(run_id)
        result=observe_field(sim,request,through)
        if sim.experiment:
            parent=get_run(sim.experiment["parent_id"])
            result["baseline"]={"run_id":parent.run_id, **observe_field(parent,request,result["through"],details=False)}
        return result

    @app.get("/api/runs/{run_id}/watches")
    def watch_cards(run_id:str, through:int|None=Query(default=None,ge=0)):
        sim=get_run(run_id)
        return [{"id":key,**observe_field(sim,FieldWatch.model_validate(data),through,details=False)}
                for key,data in repository.field_watches(sim.journal.directory).items()]

    @app.put("/api/runs/{run_id}/watches/{watch_id}")
    def save_watch(run_id:str, watch_id:str, request:FieldWatch):
        sim=get_run(run_id)
        request.resolve(sim._scenario,sim.config.catalog.values())
        repository.field_watches(sim.journal.directory,watch_id,request.model_dump(mode="json"))
        return {"id":watch_id}

    @app.get("/api/runs/{run_id}/watch-notes")
    def export_watch_notes(run_id:str,through:int|None=Query(default=None,ge=0)):
        sim=get_run(run_id)
        result=observation_notes(sim,repository.field_watches(sim.journal.directory),through)
        return Response(result,media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":f'attachment; filename="trantor-observations-{sim.run_id}.md"'})

    @app.delete("/api/runs/{run_id}/watches/{watch_id}")
    def remove_watch(run_id:str,watch_id:str):
        sim=get_run(run_id)
        repository.field_watches(sim.journal.directory,watch_id,None)
        return {"id":watch_id}

    @app.get("/api/runs/{run_id}/matters")
    def run_matters(run_id:str, through:int|None=Query(default=None,ge=0), actor:str|None=None,
                    status:Literal["open","closed"]|None=None,offset:int=Query(default=0,ge=0),limit:int=Query(default=30,ge=1,le=100)):
        return ongoing_matters(get_run(run_id),through,actor,status,offset,limit)

    @app.post("/api/runs/{run_id}/branch")
    def branch(run_id: str, request: BranchRequest):
        return repository.save_project(branch_payload(get_run(run_id),request.window,request.title))

    def check_experiment_parent(parent):
        if not isinstance(parent.config, RunConfig11):
            raise ValueError("对照实验需要绑定与原路线相同的 v1.1 模型配置")
        if parent.config.model.provider=="demo":
            return
        if profile.model != parent.config.model:
            raise ValueError("请绑定原路线使用的模型配置后再做对照实验")
        if owns_gateway and not credential_ready(profile.model):
            raise ValueError("请先配置当前模型的凭据")

    def fork_run(parent, request, run_id, job_id, **options):
        with TemporaryDirectory(dir=root,prefix="_creating-") as temporary:
            stage = Path(temporary)/"run"
            alternate = fork_experiment(parent,request,run_id,job_id,stage,**options)
            stage.rename(root/run_id)
            alternate.journal.directory = root/run_id
        with guard:
            return remember(alternate)

    studies = Studies(root, get_run, fork_run, check_experiment_parent)

    @app.get("/api/studies")
    def list_studies():
        return studies.catalog()

    @app.put("/api/runs/{parent_id}/studies/{study_id}")
    def start_study(parent_id:str, study_id:str, request:StudyRequest):
        return studies.create(study_id, get_run(parent_id), request)

    @app.get("/api/studies/{study_id}")
    def read_study(study_id:str):
        return studies.view(study_id)

    @app.get("/api/studies/{study_id}/notes")
    def study_note(study_id:str):
        return Response(study_notes(studies.view(study_id)),media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":f'attachment; filename="trantor-study-{study_id}.md"'})

    @app.get("/api/studies/{study_id}/compare")
    def compare_study_members(study_id:str,original:int=Query(ge=1),changed:int=Query(ge=1),window:int|None=Query(default=None,ge=0)):
        return studies.compare_members(study_id,original,changed,window)

    @app.post("/api/studies/{study_id}/{action}")
    def control_study(study_id:str, action:Literal["pause","stop","resume"]):
        return studies.control(study_id,action)

    @app.post("/api/jobs/{job_id}/experiment/{parent_id}")
    def experiment_job(job_id: str, parent_id: str, request: ExperimentRequest):
        parent = get_run(parent_id)
        check_experiment_parent(parent)
        protect_credentials(request.model_dump(mode="json"),parent.config.catalog.values())
        run_id = uuid.uuid5(uuid.NAMESPACE_URL,"chuantuo:experiment:"+job_id).hex[:12]
        def launch(progress):
            progress("fork","正在恢复分岔窗口的状态、记忆和已投递消息")
            alternate = fork_run(parent,request,run_id,job_id)
            start(alternate)
            return alternate.snapshot()
        spec=request.model_dump(mode="json",exclude={name for name in ("route","event_move") if getattr(request,name) is None})
        return jobs.submit(job_id,"experiment",{"run_id":run_id,"parent_id":parent_id,"spec":spec},launch)

    @app.get("/api/runs/{run_id}/ideas")
    def study_ideas(run_id:str):
        sim=get_run(run_id)
        if not isinstance(sim.config,RunConfig11):
            return []
        if sim._idea_cache is None:
            earliest=(sim.experiment or {}).get("window",0)
            watches=repository.field_watches(sim.journal.directory).values()
            ideas=useful_ideas(sim._scenario,sim.config,sim.journal.comparison_ideas,watches,earliest=earliest,reviewed=True)
            with sim._lock:
                sim._idea_cache=ideas
        with sim._lock:
            return [{**idea,"ready":idea["experiment"]["through"]<=sim.window} for idea in sim._idea_cache]

    @app.get("/api/runs/{run_id}/experiment")
    def experiment_result(run_id: str, window: int | None = Query(default=None,ge=0)):
        alternate = get_run(run_id)
        if not alternate.experiment:
            raise ValueError("这次推演没有原路线可对照")
        return experiment_comparison(get_run(alternate.experiment["parent_id"]),alternate,window)

    @app.get("/api/runs/{run_id}/alternatives")
    def alternatives(run_id: str, actor: str | None = None, field: str | None = None):
        parent = get_run(run_id)
        items = repository.catalog(with_outcomes=True)["runs"]
        with guard:
            for item in items:
                live = runs.get(item["run_id"])
                if live:
                    item["status"] = live.status
        return outcome_groups(parent,items,actor,field)

    @app.post("/api/runs/{run_id}/adopt-field")
    def adopt_field(run_id:str, request:Adoption):
        return repository.save_project(adopt_suggestion(get_run(run_id),request))

    @app.get("/api/runs/{run_id}/compare/{other_id}")
    def compare_runs(run_id: str, other_id: str, left_window: int | None = Query(default=None,ge=0),
                     right_window: int | None = Query(default=None,ge=0)):
        return compare(get_run(run_id),get_run(other_id),left_window,right_window)

    @app.get("/api/runs/{run_id}/actors/{actor_id}/journey")
    def actor_journey(run_id:str, actor_id:str, window:int|None=Query(default=None,ge=1),
                      turn:int|None=Query(default=None,ge=1), news_only:bool=False, through:int|None=Query(default=None,ge=1)):
        from .perspective import journey
        return journey(get_run(run_id),actor_id,window,turn,news_only,through)

    @app.get("/api/runs/{run_id}/actors/{actor_id}/letters")
    def read_letter(run_id:str,actor_id:str,window:int=Query(ge=1),turn:int=Query(ge=1),
                    direction:Literal["inbox","outbox"]="inbox",index:int=Query(default=0,ge=0)):
        from .perspective import letter_at
        return letter_at(get_run(run_id),actor_id,window,turn,direction,index)

    @app.get("/api/runs/{run_id}/actors/{actor_id}/papers/{document_id}")
    def paper_at(run_id:str,actor_id:str,document_id:str,window:int=Query(ge=1),turn:int=Query(ge=1)):
        from .perspective import held_paper
        return held_paper(get_run(run_id),actor_id,document_id,window,turn)

    @app.get("/api/runs/{run_id}/actors/{actor_id}/observation")
    def actor_view(run_id: str, actor_id: str, window: int = Query(ge=0)):
        return actor_observation(get_run(run_id), actor_id, window)

    @app.get("/api/runs/{run_id}/actors/{actor_id}/context")
    def actual_actor_input(run_id: str, actor_id:str, window:int=Query(ge=1), turn:int|None=Query(default=None,ge=1)):
        sim=get_run(run_id)
        with sim._lock:
            if window>sim.window or actor_id not in sim.depths:
                raise ValueError("请选择已提交窗口中的主体")
            config_id=sim.config.model_id("actor",sim.depths[actor_id])
            return {**sim.journal.decision_context(actor_id,window,turn,sim._discarded),
                    "mode":"sample" if sim.config.catalog[config_id].provider=="demo" else "api"}

    @app.get("/api/runs/{run_id}/actors/{actor_id}/export")
    def actor_export(run_id:str,actor_id:str,through:int=Query(ge=0)):
        simulation = get_run(run_id)
        if actor_id not in simulation.depths or through > simulation.snapshot()["window"]:
            raise ValueError("请选择存在的主体和已提交窗口")
        records = simulation.journal.private_history(actor_id,through)
        header = {"kind":"actor_export","run_id":run_id,"actor_id":actor_id,"through_window":through,
                  "records":len(records),"scope":"仅含本主体的观察、表达和收到的定向投递。投递后须至下一轮观察才成为其行动依据。"}
        def lines():
            yield encode(header) + "\n"
            for event in records:
                yield encode(event) + "\n"
        return StreamingResponse(lines(),media_type="application/x-ndjson",headers={"Content-Disposition":
            f'attachment; filename="chuantuo-{run_id}-actor-view-w{through}.jsonl"'})

    @app.get("/api/runs/{run_id}/evidence")
    def find_evidence(run_id: str, q: str = Query(min_length=1, max_length=2000), actor_id: str | None = None):
        simulation = get_run(run_id)
        return search_evidence(simulation, q, actor_id=actor_id, cutoff=simulation.window)

    @app.get("/api/runs/{run_id}/reports")
    def reports(run_id: str):
        return [{"id": e["id"], "window": e["window"], "title": e["data"]["report"]["title"]}
                for e in get_run(run_id).journal.snapshot({"report_created"})]

    @app.get("/api/runs/{run_id}/reports/{event_id}")
    def saved_report(run_id: str, event_id: str):
        sim = get_run(run_id)
        event = sim.journal.event(event_id)
        if event["kind"] != "report_created":
            raise FileNotFoundError("该记录不是报告")
        path = sim.journal.directory / f"report-{event_id}.md"
        markdown = path.read_text(encoding="utf-8") if path.exists() else event["data"].get("markdown")
        if markdown is None:
            raise FileNotFoundError("此旧报告没有可读取的 Markdown 文件")
        return {"record_id": event_id, "markdown": markdown, "cutoff_window": event["window"],"coverage":event["data"].get("coverage")}

    @app.post("/api/runs/{run_id}/jobs/{job_id}/question")
    def ask(run_id: str, job_id: str, question: Question):
        sim = get_run(run_id)
        return jobs.submit(job_id, "question", {"run_id": run_id, "actor_id":question.actor_id, **question.model_dump(exclude_defaults=True)},
                           lambda progress: answer_question(sim, question, progress,job_id=job_id))

    @app.get("/api/runs/{run_id}/answers")
    def answers(run_id: str):
        return [{"record_id": event["id"], **{key: event["data"][key] for key in
                 ("question","text","actor_id","evidence_ids","cutoff_window","limitations","config_id","anchor_record","cutoff_round","selection")
                 if key in event["data"]}}
                for event in get_run(run_id).journal.snapshot({"analysis_answer"})]

    @app.post("/api/runs/{run_id}/jobs/{job_id}/report")
    def queue_report(run_id: str, job_id: str, options: ReportOptions):
        sim = get_run(run_id)
        args = options.model_dump()
        return jobs.submit(job_id, "report", {"run_id": run_id, **args}, lambda progress: generate_report(sim, gateway, progress, job_id=job_id, **args))

    @app.get("/api/runs/{run_id}/report-plan")
    def report_plan(run_id:str, reuse:bool=True):
        return plan_report(get_run(run_id),reuse)

    def manual_run(run_id):
        simulation = get_run(run_id)
        if (simulation.experiment or {}).get("study"):
            raise VersionConflict("这条路线属于重复实验，请在实验组中控制进度和调用上限")
        return simulation

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str):
        simulation = manual_run(run_id)
        if simulation.status not in ("ready", "running"):
            raise HTTPException(status_code=409, detail="当前状态不可暂停")
        simulation.request_pause()
        return simulation.snapshot()

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        simulation = manual_run(run_id)
        start(simulation)
        return simulation.snapshot()

    @app.post("/api/runs/{run_id}/recover")
    def recover_run(run_id: str):
        simulation = manual_run(run_id)
        start(simulation, recover=True)
        return simulation.snapshot()

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str):
        simulation = manual_run(run_id)
        simulation.request_stop()
        return simulation.snapshot()

    @app.post("/api/runs/{run_id}/report")
    def report(run_id: str):
        return generate_report(get_run(run_id), gateway)

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        path = get_run(run_id).journal.directory / "events.jsonl"
        return FileResponse(path, media_type="application/x-ndjson", filename=f"chuantuo-{run_id}.jsonl")

    # A running service serves a matched UI/API release even when the source is edited.
    static = Path(__file__).parent / "static"
    assets = {p.name:p.read_bytes() for p in sorted(static.iterdir()) if p.is_file()}
    material_templates={"tidal":("tidal-port.md","潮汐港 · 跨层协商 · 9 窗","case"),
                        "rehearsal":("rehearsal-room.md","北廊排练室 · 排期交换 · 3 窗",None),
                        "roof":("roof-calendar.md","钟楼天台 · 外部事件 · 3 窗",None),
                        "class":("night-class.md","榆街社区夜课 · 席位与开班 · 2 窗",None),
                        "mail":("mountain-mail.md","山线货运站 · 跨窗通信 · 3 窗",None)}
    asset_names=[row[0] for row in material_templates.values()]+["tidal-replay.json","verified-replay.json"]
    case_assets = {name:(static.parent/"templates"/name).read_bytes() for name in asset_names}
    revision = hashlib.sha256(b"".join(assets.values())).hexdigest()[:12]
    build_digest = hashlib.sha256(revision.encode())
    for file in sorted(static.parent.glob("*.py")):
        build_digest.update(file.read_bytes())
    for case_data in case_assets.values():
        build_digest.update(case_data)
    build_id = build_digest.hexdigest()[:12]
    index_html = assets.pop("index.html").decode("utf-8").replace('"/static/',f'"/static/{revision}/')
    studio_html = assets.pop("workbench.html").decode("utf-8").replace('"/static/',f'"/static/{revision}/')

    @app.get("/static/{asset}", include_in_schema=False)
    @app.get("/static/{build}/{asset}", include_in_schema=False)
    def ui_asset(asset: str, build: str | None = None):
        if asset not in assets or (build and build != revision):
            raise HTTPException(status_code=404,detail="界面版本已更新，请刷新页面")
        media = "text/javascript" if asset.endswith(".js") else "text/css"
        return Response(assets[asset],media_type=media,headers={"Cache-Control":"public, max-age=31536000, immutable" if build else "no-cache"})

    @app.get("/archive", include_in_schema=False)
    def index():
        return HTMLResponse(index_html,headers={"Cache-Control":"no-cache"})

    @app.get("/", include_in_schema=False)
    def studio(request: Request):
        if "run" in request.query_params or "tab" in request.query_params:
            return HTMLResponse(index_html,headers={"Cache-Control":"no-cache"})
        return HTMLResponse(studio_html,headers={"Cache-Control":"no-cache"})
    return app
