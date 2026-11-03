"""Run with python -m chuantuo; all data stays in the local workspace."""
import argparse
import json
import sys
from pathlib import Path

from .demo import EXAMPLES, demo_config, demo_scenario, example_scenario
from .engine import Simulation, Journal
from .settlement import verify_commits
from .schema import load_config, load_scenario
from .services import generate_report


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="川陀 · 本地多主体推演工作台")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="运行明确标注的离线演示并生成报告")
    demo.add_argument("--output", type=Path)
    run = commands.add_parser("run", help="按用户提供的场景与模型配置运行")
    run.add_argument("scenario", type=Path)
    run.add_argument("config", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--report", action="store_true")
    run.add_argument("--profile-store",type=Path,default=Path("runs"),help="读取本机模型凭据的工作台资料目录")
    prepare = commands.add_parser("prepare",help="通过已启动的工作台整理文稿，保存可审阅的场景")
    prepare.add_argument("files",nargs="*",type=Path)
    prepare.add_argument("--background",default="")
    prepare.add_argument("--port",type=int,default=8767)
    prepare.add_argument("--no-search",action="store_true")
    prepare.add_argument("--resume-job")
    serve = commands.add_parser("serve", help="启动本地浏览器操作台")
    serve.add_argument("--port", type=int, default=8767)
    serve.add_argument("--runs", type=Path, default=Path("runs"))
    serve.add_argument("--stop-on-stdin-close",action="store_true",help="由父进程管理时，标准输入关闭后等待在办任务并退出")
    export = commands.add_parser("example", help="将演示场景和运行配置写入新目录")
    export.add_argument("directory", type=Path)
    export.add_argument("--template", choices=sorted(EXAMPLES), default="harbor")
    verify = commands.add_parser("verify", help="只读核验运行的轮次、私有观察、消息与结算")
    verify.add_argument("directory", type=Path)
    rescue = commands.add_parser("salvage", help="将末行写入中断的运行救回为独立副本")
    rescue.add_argument("directory", type=Path)
    resume = commands.add_parser("resume", help="从已提交窗口继续一次本地推演")
    resume.add_argument("directory", type=Path)
    resume.add_argument("--retry-uncommitted", action="store_true", help="确认重新调用失败或中断窗口的模型")
    report_command = commands.add_parser("report", help="为已有运行生成带引用的新报告")
    report_command.add_argument("directory", type=Path)
    report_command.add_argument("--max-calls",type=int,default=32)
    report_command.add_argument("--no-reuse",action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        import httpx
        from .client import prepare_at_workbench
        if not 1 <= args.port <= 65535:
            parser.error("端口必须在 1–65535 之间")
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}",trust_env=False,timeout=180) as client:
            result = prepare_at_workbench(client,args.files,args.background,allow_search=not args.no_search,
                                          resume_job=args.resume_job,progress=lambda message:print(message,file=sys.stderr))
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    if args.command == "salvage":
        from .repository import Repository
        from .recovery import salvage
        from .locking import writer_lock
        location = args.directory.resolve()
        with writer_lock(location.parent / ".workbench.lock"):
            print(json.dumps(salvage(Repository(location.parent),location.name),ensure_ascii=False,indent=2))
        return 0
    if args.command == "verify":
        events = Journal.open(args.directory).selected(transport_details=False)
        created = events[0]["data"]
        result = verify_commits(load_scenario(created["scenario"]), load_config(created["config"]), events)
        print(json.dumps({"run_id": created["run_id"], **result},ensure_ascii=False,indent=2))
        return 0
    if args.command in {"resume","report"}:
        from .locking import writer_lock
        from .profile import ModelProfile
        ModelProfile(args.directory.parent)
        with writer_lock(args.directory.parent / ".workbench.lock"):
            simulation = Simulation.restore(args.directory)
            if args.command == "report":
                result = generate_report(simulation,max_calls=args.max_calls,reuse=not args.no_reuse)
                print(args.directory / ("report-" + result["record_id"] + ".md"))
                return 0
            if not simulation.snapshot()["can_resume"]:
                raise ValueError("运行状态或调用上限不允许继续")
            if simulation.status in {"failed", "interrupted"}:
                if not args.retry_uncommitted:
                    raise ValueError("该窗口可能已有模型费用，使用 --retry-uncommitted 显式从上个提交点重试")
                simulation.recover()
            result = simulation.run()
            print(json.dumps(result, ensure_ascii=False,indent=2))
            return int(result["status"] == "failed")
    if args.command == "serve":
        import threading
        import uvicorn
        from .web import create_app
        server=uvicorn.Server(uvicorn.Config(create_app(args.runs),host="127.0.0.1",port=args.port))
        if args.stop_on_stdin_close:
            def wait_for_owner():
                while sys.stdin.buffer.read(4096):
                    pass
                server.should_exit=True
            threading.Thread(target=wait_for_owner,name="workbench-owner",daemon=True).start()
        server.run()
        return 0
    if args.command == "example":
        args.directory.mkdir(parents=True, exist_ok=True)
        for name, value in [("scenario.json", example_scenario(args.template)), ("config.json", demo_config())]:
            with (args.directory / name).open("x", encoding="utf-8") as file:
                file.write(value.model_dump_json(indent=2))
        print(str(args.directory.resolve()))
        return 0
    if args.command == "demo":
        scenario, config = demo_scenario(), demo_config()
    else:
        scenario = load_scenario(json.loads(args.scenario.read_text(encoding="utf-8-sig")))
        config = load_config(json.loads(args.config.read_text(encoding="utf-8-sig")))
        from .profile import ModelProfile
        ModelProfile(args.profile_store)
    from datetime import datetime
    output = args.output or Path("runs") / datetime.now().strftime("demo-%Y%m%d-%H%M%S")
    simulation = Simulation(scenario, config, directory=output)
    result = simulation.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "failed":
        print("失败窗口未提交；详见 events.jsonl。", file=sys.stderr)
        return 1
    if args.command == "demo" or args.report:
        report = generate_report(simulation)
        print(f"报告: {output / ('report-' + report['record_id'] + '.md')}")
    print(f"完整记录: {output / 'events.jsonl'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
