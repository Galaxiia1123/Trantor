"""One ephemeral text request through the locally signed-in Codex CLI."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from functools import lru_cache
from pathlib import Path


_DISABLED = ("shell_tool", "shell_snapshot", "apps", "plugins", "memories", "multi_agent", "multi_agent_v2",
             "runtime_agent_instances", "browser_use", "browser_use_external", "in_app_browser", "computer_use",
             "image_generation", "view_image", "hooks", "code_mode", "code_mode_host", "sleep_tool",
             "request_permissions_tool", "skill_mcp_dependency_install")
_HOST_VARIABLES = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "CODEX_THREAD_ID", "CODEX_PARENT_THREAD_ID",
                   "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_PERMISSION_PROFILE", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                   "CODEX_SESSION_ID", "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY", "CODEX_TOOL_CALL_ID")


def _environment():
    environment = os.environ.copy()
    for name in _HOST_VARIABLES:
        environment.pop(name, None)
    return environment


def executable():
    candidate = os.environ.get("TRANTOR_CODEX_BIN") or shutil.which("codex")
    if not candidate:
        return None
    path = Path(candidate).resolve()
    if path.suffix.lower() in {".cmd", ".bat", ".js"}:
        # Run the native binary so timeout and shutdown also stop its model request.
        packages = path.parent.parent.parent if path.suffix == ".js" else path.parent / "node_modules" / "@openai"
        binary = "codex.exe" if os.name == "nt" else "codex"
        candidates = [*packages.glob(f"codex/vendor/*/codex/{binary}"),
                      *packages.glob(f"codex-*/vendor/*/codex/{binary}"),
                      *packages.glob(f"codex/node_modules/@openai/codex-*/vendor/*/codex/{binary}")]
        path = next(iter(candidates), None)
    return str(path) if path and path.is_file() else None


@lru_cache(maxsize=8)
def _supported(path, modified):
    result = subprocess.run([path,"exec","--help"], capture_output=True, timeout=10,
                            env=_environment(), creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return result.returncode == 0 and all(flag in result.stdout for flag in (b"--ignore-user-config", b"--ephemeral", b"--output-last-message"))


def status():
    path = executable()
    result = {"installed":bool(path), "supported":False, "logged_in":False, "model":None, "thinking":"default"}
    if not path:
        return result
    try:
        result["supported"] = _supported(path,Path(path).stat().st_mtime_ns)
        login = subprocess.run([path,"login","status"], capture_output=True, timeout=10,
                               cwd=Path(path).parent, env=_environment(), creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        result["logged_in"] = login.returncode == 0 and b"chatgpt" in (login.stdout + login.stderr).lower()
        config = Path(os.environ.get("CODEX_HOME",str(Path.home()/".codex"))) / "config.toml"
        data = tomllib.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        model = data.get("model","")
        if isinstance(model,str) and re.fullmatch(r"(?:gpt-|codex-)[A-Za-z0-9._:/-]+",model):
            result["model"] = model
        if data.get("model_reasoning_effort") in {"low","medium","high","xhigh","max","ultra"}:
            result["thinking"] = data["model_reasoning_effort"]
    except (OSError, subprocess.SubprocessError, ValueError):
        return result
    return result


def _settings(instructions, thinking):
    settings = {"approval_policy":"never", "forced_login_method":"chatgpt", "web_search":"disabled",
                "project_doc_max_bytes":0, "model_instructions_file":str(instructions), "hide_agent_reasoning":True,
                "skills.include_instructions":False, "skills.bundled.enabled":False, "skills.max_context_tokens":1,
                "include_apps_instructions":False, "include_collaboration_mode_instructions":False,
                "tools.update_plan.enabled":False, "tools.experimental_request_user_input.enabled":False,
                "agents.enabled":False, "agents.max_depth":0}
    if thinking != "default":
        settings["model_reasoning_effort"] = thinking
    locations = (Path(os.environ.get("CODEX_HOME",str(Path.home()/".codex")))/"skills", Path.home()/".agents"/"skills")
    skills = set()
    for location in locations:
        for pattern in ("*/SKILL.md", ".system/*/SKILL.md"):
            for file in location.glob(pattern):
                skills.update((str(file.absolute()),str(file.resolve())))
    if skills:
        settings["skills.config"] = [{"path":path,"enabled":False} for path in sorted(skills)]
    return settings


def complete(config, messages, register, unregister):
    from .models import _failure, _receipt_sink
    path = executable()
    if not path:
        raise _failure(config,"cli_missing","未找到 Codex CLI，请先安装或设置 TRANTOR_CODEX_BIN")
    try:
        if not _supported(path,Path(path).stat().st_mtime_ns):
            raise _failure(config,"cli_version","当前 Codex CLI 缺少独立临时调用选项，请更新 CLI")
    except (OSError,subprocess.SubprocessError):
        raise _failure(config,"cli_missing","Codex CLI 未能启动") from None
    started = time.monotonic()
    receipt = {"provider":"codex_cli", "protocol":"codex_cli", "requested_model":config.model, "attempt":1,
               "transport":"codex_cli", "http_status":None, "error":None, "output_limit":"prompt_guidance"}
    try:
        with tempfile.TemporaryDirectory(prefix="trantor-model-") as folder:
            root = Path(folder)
            instructions = root/"instructions.md"
            system = "\n\n".join(m["content"] for m in messages if m["role"] in {"system","developer"})
            instructions.write_text("按给定任务处理消息，直接返回要求的最终文本。仅使用消息中的资料，不调用工具。\n"
                                    + system + f"\n\n输出以必要内容为准，建议控制在 {config.max_tokens} token 内。",
                                    encoding="utf-8")
            conversation = [m for m in messages if m["role"] not in {"system","developer"}]
            prompt = conversation[0]["content"] if len(conversation)==1 and conversation[0]["role"]=="user" else json.dumps(conversation,ensure_ascii=False)
            args = [path,"exec","--ignore-user-config","--ephemeral","--skip-git-repo-check",
                    "--sandbox","read-only","--json","--color","never","--model",config.model]
            for key,value in _settings(instructions,config.thinking).items():
                if key == "skills.config":
                    value = "[" + ",".join("{path="+json.dumps(item["path"])+",enabled=false}" for item in value) + "]"
                else:
                    value = json.dumps(value)
                args.extend(["-c",key+"="+value])
            for feature in _DISABLED:
                args.extend(["--disable",feature])
            answer = root/"answer.txt"
            args.extend(["--output-last-message",str(answer),"-"])
            process = subprocess.Popen(args, cwd=root, env=_environment(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            try:
                register(process)
                output, errors = process.communicate(prompt, timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise _failure(config,"cli_timeout","Codex CLI 等待超时，本次调用未完成") from None
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                unregister(process)
            completed = False
            forbidden_tool = False
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event,dict):
                    continue
                if event.get("type") == "turn.completed":
                    completed = True
                    usage = event.get("usage",{})
                    if not isinstance(usage,dict):
                        usage = {}
                    receipt["usage"] = {k:usage[k] for k in ("input_tokens","output_tokens") if type(usage.get(k)) is int and usage[k]>=0}
                    if type(usage.get("cached_input_tokens")) is int and usage["cached_input_tokens"]>=0:
                        receipt["usage"]["cache_read_input_tokens"] = usage["cached_input_tokens"]
                    if type(usage.get("reasoning_output_tokens")) is int and usage["reasoning_output_tokens"]>=0:
                        receipt["usage"]["reasoning_tokens"] = usage["reasoning_output_tokens"]
                item = event.get("item")
                if isinstance(item,dict):
                    forbidden_tool |= item.get("type") in {"command_execution","mcp_tool_call","web_search","file_change"}
            if forbidden_tool:
                raise _failure(config,"cli_tools","CLI 调用了任务上下文以外的工具，结果未采用")
            if process.returncode or not completed or not answer.is_file():
                code = "cli_limit" if any(t in (output+errors).lower() for t in ("usage limit","rate limit","quota exceeded")) else "cli_failed"
                raise _failure(config,code,"Codex CLI 未完成回答，请检查本机登录、模型权限和订阅额度")
            if answer.stat().st_size > 16 * 1024 * 1024:
                raise _failure(config,"cli_response_large","Codex CLI 回答超过单次保存大小")
            text = answer.read_text(encoding="utf-8")
            if not text.strip():
                raise _failure(config,"invalid_response","Codex CLI 没有返回完整文本")
            return text
    except (OSError, UnicodeError):
        receipt["error"] = "cli_io"
        raise _failure(config,"cli_io","Codex CLI 的本机调用未完成") from None
    except Exception as error:
        code = re.search(r"status=([a-z0-9_]+):",str(error))
        receipt["error"] = code[1] if code else "cli_failed"
        raise
    finally:
        receipt["elapsed_ms"] = round((time.monotonic()-started)*1000)
        if sink := _receipt_sink.get():
            sink(receipt)
