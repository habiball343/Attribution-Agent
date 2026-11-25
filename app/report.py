"""归因报告：把会话的分析上下文组装成结构化报告（七个部分），导出 Markdown、Excel、PDF。

build(A, meta) → report dict（sections 列表，每节有 text / tables / chart）
to_markdown / to_excel / to_pdf 三种导出共用同一个 report dict，保证内容一致。
"""
import io
from datetime import datetime

DIR = {True: "拉高", False: "拉低"}


def _money(v):
    return "—" if v is None else f"{v / 1e4:,.1f} 万元"


def _pp(v):
    return "—" if v is None else f"{v * 100:+.2f} pp"


def _pct(v):
    return "—" if v is None else f"{v:.2%}"


def build(A, meta=None):
    meta = meta or {}
    it, dec, c = A["intent"], A["decomp"], A["conclusion"]
    eco = it["domain"] == "ecommerce"
    val = (lambda v: _money(v)) if eco else (lambda v: _pct(v))
    eff = (lambda v: f"{v / 1e4:+,.1f} 万元") if eco else (lambda v: f"{v * 100:+.2f} pp")
    S = []
    # 1 异动背景
    rows = [["目标指标", dec["name"]], ["统计周期（现期）", f"{it['current']['start']} 至 {it['current']['end']}"],
            ["对比基期", f"{it['base']['start']} 至 {it['base']['end']}（{it['compare']}）"], ["基期值", val(dec["v0"])], ["现期值", val(dec["v1"])],
            ["总差异", c["total_text"]]]
    if dec.get("change_rate") is not None:
        rows.append(["差异率", f"{dec['change_rate']:+.2%}"])
    if dec.get("daily_change_rate") is not None:
        rows.append(["日均口径差异率", f"{dec['daily_change_rate']:+.2%}（剔除天数差异）"])
    S.append(dict(key="background", title="一、异动背景与问题定义", text=A.get("question", ""), tables=[dict(name="指标概况", header=["项目", "数值"], rows=rows)]))
    # 2 因子拆解
    frows = []
    for f in dec["factors"]:
        frows.append([f["name"], _fmt_v(f["key"], f["v0"], eco), _fmt_v(f["key"], f["v1"], eco), eff(f["contrib"]), f"{f['share']:.1%}", DIR[f["contrib"] > 0]])
        for ch in f.get("children", []):
            frows.append(["　└ " + ch["name"], _fmt_v(ch["key"], ch.get("v0"), eco), _fmt_v(ch["key"], ch.get("v1"), eco), eff(ch["contrib"]), f"{ch['share']:.1%}", DIR[ch["contrib"] > 0]])
    chk = dec.get("check") or {}
    S.append(dict(key="factors", title="二、因子贡献率拆解", text=f"方法：{dec['method']}。各因子贡献之和与总变动之差 {chk.get('diff', 0):.2e}，{'总和守恒' if chk.get('ok') else '存在残差'}。",
                  tables=[dict(name="因子贡献", header=["因子", "基期", "现期", "贡献", "贡献率", "方向"], rows=frows)],
                  chart=dict(type="waterfall", start=dec["v0"], end=dec["v1"], items=[(f["name"], f["contrib"]) for f in dec["factors"]], eco=eco)))
    # 3 下钻
    tables = []
    for d in A["drills"]:
        if eco:
            rows = [[r["key"], _money(r["v0"]), _money(r["v1"]), eff(r["change"]), f"{r['contrib_share']:.1%}", _pct(r.get("share0")) + " → " + _pct(r.get("share1"))] for r in d["rows"]]
            tables.append(dict(name=f"按{d['dim_name']}", header=[d["dim_name"], "基期", "现期", "差额", "贡献率", "占比变化"], rows=rows))
        else:
            rows = [[r["key"], _pct(r["share0"]) + " → " + _pct(r["share1"]), _pct(r["v0"]) + " → " + _pct(r["v1"]), eff(r["mix"]), eff(r["rate"]), eff(r["change"])] for r in d["rows"]]
            tables.append(dict(name=f"按{d['dim_name']}", header=[d["dim_name"], "营收占比", "利润率", "结构效应", "水平效应", "合计"], rows=rows))
    if A.get("cross"):
        cr = A["cross"]
        tables.append(dict(name="交叉下钻 TOP 组合（" + " × ".join(cr["dims"]) + "）", header=[cr["dims"][0], cr["dims"][1], "基期", "现期", "差额", "贡献率"],
                           rows=[[r["d1"], r["d2"], _money(r["v0"]), _money(r["v1"]), eff(r["change"]), f"{r['contrib_share']:.1%}"] for r in cr["rows"]]))
    for fh in A.get("drill_history", [])[-3:]:
        tables.append(dict(name=f"追问下钻：{fh['dim_name']}={fh['name']}", header=["指标", "基期", "现期"],
                           rows=[[m["name"], _fmt_v(m["key"], m["v0"], eco), _fmt_v(m["key"], m["v1"], eco)] for m in fh["metrics"]]))
    top_txt = "；".join(f"{d['dim_name']}：{d['rows'][0]['key']}（{eff(d['rows'][0]['change'])}）" for d in A["drills"])
    S.append(dict(key="drill", title="三、细分维度下钻证据", text="变动最集中的维度取值 — " + top_txt + "。" +
                  ("".join(f"\n追问下钻（{fh['name']}）：" + "；".join(fh["points"]) for fh in A.get("drill_history", [])[-3:])), tables=tables))
    # 4 核心结论
    crow = [[x["id"], x["rank"], x["title"], x["effect_text"], f"{x['share']:.0%}", x["nature"], x["scope"], "、".join(e["title"] for e in x["events"]) or "—"] for x in c["causes"]]
    crow.append(["—", "其他", "自然波动与未识别因素", c["residual_text"], f"{c['residual'] / c['total']:.0%}" if c["total"] else "—", "—", "—", "—"])
    S.append(dict(key="conclusion", title="四、核心归因结论", text=A.get("narrative") or c["headline"],
                  tables=[dict(name="原因排序", header=["编号", "等级", "原因", "影响", "占总变动", "性质", "内/外部", "关联事件"], rows=crow)]))
    # 5 证据链
    ev_rows = [[h["id"], h["group"], h["title"], (h.get("result") or {}).get("verdict", "失败"), f"{(h.get('result') or {}).get('confidence', 0):.2f}",
                _p((h.get("result") or {}).get("p_value")), (h.get("result") or {}).get("finding", h.get("error", ""))] for h in A["hypotheses"]]
    chain_rows = [[x["cause"], x["event"], x["mechanism"], x["factor"], x["effect"], f"{x['confidence']:.2f}"] for x in c["chain"]]
    S.append(dict(key="evidence", title="五、因果证据链与检验置信度", text=f"共 {len(A['hypotheses'])} 条候选假设，全部在只读沙箱中执行检验代码；置信度低于阈值的自动排除。"
                  + ("派生现象：" + "；".join(f"{d['phenomenon']}（属于 {d['parent']}）" for d in c["derived"]) if c["derived"] else ""),
                  tables=[dict(name="因果链", header=["原因", "关联事件", "机制", "影响因子", "影响", "置信度"], rows=chain_rows),
                          dict(name="假设检验明细", header=["编号", "类别", "假设", "结论", "置信度", "p 值", "检验发现"], rows=ev_rows),
                          dict(name="窗口内业务事件", header=["事件", "类型", "日期", "状态", "关联假设"],
                               rows=[[f"{e['event_id']} {e['title']}", e["event_type"], e["start"] + ("" if e["end"] == e["start"] else f" 至 {e['end']}"), e["status"], e["hypothesis"] or "—"]
                                     for e in A.get("event_table", [])])]))
    # 6 What-If
    w = A.get("whatif") or {}
    fv = (lambda v: _money(v)) if eco else (lambda v: _pct(v))
    wrows = [[s["name"], fv(s["predicted"]), eff(s["delta"]), f"{fv(s['band'][0])} ~ {fv(s['band'][1])}"] for s in A.get("whatif_compare", [])]
    cur = A.get("whatif_result")
    if cur and any(abs(x["delta"]) > 0 for x in cur.get("contrib", [])):
        wrows.append(["当前模拟器参数", fv(cur["predicted"]), eff(cur["delta"]), f"{fv(cur['band'][0])} ~ {fv(cur['band'][1])}"])
    for s in A.get("saved_scenarios", []):
        wrows.append([s["name"], fv(s["predicted"]), eff(s["delta"]), f"{fv(s['band'][0])} ~ {fv(s['band'][1])}"])
    S.append(dict(key="whatif", title="六、What-If 情景模拟结论", text=(w.get("note", "") + "。区间为 95% 区间（参数 ±30% 与历史波动）。"),
                  tables=[dict(name="情景对比", header=["情景", f"预测{w.get('metric_name', '')}", "较基线", "95% 区间"], rows=wrows),
                          dict(name="杠杆说明", header=["杠杆", "范围", "说明"], rows=[[l["name"], f"{l['min']} ~ {l['max']} {l['unit']}", l["desc"]] for l in w.get("levers", [])])]))
    # 7 行动
    arows = [[a["priority"], a["owner"], a["action"], a["period"], a["amount_text"], a["cause"] + " " + a["cause_title"], a["basis"]] for a in A.get("actions", [])]
    S.append(dict(key="actions", title="七、可落地策略行动清单", text=f"共 {len(arows)} 条，按优先级和预期收益排序；每条绑定一个量化驱动因子。",
                  tables=[dict(name="行动清单", header=["优先级", "责任岗位", "行动", "完成周期", "预期收益", "对应原因", "测算依据"], rows=arows)]))
    return dict(title=meta.get("title") or f"{it['current']['label']}{dec['name']}异动归因报告", domain=it["domain"], created=datetime.now().strftime("%Y-%m-%d %H:%M"),
                author=meta.get("author", ""), summary=c["headline"], sections=S, annotations=meta.get("annotations", []),
                appendix=[dict(id=h["id"], title=h["title"], code=h["code"]) for h in A["hypotheses"]])


def _p(p):
    if p is None:
        return "—"
    return f"{p:.1e}" if p < 0.001 else f"{p:.3f}"


def _fmt_v(key, v, eco):
    if v is None:
        return "—"
    if eco:
        if key in ("cvr",):
            return f"{v:.2%}"
        if key in ("uv", "orders"):
            return f"{v:,.0f}"
        if key == "ipo":
            return f"{v:.3f}"
        if key in ("aov", "asp"):
            return f"{v:,.1f} 元"
        return _money(v)
    return _pct(v)


# ============================================================ Markdown

def to_markdown(R):
    out = [f"# {R['title']}", "", f"> 生成时间 {R['created']}" + (f"｜作者 {R['author']}" if R["author"] else ""), "", f"**摘要**：{R['summary']}", ""]
    notes = {}
    for a in R.get("annotations", []):
        notes.setdefault(a["section"], []).append(a)
    for s in R["sections"]:
        out += [f"## {s['title']}", "", s["text"], ""]
        for t in s.get("tables", []):
            if not t["rows"]:
                continue
            out += [f"**{t['name']}**", "", "| " + " | ".join(t["header"]) + " |", "|" + "---|" * len(t["header"])]
            out += ["| " + " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in r) + " |" for r in t["rows"]]
            out.append("")
        for a in notes.get(s["key"], []):
            out.append(f"> 批注（{a.get('author', '')}，{a.get('created', '')}）：{a['text']}")
        if notes.get(s["key"]):
            out.append("")
    out += ["## 附录：假设检验代码", ""]
    for x in R["appendix"]:
        out += [f"### {x['id']} {x['title']}", "", "```python", x["code"].strip(), "```", ""]
    return "\n".join(out)


# ============================================================ Excel

def to_excel(R):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "摘要"
    ws["A1"] = R["title"]
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"生成时间 {R['created']}"
    ws["A4"] = "摘要"
    ws["A4"].font = Font(bold=True)
    ws["A5"] = R["summary"]
    ws["A5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A5:H12")
    ws.column_dimensions["A"].width = 18
    hdr_fill = PatternFill("solid", fgColor="DCE6F1")
    names = {"background": "1 背景", "factors": "2 因子拆解", "drill": "3 维度下钻", "conclusion": "4 归因结论", "evidence": "5 证据链", "whatif": "6 What-If", "actions": "7 行动清单"}
    for s in R["sections"]:
        sh = wb.create_sheet(names.get(s["key"], s["key"])[:31])
        sh["A1"] = s["title"]
        sh["A1"].font = Font(bold=True, size=12)
        sh["A2"] = s["text"][:3000]
        sh["A2"].alignment = Alignment(wrap_text=True, vertical="top")
        sh.merge_cells("A2:H6")
        row = 8
        for t in s.get("tables", []):
            sh.cell(row=row, column=1, value=t["name"]).font = Font(bold=True)
            row += 1
            for j, h in enumerate(t["header"], 1):
                c = sh.cell(row=row, column=j, value=h)
                c.font = Font(bold=True)
                c.fill = hdr_fill
            hdr_row = row
            for r in t["rows"]:
                row += 1
                for j, v in enumerate(r, 1):
                    sh.cell(row=row, column=j, value=v).alignment = Alignment(wrap_text=True, vertical="top")
            row += 2
            if s["key"] == "factors" and t["name"] == "因子贡献":
                # 原生条形图：因子贡献（数值列另写一份）
                sh.cell(row=hdr_row, column=9, value="贡献（数值）").font = Font(bold=True)
                ch = s["chart"]
                for k, (n, v) in enumerate(ch["items"]):
                    sh.cell(row=hdr_row + 1 + k, column=8, value=n)
                    sh.cell(row=hdr_row + 1 + k, column=9, value=round(v / 1e4, 2) if ch["eco"] else round(v * 100, 3))
                bc = BarChart()
                bc.title = "因子贡献（" + ("万元" if ch["eco"] else "百分点") + "）"
                bc.add_data(Reference(sh, min_col=9, min_row=hdr_row, max_row=hdr_row + len(ch["items"])), titles_from_data=True)
                bc.set_categories(Reference(sh, min_col=8, min_row=hdr_row + 1, max_row=hdr_row + len(ch["items"])))
                bc.height, bc.width = 7, 14
                sh.add_chart(bc, f"K{hdr_row}")
        for j in range(1, 10):
            sh.column_dimensions[chr(64 + j)].width = 22 if j in (3, 7) else 16
    sh = wb.create_sheet("附录-检验代码")
    sh.append(["编号", "假设", "代码"])
    for x in R["appendix"]:
        sh.append([x["id"], x["title"], x["code"][:32000]])
    sh.column_dimensions["B"].width = 40
    sh.column_dimensions["C"].width = 120
    if R.get("annotations"):
        sh = wb.create_sheet("批注")
        sh.append(["章节", "作者", "时间", "内容"])
        for a in R["annotations"]:
            sh.append([a["section"], a.get("author", ""), a.get("created", ""), a["text"]])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ============================================================ PDF

def to_pdf(R):
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak, Preformatted

    F = _register_font(pdfmetrics, UnicodeCIDFont)
    st_t = ParagraphStyle("t", fontName=F, fontSize=17, leading=24, spaceAfter=6)
    st_h = ParagraphStyle("h", fontName=F, fontSize=13, leading=19, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1f3b63"))
    st_b = ParagraphStyle("b", fontName=F, fontSize=9.5, leading=14.5, wordWrap="CJK")
    st_c = ParagraphStyle("c", fontName=F, fontSize=7.8, leading=10.5, wordWrap="CJK")
    st_n = ParagraphStyle("n", fontName=F, fontSize=8.5, leading=12, textColor=colors.HexColor("#8a5a00"), backColor=colors.HexColor("#fff6e0"), wordWrap="CJK")
    st_code = ParagraphStyle("code", fontName="Courier", fontSize=6.5, leading=8)

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")

    W = A4[0] - 30 * mm
    story = [Paragraph(esc(R["title"]), st_t), Paragraph(esc(f"生成时间 {R['created']}" + (f"　作者 {R['author']}" if R["author"] else "")), st_c), Spacer(1, 6),
             Paragraph("<b>摘要</b>　" + esc(R["summary"]), st_b), Spacer(1, 6)]
    notes = {}
    for a in R.get("annotations", []):
        notes.setdefault(a["section"], []).append(a)
    for s in R["sections"]:
        story.append(Paragraph(esc(s["title"]), st_h))
        story.append(Paragraph(esc(s["text"]), st_b))
        if s.get("chart", {}).get("type") == "waterfall":
            story.append(_waterfall(s["chart"], W, Drawing, Rect, Line, String, colors, F))
        for t in s.get("tables", []):
            if not t["rows"]:
                continue
            story.append(Spacer(1, 4))
            story.append(Paragraph("<b>" + esc(t["name"]) + "</b>", st_b))
            data = [[Paragraph(esc(h), st_c) for h in t["header"]]] + [[Paragraph(esc(x), st_c) for x in r] for r in t["rows"]]
            n = len(t["header"])
            wide = {i for i, h in enumerate(t["header"]) if h in ("行动", "检验发现", "测算依据", "机制", "假设", "原因", "说明", "关联事件")}
            unit = W / (n + len(wide) * 1.5)
            cw = [unit * (2.5 if i in wide else 1) for i in range(n)]
            tb = Table(data, colWidths=cw, repeatRows=1)
            tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b9c4d3")),
                                    ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
            story.append(tb)
        for a in notes.get(s["key"], []):
            story.append(Spacer(1, 3))
            story.append(Paragraph(esc(f"批注（{a.get('author', '')}，{a.get('created', '')}）：{a['text']}"), st_n))
    story.append(PageBreak())
    story.append(Paragraph("附录：假设检验代码", st_h))
    for x in R["appendix"]:
        story.append(Paragraph(esc(f"{x['id']} {x['title']}"), st_b))
        code = "\n".join(l for l in x["code"].splitlines() if not l.lstrip().startswith("#"))  # 中文注释在等宽字体下无法显示，去掉
        story.append(Preformatted(code[:6000], st_code))
        story.append(Spacer(1, 4))
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm, title=R["title"])

    def footer(canvas, d):
        canvas.setFont(F, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"{R['title']}　第 {d.page} 页")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return bio.getvalue()


FONT_CANDIDATES = [  # 优先嵌入系统里的 TrueType 中文字体（任何阅读器都能显示）；都没有时用 PDF 内置的 STSong-Light（需阅读器自带中文字体）
    ("C:/Windows/Fonts/simhei.ttf", 0), ("C:/Windows/Fonts/msyh.ttc", 0), ("C:/Windows/Fonts/simsun.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0), ("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0), ("/Library/Fonts/Arial Unicode.ttf", 0),
]


def _register_font(pdfmetrics, UnicodeCIDFont):
    import os
    from reportlab.pdfbase.ttfonts import TTFont
    env = os.getenv("PDF_FONT")
    cands = ([(env, 0)] if env else []) + FONT_CANDIDATES
    for path, idx in cands:
        if path and os.path.exists(path):
            try:
                if "CJK" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=idx))
                return "CJK"
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _waterfall(ch, W, Drawing, Rect, Line, String, colors, F):
    eco = ch["eco"]
    conv = (lambda v: v / 1e4) if eco else (lambda v: v * 100)
    items = [("基期", conv(ch["start"]), "total")] + [(n, conv(v), "delta") for n, v in ch["items"]] + [("现期", conv(ch["end"]), "total")]
    H = 150
    d = Drawing(W, H + 30)
    lo_hi = []
    run = 0
    for n, v, k in items:
        if k == "total":
            lo_hi.append((0, v))
            run = v
        else:
            lo_hi.append((run, run + v))
            run += v
    vmin = min(min(a, b) for a, b in lo_hi)
    vmax = max(max(a, b) for a, b in lo_hi)
    if not eco:  # 比率指标从接近的值起画，避免柱子看不出差异
        span = vmax - vmin
        vmin = max(0, vmin - span * 0.6)
    vmax += (vmax - vmin) * 0.08
    sc = lambda v: 20 + (v - vmin) / (vmax - vmin) * H if vmax > vmin else 20
    bw = W / len(items)
    for i, ((n, v, k), (a, b)) in enumerate(zip(items, lo_hi)):
        x = i * bw + bw * 0.18
        y0, y1 = sorted([sc(a), sc(b)])
        col = colors.HexColor("#5b7fb5") if k == "total" else (colors.HexColor("#d2564e") if v < 0 else colors.HexColor("#4f9d69"))
        d.add(Rect(x, y0, bw * 0.64, max(y1 - y0, 0.8), fillColor=col, strokeColor=None))
        lab = (f"{v:,.1f}" if k == "total" else f"{v:+,.1f}") + ("" if eco else "")
        d.add(String(x + bw * 0.32, y1 + 3, lab, fontName=F, fontSize=7, textAnchor="middle"))
        d.add(String(x + bw * 0.32, 6, n[:8], fontName=F, fontSize=7, textAnchor="middle"))
    d.add(String(0, H + 22, "因子贡献瀑布图（" + ("万元" if eco else "%，比率指标") + "）", fontName=F, fontSize=8))
    return d
