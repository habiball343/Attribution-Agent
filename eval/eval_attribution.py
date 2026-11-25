"""归因准确性评测：用 Agent 跑两个预置场景，和数据生成时留下的标准答案对比。

标准答案只在这里读取，Agent 运行时不读（app/ 下没有任何代码引用标准答案文件）。
指标：
  拆解守恒        各因子贡献之和与总变动的相对误差；与参考分解（LMDI / 差额）的逐因子误差
  根因命中率      标准答案中的每条根因，Agent 是否给出了对应的“支持”结论
  主次判定一致率  命中的根因中，主要/次要/反向的判定是否与标准答案一致
  定量误差        命中根因的影响估计与真实影响的相对误差（电商：万元；快消：净利率百分点）
  干扰项排除率    标准答案列出的干扰项（伪相关），Agent 是否检验并排除
  派生现象识别率  标准答案列出的派生现象，Agent 是否识别为派生并归到正确的根因
  事件关联率      标准答案中带事件证据的根因，Agent 是否关联到同一个事件
  多余主因数      Agent 判为“主要”但不在标准答案根因里的原因数（误报）

用法：python eval/eval_attribution.py [--answers 数据目录] [--online]
默认离线（规则引擎）；--online 时使用 .env 里配置的大模型（意图解析、补充假设、结论撰写）。
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCENARIOS = [("标准答案_电商GMV.json", "9 月 GMV 比 8 月下降了多少？为什么？"),
             ("标准答案_快消利润率.json", "9 月净利率比 6 月下降了多少个百分点？为什么？")]


def bigrams(t):
    t = re.sub(r"[^一-鿿A-Za-z0-9]", "", t or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def sim(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, min(len(A), len(B)))


def events_in(text):
    return set(re.findall(r"\b(F?E\d{2})\b", text or ""))


def match_cause(truth, causes):
    """先按事件编号对上，再按名称相似度（二字词重合）。"""
    ev = set()
    for e in truth.get("evidence", []):
        ev |= events_in(e)
    best, score = None, 0.0
    for c in causes:
        cev = {e["event_id"] for e in c["events"]}
        s = 1.0 if ev & cev else sim(truth["name"] + truth.get("mechanism", "")[:20], c["title"] + c.get("slice", ""))
        if s > score:
            best, score = c, s
    return (best, score) if score >= 0.25 else (None, score)


def run_one(path, question):
    from app import agent
    truth = json.loads(Path(path).read_text(encoding="utf-8"))
    t0 = time.time()
    A = agent.run(question)
    elapsed = time.time() - t0
    eco = "ecommerce" in truth["database"]
    unit = 1e4 if eco else 0.01
    c = A["conclusion"]
    dec = A["decomp"]
    out = dict(scenario=truth["scenario"], question=question, elapsed_s=round(elapsed, 1),
               llm_hypotheses=[dict(id=h["id"], title=h["title"], verdict=(h.get("result") or {}).get("verdict"), repaired=bool(h.get("repaired")),
                                    finding=(h.get("result") or {}).get("finding"), error=h.get("error")) for h in A["hypotheses"] if h.get("source") == "大模型"],
               narrative=A.get("narrative"))
    # 1 拆解
    ref = truth["reference_decomposition"]
    if eco:
        ours = {f["key"]: f["contrib"] for f in dec["factors"]}
        pairs = [("uv", "访客数"), ("cvr", "支付转化率"), ("aov", "客单价")]
        fac = [dict(factor=n, ours=ours[k] / 1e4, reference=ref[k] / 1e4) for k, n in pairs]
        total_true = truth["actual"]["change"]
    else:
        ours = {f["key"]: f["contrib"] * 100 for f in dec["factors"]}
        pairs = [("cogs_rate", "产品成本率"), ("logistics_rate", "物流费用率"), ("commission_rate", "佣金率"), ("marketing_rate", "营销费用率")]
        fac = [dict(factor=n, ours=ours[k], reference=ref[k]) for k, n in pairs]
        total_true = truth["actual"]["net_margin_change_pp"] / 100
    out["decomposition"] = dict(sum_rel_error=dec["check"]["rel_error"], conserved=dec["check"]["ok"], total_ours=dec["change"] / unit, total_true=total_true / unit,
                                factors=fac, max_factor_abs_error=max(abs(x["ours"] - x["reference"]) for x in fac))
    # 2-4 根因
    supported = [x for x in c["causes"] if x["rank"] != "口径因素"]
    rows, used = [], set()
    for t in truth["root_causes"]:
        m, s = match_cause(t, supported)
        true_eff = t.get("true_impact") if eco else t.get("true_impact_net_pp")
        true_eff = true_eff / 1e4 if eco else true_eff
        row = dict(id=t["id"], name=t["name"], true_rank=t["rank"], true_effect=round(true_eff, 2), hit=bool(m))
        if m:
            used.add(m["id"])
            ours_eff = m["effect"] / unit
            rank_map = {"主要": "主要", "次要": "次要", "轻微": "次要", "反向因素": "反向因素"}
            row.update(matched=f"{m['id']} {m['title']}", our_rank=m["rank"], rank_ok=rank_map.get(m["rank"]) == t["rank"],
                       our_effect=round(ours_eff, 2), rel_error=round(abs(ours_eff - true_eff) / abs(true_eff), 3) if true_eff else None)
            tev = set()
            for e in t.get("evidence", []):
                tev |= events_in(e)
            if tev:
                row["event_ok"] = bool(tev & {e["event_id"] for e in m["events"]})
        rows.append(row)
    out["root_causes"] = rows
    hits = [r for r in rows if r["hit"]]
    out["hit_rate"] = len(hits) / len(rows)
    out["rank_agreement"] = sum(r["rank_ok"] for r in hits) / max(1, len(hits))
    errs = [r["rel_error"] for r in hits if r.get("rel_error") is not None]
    out["mean_rel_error"] = sum(errs) / max(1, len(errs))
    out["within_25pct"] = sum(e <= 0.25 for e in errs) / max(1, len(errs))
    evr = [r["event_ok"] for r in hits if "event_ok" in r]
    out["event_link_rate"] = sum(evr) / max(1, len(evr))
    extra = [x for x in c["causes"] if x["rank"] == "主要" and x["id"] not in used]
    out["extra_major"] = [f"{x['id']} {x['title']}" for x in extra]
    out["other_causes"] = [dict(id=x["id"], title=x["title"], rank=x["rank"], effect=round(x["effect"] / unit, 2)) for x in c["causes"] if x["id"] not in used]
    # 5 干扰项
    hyps = A["hypotheses"]
    rh = []
    for r in truth.get("red_herrings", []):
        best = max(hyps, key=lambda h: sim(r["name"], h["title"] + "".join(e["title"] for e in h.get("events", []))))
        v = (best.get("result") or {}).get("verdict")
        rh.append(dict(id=r["id"], name=r["name"], tested=sim(r["name"], best["title"] + "".join(e["title"] for e in best.get("events", []))) >= 0.25,
                       hypothesis=f"{best['id']} {best['title']}", verdict=v, excluded=v == "排除", finding=(best.get("result") or {}).get("finding")))
    out["red_herrings"] = rh
    out["exclusion_rate"] = sum(x["tested"] and x["excluded"] for x in rh) / max(1, len(rh))
    # 6 派生
    dv = []
    hit_parent = {r["id"]: r.get("matched", "").split(" ")[0] for r in rows}
    for d in truth.get("derived_phenomena", []):
        best = max(c["derived"], key=lambda x: sim(d["name"], x["phenomenon"]), default=None)
        ok = best is not None and sim(d["name"], best["phenomenon"]) >= 0.25
        cid = re.search(r"C\d", d["explanation"])
        parent_ok = None
        if ok and cid:
            parent_ok = best["parent"] == hit_parent.get(cid.group(0))
        elif ok:  # 说明里没写编号的（如“社区团购增长后…”），按根因名称对
            parent_ok = any(best["parent"] == hit_parent.get(t["id"]) and sim(t["name"], d["explanation"]) >= 0.2 for t in truth["root_causes"])
        dv.append(dict(name=d["name"], identified=ok, ours=best["phenomenon"] if ok else None, parent=best["parent_title"] if ok else None, parent_ok=parent_ok))
    out["derived"] = dv
    out["derived_rate"] = sum(x["identified"] and x["parent_ok"] is not False for x in dv) / max(1, len(dv))
    out["explained_share"] = c["explained_share"]
    out["residual"] = c["residual"] / unit
    out["true_other"] = (truth["other_effects"]["impact"] / 1e4) if eco else truth["other_effects"]["impact_net_pp"]
    out["hypotheses"] = len(hyps)
    out["sandbox_failures"] = sum(1 for h in hyps if not h.get("result"))
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", default=str(ROOT / "数据"), help="标准答案 JSON 所在目录")
    ap.add_argument("--online", action="store_true", help="使用 .env 中的大模型")
    ap.add_argument("--out", default=str(ROOT / "eval" / "结果" / "attribution.json"))
    args = ap.parse_args()
    if not args.online:
        os.environ["LLM_API_KEY"] = ""
    from app import config
    if not args.online:
        config.LLM_API_KEY = ""
    res = []
    for fn, q in SCENARIOS:
        r = run_one(Path(args.answers) / fn, q)
        res.append(r)
        u = "万元" if "GMV" in r["scenario"] else "pp"
        print(f"\n== {r['scenario']}（{r['elapsed_s']} 秒，{r['hypotheses']} 条假设，沙箱失败 {r['sandbox_failures']}）")
        d = r["decomposition"]
        print(f"拆解：总变动 {d['total_ours']:.2f} / 标准 {d['total_true']:.2f} {u}；守恒误差 {d['sum_rel_error']:.1e}；与参考分解最大因子误差 {d['max_factor_abs_error']:.3f} {u}")
        for x in r["root_causes"]:
            print(f"  {x['id']} {x['name']}（{x['true_rank']}，{x['true_effect']} {u}） → " + (f"{x['matched']}｜{x['our_rank']}{'✓' if x['rank_ok'] else '✗'}｜{x['our_effect']} {u}｜误差 {x['rel_error']:.0%}"
                                                                                       + (f"｜事件{'✓' if x.get('event_ok') else '✗'}" if 'event_ok' in x else "") if x["hit"] else "未命中"))
        for x in r["red_herrings"]:
            print(f"  干扰项 {x['name']} → {x['hypothesis']}｜{x['verdict']}")
        for x in r["derived"]:
            print(f"  派生 {x['name']} → {x['ours']}｜归属 {x['parent']}｜{'✓' if x['identified'] and x['parent_ok'] is not False else '✗'}")
        print(f"  其他原因：" + "；".join(f"{o['title']}（{o['rank']}，{o['effect']}）" for o in r["other_causes"]))
        print(f"命中率 {r['hit_rate']:.0%}｜主次一致 {r['rank_agreement']:.0%}｜平均相对误差 {r['mean_rel_error']:.0%}（25% 以内 {r['within_25pct']:.0%}）｜事件关联 {r['event_link_rate']:.0%}"
              f"｜干扰项排除 {r['exclusion_rate']:.0%}｜派生识别 {r['derived_rate']:.0%}｜多余主因 {len(r['extra_major'])}｜未解释 {r['residual']:.2f}（标准答案“其他” {r['true_other']:.2f}）")
    from app import llm
    summary = dict(mode="online" if args.online else "offline", model=config.LLM_MODEL if args.online else None, llm_usage=dict(llm.USAGE), scenarios=res,
                   overall=dict(hit_rate=sum(r["hit_rate"] for r in res) / len(res), rank_agreement=sum(r["rank_agreement"] for r in res) / len(res),
                                mean_rel_error=sum(r["mean_rel_error"] for r in res) / len(res), exclusion_rate=sum(r["exclusion_rate"] for r in res) / len(res),
                                derived_rate=sum(r["derived_rate"] for r in res) / len(res), event_link_rate=sum(r["event_link_rate"] for r in res) / len(res),
                                conserved=all(r["decomposition"]["conserved"] for r in res)))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    from app.agent import clean
    Path(args.out).write_text(json.dumps(clean(summary), ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n总体：" + "，".join(f"{k}={v:.0%}" if isinstance(v, float) else f"{k}={v}" for k, v in summary["overall"].items()))
    print(f"大模型用量：{llm.USAGE}")
    print(f"结果已写入 {args.out}")


if __name__ == "__main__":
    main()
