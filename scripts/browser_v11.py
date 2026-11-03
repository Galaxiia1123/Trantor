"""Drive the real product in Edge/Chromium against a local scripted HTTP model, using an isolated store."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_smoke import server
from v11_fixture import ROOT, response


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        incoming = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer local-test-fixture-11":
            self.send_error(401); return
        try:
            data = json.loads(incoming["messages"][-1]["content"])
            answer = response(data)
            if data["task"] == "world" and data["window"] == 9:
                answer["suggested_fields"] = ["如需研究具体收函日期，可由用户另行定义该字段。"]
            self.server.calls.append((incoming["model"], data["task"]))
            raw = json.dumps({"model":incoming["model"],"choices":[{"message":{"content":json.dumps(answer,ensure_ascii=False)}}]}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        except Exception as error:
            self.server.errors.append(repr(error)); self.send_error(400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="msedge" if os.name=="nt" else "chromium")
    parser.add_argument("--screenshots",type=Path)
    parser.add_argument("--installed-python",type=Path,help="从隔离环境中安装的 wheel 启动被测服务与命令行")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(exist_ok=True,parents=True)
    endpoint = ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
    endpoint.calls, endpoint.errors = [], []
    threading.Thread(target=endpoint.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="chuantuo-v11-browser-") as tmp, sync_playwright() as p:
            browser = p.chromium.launch(channel=args.channel,headless=True)
            page = browser.new_page(viewport={"width":1400,"height":950},accept_downloads=True)
            page.set_default_timeout(20000)
            errors=[]; page.on("pageerror",lambda err: errors.append(str(err)))
            with server(tmp,0,args.installed_python) as url:
                page.goto(url); expect(page.get_by_role("heading",name="准备一场推演")).to_be_visible()
                if args.screenshots: page.screenshot(path=args.screenshots/"home.png")
                page.get_by_role("link",name="阅读这次推演").click()
                expect(page.locator("#watch-meta")).to_contain_text("真实接口实录")
                expect(page.locator("#actors")).to_contain_text("实际到账为0万元")
                page.locator("#reports").get_by_role("button",name="阅读").click()
                expect(page.locator("#reading-content")).to_contain_text("芦湾维修")
                page.locator("#reading-content").get_by_role("button",name="e000001",exact=True).first.click()
                expect(page.locator("#reading-title")).to_have_text("记录 e000001"); page.get_by_role("button",name="返回推演报告",exact=False).click()
                page.keyboard.press("Escape"); assert not endpoint.calls
                page.locator("#nav-new").click(); page.locator("#try-letter").click()
                page.get_by_role("button",name="按时送达，并通知运营社",exact=False).click()
                expect(page.locator("#experiment-tools")).to_contain_text("第 2 窗 · 1 项状态不同")
                expect(page.locator(".comparison-side").first).to_contain_text("待签收")
                expect(page.locator(".comparison-side").last).to_contain_text("已排入复核")
                page.get_by_text("并排查看主体的决定",exact=True).click()
                page.get_by_label("选择要比较决定的主体").select_option("desk")
                page.get_by_role("button",name="查看当时的决策现场").last.click()
                expect(page.locator("#reading-content")).to_contain_text("有效签字函已送达收件台")
                page.keyboard.press("Escape")
                page.get_by_role("button",name="已有路线",exact=True).click()
                expect(page.locator("#reading-content")).to_contain_text("已排入复核 · 1 次")
                page.keyboard.press("Escape")
                page.get_by_role("button",name="添加关注",exact=True).click()
                page.get_by_label("观察字段",exact=True).select_option(label="复核安排")
                page.get_by_label("观察方式",exact=True).select_option("equals")
                page.get_by_label("目标值",exact=True).select_option(label="已排入复核")
                page.get_by_label("问题名称（可选）").fill("申请何时被登记？")
                page.get_by_role("button",name="查看结果",exact=True).click()
                expect(page.locator(".watch-finding")).to_contain_text("第 2 窗首次记录达成")
                expect(page.locator(".watch-baseline")).to_contain_text("待签收")
                page.get_by_role("button",name="固定到观察台").click()
                expect(page.locator("#field-watches")).to_contain_text("申请何时被登记？")
                with page.expect_download() as notes: page.get_by_role("link",name="下载观察笔记").click()
                assert "observations" in notes.value.suggested_filename
                page.get_by_role("button",name="主体持续事项").click()
                expect(page.locator("#reading-content")).to_contain_text("三个工作日内等候")
                page.keyboard.press("Escape")
                page.locator("#feed-actor").select_option("desk"); page.locator("#feed-kind").select_option("message_delivered")
                expect(page.locator("#events")).to_contain_text("签章已核对")
                if args.screenshots: page.screenshot(path=args.screenshots/"compare.png")
                page.get_by_role("button",name="再开一条路线",exact=True).click()
                page.get_by_role("button",name="按时送达，暂不通知运营社",exact=False).click()
                expect(page.locator("#experiment-tools")).to_contain_text("第 2 窗 · 0 项状态不同")
                page.get_by_role("button",name="下一窗",exact=True).click()
                page.locator("#feed-actor").select_option("desk")
                page.locator("#feed-kind").select_option("decision_deferred")
                expect(page.locator("#events")).to_contain_text("运营社 · 延续等待")
                page.locator("#events").get_by_role("button",name="当时知道什么").click()
                expect(page.locator("#reading-content")).to_contain_text("没有新的模型请求")
                page.keyboard.press("Escape")
                assert not endpoint.calls
                page.locator("#nav-new").click()
                page.locator("#read-case").click(); expect(page.locator("#watch-meta")).to_contain_text("脚本案例")
                page.locator("#window").select_option("8"); expect(page.locator("#actors")).to_contain_text("已列入复议")
                page.locator("#nav-new").click(); page.locator("#material").set_input_files(ROOT/"chuantuo/templates/tidal-port.md")
                expect(page.locator("#files")).to_contain_text("tidal-port.md"); expect(page.locator("#brief")).to_have_value("")
                page.locator("#prepare").click(); expect(page.locator("#model-dialog")).to_be_visible()
                page.locator("#model-name").fill("local-script-http"); page.locator("#base-url").fill("http://127.0.0.1:"+str(endpoint.server_port)+"/v1")
                page.locator("#model-key").fill("local-test-fixture-11"); page.get_by_text("高级连接设置").click()
                page.locator("#context-limit").fill("2000000"); page.get_by_role("button",name="保存模型").click()
                expect(page.locator("#open-model")).to_contain_text("local-script-http")
                with page.expect_response(lambda r: r.request.method=="POST" and r.url.endswith("/prepare")) as prepared:
                    page.locator("#prepare").click()
                job_id = prepared.value.json()["id"]
                expect(page.locator("#draft")).to_contain_text("9 个窗口")
                expect(page.locator("#draft")).to_contain_text("准备追踪的问题（1）")
                expect(page.locator("#draft")).to_contain_text("已存 · 第 1 版")
                interpreter = [str(args.installed_python),"-I"] if args.installed_python else [sys.executable]
                adoption = subprocess.run([*interpreter,"-m","chuantuo","prepare","--resume-job",job_id,"--port",url.rsplit(":",1)[1]],
                                          cwd=tmp if args.installed_python else ROOT,capture_output=True,encoding="utf-8",timeout=20,check=True)
                assert json.loads(adoption.stdout)["review_url"] == page.url
                page.get_by_text("参与者与有向关系",exact=True).click()
                diagram = page.locator("#draft .relation-graph")
                expect(diagram).to_be_visible()
                diagram.get_by_role("button",name="查看许澄的关系").press("Enter")
                expect(page.locator("#draft .relations select")).to_have_value("engineer")
                expect(page.locator("#draft .relation").first).to_contain_text("许澄")
                page.get_by_text("参与者与有向关系",exact=True).click()
                if args.screenshots: page.locator("#draft").screenshot(path=args.screenshots/"overview.png")
                page.get_by_text("高级编辑",exact=True).click()
                page.get_by_label("运行窗口数",exact=True).fill("0")
                page.get_by_role("button",name="校验并保存修改").click()
                expect(page.locator("#notice")).to_contain_text("运行窗口数")
                page.get_by_label("运行窗口数",exact=True).fill("9")
                page.get_by_text("记忆、并发与失败处理",exact=True).click()
                page.get_by_label("同时请求上限",exact=True).fill("2")
                page.get_by_role("button",name="校验并保存修改").click()
                expect(page.locator("#draft")).to_contain_text("已存 · 第 2 版")
                page.get_by_role("button",name="启动推演").click()
                page.wait_for_url(re.compile(r"#run/[a-f0-9]{12}"), timeout=25000)
                expect(page.locator("#watch-meta")).to_contain_text("已完成",timeout=55000)
                run_url = page.url; rid = run_url.split("#run/")[1]
                expect(page.locator("#field-watches")).to_contain_text("第 8 窗首次记录达成")
                page.locator("#all-windows>summary").click()
                page.locator("#window-list .record-row").filter(has_text="第 8 窗").get_by_role("button",name="回看").click()
                expect(page.locator("#window")).to_have_value("8"); expect(page.locator("#actors")).to_contain_text("资金缺口为2000万元")
                page.locator("#actors .actor").filter(has_text="许澄").get_by_role("button",name="当时可见的信息").click()
                expect(page.locator("#reading-title")).to_have_text("许澄的决策现场")
                page.get_by_label("查看主体决策轮次").select_option("1")
                expect(page.locator("#reading-content")).to_contain_text("持续事项")
                page.keyboard.press("Escape");expect(page.locator("#reading")).to_be_hidden()
                expect(page.locator("#events")).to_contain_text("重新审议")
                page.locator("#events").get_by_role("button", name="记录",exact=False).last.click()
                expect(page.locator("#reading")).to_be_visible()
                expect(page.locator("#reading-content")).to_contain_text("window",ignore_case=True)
                page.keyboard.press("Escape"); expect(page.locator("#reading")).to_be_hidden()
                if args.screenshots: page.screenshot(path=args.screenshots/"window.png")
                page.locator("#write-report").click(); page.get_by_role("button",name="开始生成").click()
                expect(page.locator("#reading-content")).to_contain_text("未发生付款")
                with page.expect_download() as download: page.get_by_role("button",name="下载 Markdown").click()
                assert download.value.suggested_filename.endswith(".md")
                page.keyboard.press("Escape"); page.reload(); expect(page.locator("#reports")).to_contain_text("有条件担保")
                page.locator("#followup>summary").click(); page.locator("#question-scope").select_option("resident")
                page.locator("#question-text").fill("我当时知道哪些夜间施工安排？"); page.locator("#ask").click()
                expect(page.locator("#reading-title")).to_have_text("记录回答"); page.keyboard.press("Escape")
                expect(page.locator("#answers")).to_contain_text("夜间施工")
                page.locator("#window").select_option("9")
                page.get_by_text("世界提出的字段建议（1）",exact=True).click()
                page.get_by_role("button",name="在新场景中采纳").click()
                page.locator("#reading").get_by_label("字段名称",exact=True).fill("收函日期")
                page.locator("#reading").get_by_label("类型",exact=True).select_option("string")
                page.get_by_role("button",name="采纳并建立新场景").click()
                page.wait_for_url(re.compile(r"#project/[a-f0-9]{12}"))
                adopted=page.request.get(url+"/api/projects/"+page.url.split("#project/")[1]).json()["payload"]["scenario"]
                assert any(f.get("label")=="收函日期" and f["value"] is None for a in adopted["entities"] for f in a["fields"])
                page.goto(run_url); expect(page.locator("#watch-meta")).to_contain_text("已完成")
                page.set_viewport_size({"width":390,"height":844}); expect(page.locator("#watch-title")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.locator("#followup>summary").click()
                page.locator("#window").select_option("7"); page.locator("#branch").click()
                expect(page.locator("#draft")).to_contain_text("窗口 7")
                page.locator("#nav-library").click(); expect(page.locator("#projects")).to_contain_text("潮汐港")
                page.locator("#projects button").first.click(); expect(page.locator("#draft")).to_contain_text("9 个窗口")
                assert page.request.get(url+"/api/runs/"+rid+"/reports").json()
                assert len(endpoint.calls)==102
                page.goto(run_url); expect(page.locator("#watch-meta")).to_contain_text("已完成")
                page.get_by_role("button",name="试一个条件",exact=True).click()
                page.get_by_label("从哪里分岔").select_option("8")
                page.get_by_label("改变的条件").fill("一份附有复核日期的新回执已送达市议会。")
                page.get_by_label("开始时谁知道这个条件").select_option("readers")
                page.locator("#reading input[value=council]").check()
                page.get_by_role("button",name="启动新路线",exact=True).click()
                expect(page.locator("#experiment-tools")).to_contain_text("第 9 窗 · 0 项状态不同",timeout=30000)
                assert page.url!=run_url
                variant_id=page.url.split("#run/")[1]
                assert page.request.get(url+"/api/runs/"+variant_id+"/watches").json()[0]["first_hit"]["window"]==8
                filtered=page.request.get(url+"/api/runs/"+variant_id+"/watch?window=9&actor=engineer&kind=expression&limit=1").json()
                assert filtered["total"]==2 and filtered["settlement"]["kind"]=="window_committed"
            assert len([task for _,task in endpoint.calls if task=="agent"]) == 100
            assert len(endpoint.calls)==113 and {m for m,_ in endpoint.calls}=={"local-script-http"},endpoint.calls
            assert not errors and not endpoint.errors,(errors,endpoint.errors)
            browser.close(); print("Browser acceptance passed: offline letter experiment, Markdown intake, 9 windows, 113 local HTTP calls including a one-window fork, linked comparison, report, private interview and mobile.")
    finally:
        endpoint.shutdown(); endpoint.server_close()


if __name__ == "__main__":
    main()
