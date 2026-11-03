"""Offline browser acceptance. Install the browser extra and use a local Chromium browser."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


@contextmanager
def _server_instance(data, port, installed_python=None):
    url = f"http://127.0.0.1:{port}" if port else None
    logfile = Path(data) / f"browser-server-{port}.log"
    log = logfile.open("wb")
    interpreter = [str(installed_python), "-I"] if installed_python else [sys.executable]
    working = data if installed_python else Path(__file__).resolve().parents[1]
    process = subprocess.Popen([*interpreter, "-m", "chuantuo", "serve", "--port", str(port), "--runs", str(data),"--stop-on-stdin-close"],
                               stdin=subprocess.PIPE,stdout=log, stderr=log, cwd=working,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(100):
            diagnostics = logfile.read_text(encoding="utf-8",errors="replace")
            if process.poll() is not None:
                raise RuntimeError("本地测试服务未能启动\n" + diagnostics[-1200:])
            if url is None:
                address = re.search(r"Uvicorn running on (http://127\.0\.0\.1:\d+)",diagnostics)
                if address:
                    url = address.group(1)
                else:
                    time.sleep(.1)
                    continue
            try:
                with urlopen(url + "/api/demo", timeout=.4):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("测试服务超时")
        yield url
    finally:
        try:
            process.stdin.close()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
        finally:
            log.close()


@contextmanager
def server(data, port, installed_python=None):
    # Windows can assign SIP and X11 ports which Chromium refuses to navigate.
    for _ in range(6):
        with _server_instance(data, port, installed_python) as url:
            selected = urlsplit(url).port
            if not port and selected in {5060,5061,6000,6566,6665,6666,6667,6668,6669,6697,10080}:
                continue
            yield url
            return
    raise RuntimeError("未取得浏览器可访问的本机端口")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="msedge" if os.name == "nt" else "chromium")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chuantuo-browser-") as folder, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(12000)
        with server(folder, 0) as url:
            port = urlsplit(url).port
            page.goto(url + "/archive")
            page.locator("#assistant-goal").fill("观察工程师向市府传递意见的过程")
            page.locator("#assistant-files").set_input_files({"name":"现场记录.txt","mimeType":"text/plain",
                                                              "buffer":"工程师向市府提出施工建议。".encode()})
            expect(page.locator("#assistant-sources")).to_contain_text("现场记录")
            with page.expect_response(lambda r: "/api/jobs/" in r.url and r.url.endswith("/definition")):
                page.locator("#assistant-generate").click()
            page.reload()
            expect(page.locator("#assistant-proposal")).to_be_visible()
            page.locator("#assistant-accept").click()
            page.locator("#window-count").fill("2")
            page.locator("#window-count").press("Tab")
            page.locator("#start-run").click()
            expect(page.locator("#run-status")).to_have_text("已完成")
            run_url = page.url
            page.locator("#tab-replay").click()
            expect(page.locator("#replay-title")).to_have_text("第 2 个窗口")
            page.locator("#replay-window").fill("1")
            expect(page.locator("#replay-title")).to_have_text("第 1 个窗口")
            page.locator("#replay-actor").select_option("city")
            expect(page.locator("#actor-observation")).to_contain_text("市府")
            assert "PRIVATE_PORT" not in page.locator("#actor-observation").inner_text()
            page.locator("#replay-changes .evidence-link").last.click()
            expect(page.locator("#evidence-dialog")).to_be_visible()
            expect(page.locator("#evidence-body")).to_contain_text("window_committed")
            page.keyboard.press("Escape")
            if args.screenshots:
                page.screenshot(path=str(args.screenshots / "window-replay.png"), full_page=True)
            page.locator("#tab-report").click()
            with page.expect_response(lambda r: "/jobs/" in r.url and r.url.endswith("/report")):
                page.locator("#generate-report").click()
            page.reload()
            expect(page.locator("#report-reading")).to_contain_text("离线流程验证报告")
            page.locator("#report-reading .evidence-link").first.click()
            expect(page.locator("#evidence-dialog")).to_be_visible()
            page.keyboard.press("Escape")
            page.locator("#ask-actor").select_option("city")
            page.locator("#ask-text").fill("你收到过哪些意见？")
            with page.expect_response(lambda r: "/jobs/" in r.url and r.url.endswith("/question")):
                page.locator("#ask-question").click()
            page.reload()
            expect(page.locator("#answers-list")).to_contain_text("主体访谈")
            with page.expect_download() as saved:
                page.locator("#download-report").click()
            assert saved.value.suggested_filename.endswith(".md")
        with server(folder, port) as url:
            page.goto(run_url)
            expect(page.locator("#run-status")).to_have_text("已完成")
            page.locator("#tab-report").click()
            expect(page.locator("#report-reading")).to_contain_text("离线流程验证报告")
            expect(page.locator("#answers-list")).to_contain_text("你收到过哪些意见")
            page.locator("#show-library").click()
            expect(page.locator("#project-list")).to_contain_text("港口")
            expect(page.locator("#run-list")).to_contain_text("1 份报告")
            page.locator("#project-list").get_by_role("button", name="查看版本").first.click()
            page.locator(".version-list button").first.click()
            expect(page.locator("#window-count")).to_have_value("2")
            page.goto(run_url)
            page.locator("#tab-replay").click()
            expect(page.locator("#replay-content")).to_be_visible()
            page.locator("#replay-window").fill("1")
            page.locator("#replay-actor").select_option("engineer")
            page.reload()
            expect(page.locator("#replay-title")).to_have_text("第 1 个窗口")
            expect(page.locator("#replay-actor")).to_have_value("engineer")
            page.locator("#branch-create").locator("xpath=ancestor::details/summary").click()
            page.locator("#branch-title").fill("窗口一对照")
            page.locator("#branch-create").click()
            expect(page.locator("#definition-title")).to_have_text("窗口一对照")
            page.locator("#window-count").fill("1")
            page.locator("#window-count").press("Tab")
            page.locator("#start-run").click()
            expect(page.locator("#run-status")).to_have_text("已完成")
            page.locator("#tab-replay").click()
            page.locator("#compare-box summary").click()
            page.locator("#compare-window").fill("1")
            page.locator("#compare-go").click()
            row = page.locator("#compare-result tr").filter(has_text="city / readiness")
            expect(row).to_be_visible()
            _, left, right, difference = row.locator("td").all_text_contents()
            assert Decimal(difference) == Decimal(right.split()[0]) - Decimal(left.split()[0]) < 0
            page.locator("#show-library").click()
            with page.expect_download() as archive:
                page.locator("#project-list a[download]").first.click()
            package = Path(folder)/"transfer.zip"
            archive.value.save_as(package)
            page.locator("#package-file").set_input_files(package)
            expect(page.locator("#notice")).to_contain_text("独立项目导入")
            page.set_viewport_size({"width":390, "height":844})
            page.locator("#show-library").click()
            expect(page.locator("#run-list")).to_contain_text("已完成")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            assert not errors, errors
            journals = list(Path(folder).glob("*/events.jsonl"))
            assert len(journals) == 2
            first_id=urlsplit(run_url).query.split("run=")[1].split("&")[0]
            events = [json.loads(line) for line in (Path(folder)/first_id/"events.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [e["window"] for e in events if e["kind"] == "window_committed"] == [1, 2]
            assert sum(e["kind"] == "report_created" for e in events) == 1
            assert sum(e["kind"] == "analysis_answer" for e in events) == 1
            print(f"Browser acceptance passed: parent {len(events)} events / 2 commits / 1 report / 1 interview; refresh, restart, linked replay, branch, comparison, package and mobile layout.")
        browser.close()


if __name__ == "__main__":
    main()
