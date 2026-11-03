"""Portable project versions with their exact material bytes and a checksum manifest."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .schema import Source

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
ASSET_ENTRY = re.compile(r"materials/([a-f0-9]{32})/(original|source\.json|page-[1-9][0-9]{0,3}\.jpg)")


def _sources(payload):
    assistant = payload.get("assistant") or {}
    groups = [payload["scenario"].get("sources", []), assistant.get("sources", []),
              (assistant.get("proposal") or {}).get("scenario", {}).get("sources", [])]
    for group in groups:
        for item in group:
            source = Source.model_validate(item)
            if source.asset_id:
                yield source


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def export_project(repository, store, project_id, version=None):
    project = repository.get_project(project_id, version)
    selected = {}
    roots = store.directory.resolve()
    for source in _sources(project["payload"]):
        original, folder = store.load(source.asset_id)
        store.check_metadata(source,original)
        if source.asset_id not in selected:
            paths = [folder/"source.json", folder/"original", *sorted(folder.glob("page-*.jpg"))]
            if any(not path.is_file() or not path.resolve().is_relative_to(roots) for path in paths):
                raise ValueError("材料原件不完整，无法导出自包含项目包")
            if _digest(folder/"original") != original.sha256:
                raise ValueError("材料原件校验失败")
            for path in paths:
                if not ASSET_ENTRY.fullmatch(f"materials/{source.asset_id}/{path.name}"):
                    raise ValueError("材料目录含有无法打包的页图名称")
            selected[source.asset_id] = paths
    encoded = json.dumps(project,ensure_ascii=False,allow_nan=False).encode()
    files = [(f"materials/{aid}/{p.name}",p) for aid, paths in selected.items() for p in paths]
    if len(encoded) + sum(p.stat().st_size for _,p in files) > MAX_PACKAGE_BYTES - 1024*1024:
        raise ValueError("项目包超过 256 MiB 上限")
    with tempfile.NamedTemporaryFile(suffix=".zip",delete=False) as temporary:
        path = Path(temporary.name)
    try:
        with ZipFile(path,"w",ZIP_DEFLATED,compresslevel=2) as archive:
            entries = {"project.json":hashlib.sha256(encoded).hexdigest()}
            archive.writestr("project.json",encoded)
            for name,file in files:
                archive.write(file,name)
                entries[name] = _digest(file)
            manifest = {"format":"chuantuo-project","version":1,"sha256":entries}
            archive.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False))
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def import_project(stream, repository, store, validate):
    stream.seek(0,os.SEEK_END)
    if stream.tell() > MAX_PACKAGE_BYTES:
        raise ValueError("项目包超过 256 MiB 上限")
    stream.seek(0)
    repository.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=repository.root,prefix="_import-") as tmp:
        stage = Path(tmp)
        try:
            with ZipFile(stream) as archive:
                entries = archive.infolist()
                names = {e.filename for e in entries}
                if (len(entries) != len(names) or len(entries) > 4096 or sum(e.file_size for e in entries) > MAX_PACKAGE_BYTES
                        or not {"manifest.json","project.json"} <= names):
                    raise ValueError("项目包缺少索引、含重复文件或超过解包上限")
                for entry in entries:
                    if entry.filename not in {"manifest.json","project.json"} and not ASSET_ENTRY.fullmatch(entry.filename):
                        raise ValueError("项目包包含未知或不安全的路径")
                    if entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("项目包不支持加密条目或软链接")
                if archive.getinfo("manifest.json").file_size > 1024*1024:
                    raise ValueError("项目包索引过大")
                manifest = json.loads(archive.read("manifest.json"))
                hashes = manifest.get("sha256")
                if (manifest.get("format") != "chuantuo-project" or manifest.get("version") != 1
                        or not isinstance(hashes,dict) or set(hashes) != names - {"manifest.json"}):
                    raise ValueError("项目包版本或文件清单不匹配")
                for name,expected in hashes.items():
                    dest = stage / name
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(name) as reader, dest.open("xb") as writer:
                        shutil.copyfileobj(reader,writer)
                    if _digest(dest) != expected:
                        raise ValueError(f"项目包校验失败：{name}")
        except (BadZipFile, UnicodeDecodeError, json.JSONDecodeError, AttributeError, NotImplementedError) as error:
            raise ValueError("文件不是完整的川陀项目包") from error
        record = json.loads((stage/"project.json").read_text(encoding="utf-8"))
        if not isinstance(record,dict) or not isinstance(record.get("payload"),dict):
            raise ValueError("项目包缺少场景版本")
        payload = validate(record["payload"])
        assets = {}
        for source in _sources(payload):
            aid = source.asset_id
            if aid not in assets:
                folder = stage/"materials"/aid
                if not (folder/"source.json").is_file() or not (folder/"original").is_file():
                    raise ValueError("项目包缺少被引用的原件")
                original = Source.model_validate_json((folder/"source.json").read_bytes())
                if original.asset_id != aid or _digest(folder/"original") != original.sha256:
                    raise ValueError("原件与材料元数据不符")
                if original.mime_type == "application/pdf" or (original.mime_type or "").startswith("image/"):
                    if any(not (folder/f"page-{p.number}.jpg").is_file() for p in original.pages):
                        raise ValueError("项目包缺少材料页图")
                assets[aid] = original
            store.check_metadata(source,assets[aid])
        embedded = {m.group(1) for name in hashes if (m:=ASSET_ENTRY.fullmatch(name))}
        if set(assets) != embedded:
            raise ValueError("项目包中存在未被场景或草案引用的材料")
        with repository.lock:
            store.directory.mkdir(parents=True,exist_ok=True)
            install, reused = [], 0
            for aid in assets:
                folder, target = stage/"materials"/aid, store.directory/aid
                if target.exists():
                    for source_file in folder.iterdir():
                        local = target/source_file.name
                        if not local.is_file() or _digest(local) != hashes[f"materials/{aid}/{source_file.name}"]:
                            raise ValueError("本机存在同编号的不同材料，导入已停止")
                    reused += 1
                else:
                    install.append((folder,target))
            installed = []
            try:
                for folder,target in install:
                    folder.rename(target)
                    installed.append((folder,target))
                saved = repository.save_project(payload)
            except BaseException:
                for folder,target in reversed(installed):
                    target.rename(folder)
                raise
        return {"project":saved,"installed_assets":len(installed),"reused_assets":reused,
                "source_project":{"id":record.get("id"),"version":record.get("version")}}
