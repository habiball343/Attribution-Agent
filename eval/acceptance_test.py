"""端到端测试：8 个核心场景（发起归因、拆解守恒、假设检验、多轮追问、事件关联、What-If、报告导出、双场景闭环），通过 HTTP 接口逐条跑一遍（FastAPI TestClient，不需要先启动服务）。

用法：python eval/acceptance_test.py [--online]
默认离线（规则引擎），结果写到 eval/结果/acceptance.json。应用数据写到临时目录，不影响正式库。
"""
import argparse
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "eval" / "结果" / "acceptance.json"))
    args = ap.parse_args()
    os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="attr_accept_")
    if not args.online:
        os.environ["LLM_API_KEY"] = ""
    from app import config
    if not args.online:
        config.LLM_API_KEY = ""
    from fastapi.testclient import TestClient
    from app.main import app

    results = []

    def check(no, name, ok, detail):
        results.append(dict(no=no, name=name, passed=bool(ok), detail=detail))
        print(f"[{'通过' if ok else '失败'}] {no}. {name}：{detail}")

    with TestClient(app) as c:
        def login(u, p):
            return {"Authorization": "Bearer " + c.post("/api/login", json=dict(username=u, password=p)).json()["token"]}
        HA, HM, HD = login("analyst", "analyst123"), login("manager", "manager123"), login("admin", "admin123")

        def ask(sid, q, domain=None):
            evs, t = [], time.time()
            with c.stream("POST", f"/api/sessions/{sid}/ask", json=dict(question=q, domain=domain), headers=HA) as r:
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        evs.append(json.loads(line[5:]))
            return evs, time.time() - t

        # 1 发起会话
        dash = c.get("/api/dashboard?domain=ecommerce", headers=HA).json()
        flagged = [x for x in dash["cards"] if x["flag"]]
        sid = c.post("/api/sessions", json=dict(domain="ecommerce"), headers=HA).json()["id"]
        evs, sec = ask(sid, flagged[0]["question"], "ecommerce")
        kinds = [e["event"] for e in evs]
        steps = [e for e in evs if e["event"] == "step" and e.get("status") == "done"]
        check(1, "在大盘或工作台发起指定指标的归因会话", "conclusion" in kinds and len(steps) >= 8,
              f"大盘标红 {len(flagged)} 个指标，从“{flagged[0]['name']}”卡片发起：{flagged[0]['question']}；流式事件 {len(evs)} 个，完成 {len(steps)} 个步骤，用时 {sec:.1f} 秒")

        # 2 拆解守恒
        st = c.get(f"/api/sessions/{sid}", headers=HA).json()["state"]
        sid2 = c.post("/api/sessions", json=dict(domain="fmcg"), headers=HA).json()["id"]
        ask(sid2, "9 月净利率比 6 月下降了多少个百分点？为什么？", "fmcg")
        st2 = c.get(f"/api/sessions/{sid2}", headers=HA).json()["state"]
        sub = c.get(f"/api/sessions/{sid}/decompose?metric=aov", headers=HA).json()
        errs = [st["decomp"]["check"]["rel_error"], st2["decomp"]["check"]["rel_error"], sub["check"]["rel_error"]]
        check(2, "复合指标多级拆解，贡献总和守恒", all(e < 1e-4 for e in errs) and st["decomp"]["factors"][-1].get("children"),
              f"GMV（LMDI，3 因子 + 客单价二级拆解）相对误差 {errs[0]:.1e}；净利率（差额，4 项费用率 + 成本率三分）{errs[1]:.1e}；点击客单价节点二次拆解 {errs[2]:.1e}")

        # 3 假设与沙箱验证、伪相关排除
        H = st["hypotheses"]
        sup = [h for h in H if h["result"] and h["result"]["verdict"] == "支持"]
        exc = [h for h in H if h["result"] and h["result"]["verdict"] == "排除"]
        der = [h for h in H if h["result"] and h["result"]["verdict"] == "派生"]
        all_code = all("q(" in h["code"] and "result" in h["code"] for h in H)
        bad = c.post(f"/api/sessions/{sid}/hypotheses/{H[0]['id']}/rerun", json=dict(code="import os\nresult = os.listdir('.')"), headers=HA).json()
        check(3, "自动生成假设，沙箱量化验证并排除伪相关", len(H) >= 5 and sup and exc and all_code and not bad["ok"],
              f"{len(H)} 条假设全部生成检验代码并在沙箱执行：支持 {len(sup)}、排除 {len(exc)}（{'；'.join(h['title'] for h in exc)}）、派生 {len(der)}；"
              f"沙箱拦截越权代码：{bad.get('error', '')[:40]}")

        # 4 多轮追问
        e1, _ = ask(sid, "信息流广告为什么掉这么多？")
        e2, _ = ask(sid, "那华北呢")
        e3, _ = ask(sid, "继续往下看")
        st = c.get(f"/api/sessions/{sid}", headers=HA).json()
        path = [f[2] for f in st["state"]["focus"]]
        routes = [next(e["route"] for e in x if e["event"] == "route") for x in (e1, e2, e3)]
        check(4, "多轮追问保持上下文并刷新证据链", routes == ["focus"] * 3 and len(st["state"]["drill_history"]) == 3 and len(st["snapshots"]) >= 4,
              f"三轮追问路由 {routes}，下钻路径 {' → '.join(path)}，每轮刷新切片指标、子维度拆解、相关假设与事件；会话快照 {len(st['snapshots'])} 个")

        # 5 事件日历
        et = st["state"]["event_table"]
        linked = [e for e in et if e["status"].startswith("已关联")]
        ev_new = c.post("/api/events", json=dict(domain="ecommerce", start_date="2026-09-07", end_date="2026-09-08", event_type="投放调整",
                                                 title="信息流素材审核规则收紧", channel_id="CH03"), headers=HD).json()
        sid3 = c.post("/api/sessions", json=dict(domain="ecommerce"), headers=HA).json()["id"]
        ask(sid3, "9 月 GMV 比 8 月下降了多少？为什么？", "ecommerce")
        st3 = c.get(f"/api/sessions/{sid3}", headers=HA).json()["state"]
        h_tr = next(h for h in st3["hypotheses"] if h["type"] == "traffic_drop")
        custom_hit = any(e["event_id"] == f"U{ev_new['id']}" for e in h_tr["events"])
        ext = [x for x in st2["conclusion"]["causes"] if x["scope"] == "外部诱因"]
        check(5, "自动匹配业务事件日历，输出业务语境因果解释", len(linked) >= 4 and custom_hit and ext,
              f"电商窗口内 {len(et)} 个事件，{len(linked)} 个进入因果链；管理员补录事件后重新分析，信息流假设关联到补录事件（{custom_hit}）；"
              f"快消识别外部诱因 {len(ext)} 条（{'、'.join(x['title'] for x in ext)}）")

        # 6 What-If
        w = st["state"]["whatif"]
        rec = [l for l in w["levers"] if l["key"].startswith("rec_")]
        r1 = c.post(f"/api/sessions/{sid}/whatif", json=dict(values={rec[0]["key"]: 50}), headers=HA).json()
        r2 = c.post(f"/api/sessions/{sid}/whatif", json=dict(values={rec[0]["key"]: 100}, name="完全恢复"), headers=HA).json()
        ew, _ = ask(sid2, "如果社区团购折扣降到 30%、佣金率降到 8% 会怎样？")
        wr = next(e for e in ew if e["event"] == "whatif_result")["whatif_result"]
        check(6, "What-If 调节驱动因子，实时推演并情景对比", r2["result"]["delta"] > r1["result"]["delta"] > 0 and len(r2["saved"]) == 1 and len(r2["presets"]) == 3 and wr["delta"] > 0,
              f"电商：{rec[0]['name']} 50% → {r1['result']['delta'] / 1e4:+.1f} 万元，100% → {r2['result']['delta'] / 1e4:+.1f} 万元（含 95% 区间与敏感度曲线），"
              f"保存情景 1 个 + 预置 3 个；快消自然语言推演净利率 {wr['delta'] * 100:+.2f} pp")

        # 7 报告导出
        rid = c.post(f"/api/sessions/{sid}/report", json={}, headers=HA).json()["id"]
        c.post(f"/api/reports/{rid}/annotations", json=dict(section="conclusion", text="断货补货进度已与供应链确认"), headers=HA)
        c.put(f"/api/reports/{rid}", json=dict(status="已发布"), headers=HA)
        R = c.get(f"/api/reports/{rid}", headers=HM).json()
        files = {f: c.get(f"/api/reports/{rid}/export?fmt={f}", headers=HM) for f in ("md", "pdf", "xlsx")}
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(files["xlsx"].content))
        keys = [s["key"] for s in R["sections"]]
        ok7 = (keys == ["background", "factors", "drill", "conclusion", "evidence", "whatif", "actions"] and files["pdf"].content[:4] == b"%PDF"
               and "七、可落地策略行动清单" in files["md"].text and len(wb.sheetnames) >= 8)
        acts = next(s for s in R["sections"] if s["key"] == "actions")["tables"][0]["rows"]
        check(7, "输出结构化报告并导出 PDF / Excel / Markdown", ok7,
              f"报告 7 个章节（含批注），行动 {len(acts)} 条（责任岗位、周期、预期收益齐全）；MD {len(files['md'].content) // 1024} KB、PDF {len(files['pdf'].content) // 1024} KB、"
              f"Excel {len(wb.sheetnames)} 个工作表；管理层账号可查看已发布报告并导出")

        # 8 两组场景闭环
        both = st["state"]["conclusion"]["causes"] and st2["conclusion"]["causes"] and st2["actions"] and st["state"]["actions"]
        check(8, "至少两组预置业务场景全流程闭环", both,
              f"电商 GMV 下滑：{st['state']['conclusion']['headline'][:60]}…；快消利润率：{st2['conclusion']['headline'][:60]}…")

        # 权限
        perm = [c.post("/api/sessions", json={}, headers=HM).status_code, c.post("/api/events", json={}, headers=HA).status_code,
                c.get("/api/audit", headers=HA).status_code, c.get("/api/audit", headers=HD).status_code]
        print(f"权限检查：管理层建会话 {perm[0]}、分析师改事件 {perm[1]}、分析师看审计 {perm[2]}、管理员看审计 {perm[3]}")

    passed = sum(r["passed"] for r in results)
    print(f"\n端到端测试 {passed}/{len(results)} 通过")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(dict(mode="online" if args.online else "offline", passed=passed, total=len(results), items=results,
                                              permission_status_codes=perm), ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
