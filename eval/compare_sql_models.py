"""查数场景对比：同一批归因分析中常问的数据问题，分别交给通用大模型和微调 SQL 模型（app/sqltool.py 的两种模式），
执行生成的 SQL，与人工写的标准 SQL 的结果比对（忽略行序列序，数值相对误差 1e-6）。

用法：python eval/compare_sql_models.py --mode api|sft
  api 需要 .env 里的 LLM_API_KEY；sft 需要 SQL_LLM_BASE_URL（vLLM 部署的 03 微调模型）
结果写到 eval/结果/sql_<mode>.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def cell_eq(a, b, tol=1e-6):
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if a is None or b is None:
        return a is None and b is None
    if num(a) and num(b):
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))
    return (not num(a) and not num(b)) and str(a).strip() == str(b).strip()


def results_match(gold, pred):
    """忽略行序和列序；允许预测多出列；金标准每一列都要在预测里找到取值完全对应的一列。"""
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    key = lambda v: (0, 0.0, "") if v is None else ((1, float(v), "") if isinstance(v, (int, float)) else (2, 0.0, str(v)))
    gcols = [sorted((r[j] for r in gold), key=key) for j in range(len(gold[0]))]
    pcols = [sorted((r[k] for r in pred), key=key) for k in range(len(pred[0]))]
    used = set()
    for g in gcols:
        k = next((k for k, p in enumerate(pcols) if k not in used and all(cell_eq(a, b) for a, b in zip(g, p))), None)
        if k is None:
            return False
        used.add(k)
    return True


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["api", "sft"], required=True)
    args = ap.parse_args()
    from app import sqltool, warehouse
    items = [json.loads(l) for l in open(ROOT / "eval" / "sql_questions.jsonl", encoding="utf-8")]
    rows = []
    for it in items:
        gold = warehouse.query(it["db"], it["gold"]).values.tolist()
        t = time.time()
        r = sqltool.ask(it["db"], it["question"], mode=args.mode, max_rows=5000)
        pred = r.get("rows") if r["type"] == "sql" else None
        ok = pred is not None and results_match(gold, pred)
        rows.append(dict(id=it["id"], db=it["db"], question=it["question"], correct=ok, type=r["type"], sql=r.get("sql"),
                         error=r.get("error") or r.get("message"), repaired=len(r.get("trace", [])) > 1, seconds=round(time.time() - t, 2)))
        print(f"{it['id']} {'✓' if ok else '✗'} {it['question']}  {r['type']}  {(r.get('sql') or r.get('error') or r.get('message') or '')[:160]}")
    acc = sum(r["correct"] for r in rows) / len(rows)
    out = dict(mode=args.mode, model=sqltool.MODES[args.mode], accuracy=acc, n=len(rows), rows=rows)
    p = ROOT / "eval" / "结果" / f"sql_{args.mode}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sqltool.MODES[args.mode]}：{sum(r['correct'] for r in rows)}/{len(rows)} 正确，结果写入 {p}")


if __name__ == "__main__":
    main()
