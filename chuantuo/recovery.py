"""Publish a verified copy of a log with an interrupted final write."""
from hashlib import file_digest, sha256
import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from .engine import Journal, Simulation, encode
from .locking import writer_active


def salvage(repository, folder_name):
    root = repository.root.resolve()
    folder = root / folder_name
    if (Path(folder_name).name != folder_name or folder_name.startswith(("_",".")) or
            not folder.is_dir() or folder.resolve().parent != root or folder.is_symlink()):
        raise ValueError("只能恢复资料库中的一个运行目录")
    source = folder / "events.jsonl"
    if not source.is_file() or source.resolve().parent != folder.resolve():
        raise ValueError("原运行日志不存在")
    if writer_active(folder / ".running.lock"):
        raise ValueError("请等待原进程结束写入")
    with source.open("rb") as stream:
        if not stream.seek(0,2):
            raise ValueError("空文件没有可恢复的记录")
        stream.seek(-1,2)
        if stream.read(1) == b"\n":
            raise ValueError("仅支持救回末行写入中断的日志")
        stream.seek(0)
        fingerprint = file_digest(stream,"sha256").hexdigest()
    new_id = sha256(f"chuantuo-salvage-v1/{folder.name}/{fingerprint}".encode()).hexdigest()[:12]
    destination = root / new_id
    with repository.lock:
        if destination.exists():
            existing = Journal.open(destination).snapshot({"run_salvaged"})
            if not any(e["data"].get("source_sha256")==fingerprint for e in existing):
                raise ValueError("恢复目录编号已被另一份记录使用")
            return {"run_id":new_id,**existing[-1]["data"]}
        with TemporaryDirectory(dir=root,prefix="_salvage-") as temporary:
            stage = Path(temporary) / "archive"
            stage.mkdir()
            tail = 0
            source_id = None
            digest = sha256()
            kept_final = False
            with source.open("rb") as reader, (stage/"events.jsonl").open("xb") as writer:
                for index,line in enumerate(reader,1):
                    digest.update(line)
                    try:
                        event = json.loads(line)
                    except (ValueError,UnicodeDecodeError):
                        if line.endswith(b"\n") or index==1:
                            raise ValueError(f"第 {index} 行已损坏，无法单靠末行恢复") from None
                        tail = len(line)
                        continue
                    if not isinstance(event,dict) or event.get("id") != f"e{index:06d}":
                        raise ValueError(f"第 {index} 行编号不连续，停止复制")
                    if index==1:
                        if event.get("kind")!="run_created" or not isinstance(event.get("data"),dict):
                            raise ValueError("无法恢复初始场景")
                        source_id = event["data"].get("run_id")
                        event["data"]["run_id"]=new_id
                        raw = encode(event).encode("utf-8") + b"\n"
                    else:
                        raw = line if line.endswith(b"\n") else line + b"\n"
                    writer.write(raw)
                    kept_final = not line.endswith(b"\n")
            if digest.hexdigest() != fingerprint:
                raise ValueError("原文件在复制时发生变化，请等写入结束后再试")
            restored = Simulation.restore(stage)
            # Markdown may contain a preserved editorial correction in older runs.
            for report in restored.journal.snapshot({"report_created"}):
                for suffix in (".md",".original.md"):
                    name = f"report-{report['id']}{suffix}"
                    original = folder/name
                    if original.is_file() and original.resolve().parent == folder.resolve():
                        copyfile(original,stage/name)
            for name in ("report-correction.json","verification.json","api-calls.jsonl"):
                original = folder/name
                if original.is_file() and original.resolve().parent == folder.resolve():
                    copyfile(original,stage/name)
            receipt = {"source_run_id":source_id,"source_directory":folder.name,"source_sha256":fingerprint,
                       "discarded_tail_bytes":tail,"kept_final_record":kept_final,"committed_window":restored.window}
            restored.journal.append("run_salvaged",receipt,restored.window)
            stage.rename(destination)
    return {"run_id":new_id,**receipt}
