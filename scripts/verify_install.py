"""Build and exercise the distribution in a fresh, separately installed Python 3.12 environment."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",action="store_true",help="用真实浏览器完成新安装包的九窗验收")
    parser.add_argument("--channel",default="msedge" if os.name=="nt" else "chromium")
    parser.add_argument("--wheel-out",type=Path,help="将通过全部检查的安装包保留在指定目录")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="chuantuo-install-") as tmp:
        base = Path(tmp)
        source = base / "source"
        source.mkdir()
        shutil.copy2(project / "pyproject.toml",source)
        shutil.copy2(project / "README.md",source)
        shutil.copytree(project / "chuantuo",source/"chuantuo",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        wheels = base / "wheels"
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1")
        env.pop("PYTHONPATH",None)

        def call(*args):
            subprocess.run(list(map(str,args)),cwd=base,env=env,check=True)

        call(sys.executable,"-m","pip","wheel","--no-deps","--wheel-dir",wheels,source)
        wheel, = wheels.glob("chuantuo-*.whl")
        call(sys.executable,"-m","venv",base/"venv")
        python = base/"venv"/("Scripts/python.exe" if os.name=="nt" else "bin/python")
        call(python,"-m","pip","install","--constraint",project/"requirements.lock.txt",wheel)
        call(python,"-m","pip","check")
        call(python,"-I","-m","chuantuo","example",base/"fixture","--template","release")
        call(python,"-I","-m","chuantuo","run",base/"fixture/scenario.json",base/"fixture/config.json","--output",base/"runs/new","--report")
        call(python,"-I","-m","chuantuo","verify",base/"runs/new")
        call(python,"-I","-c","from pathlib import Path; from fastapi.testclient import TestClient; from chuantuo.web import create_app; "
             "c=TestClient(create_app(Path('runs'))); assert c.get('/api/health').json()['version']=='1.1.0'; "
             "assert c.get('/static/workbench.js').status_code==200 and '准备一场推演' in c.get('/').text; "
             "assert len(c.get('/api/case/replay').json()['windows'])==10 and c.get('/archive').status_code==200; "
             "assert c.get('/api/case/recording').json()['kind']=='recorded_model_run'; "
             "assert c.get('/api/examples/release').json()['scenario']['entities']; assert c.get('/api/runs').json()['runs'][0]['status']=='completed'")
        print(f"Fresh-wheel acceptance passed: {wheel.name}, CLI, packaged template, server assets, history, report and replay.")
        if args.browser:
            call(sys.executable,project/"scripts/browser_v11.py","--installed-python",python,"--channel",args.channel)
        if args.wheel_out:
            import hashlib
            destination=args.wheel_out.resolve()/wheel.name
            destination.parent.mkdir(parents=True,exist_ok=True)
            staged = None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent,prefix=".verified-",suffix=".tmp",delete=False) as target, wheel.open("rb") as source_file:
                    staged = Path(target.name)
                    shutil.copyfileobj(source_file,target)
                    target.flush()
                    os.fsync(target.fileno())
                os.link(staged,destination)
            except FileExistsError:
                raise ValueError("目标已有同名安装包，请选择新的交付目录") from None
            finally:
                if staged:
                    staged.unlink(missing_ok=True)
            with destination.open("rb") as delivered:
                print("Verified wheel:",destination,"SHA256",hashlib.file_digest(delivered,"sha256").hexdigest())


if __name__=="__main__":
    main()
