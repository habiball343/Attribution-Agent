"""假设检验代码模板。

每条候选假设都对应一段完整、可读、可单独运行的 Python 代码，在沙箱里执行（见 sandbox.py）。
代码里只用注入的 q(sql, params) 读数，最后给 result 赋值：
  effect      对目标指标变动的贡献（电商为元；快消为净利率的比例值，0.01 = 1 个百分点）
  confidence  置信度 0 到 1 = 统计显著性（1 − p）× 重要性（|effect| 相对总变动 2% 的比例，封顶 1）
  verdict     支持 / 排除 / 派生（派生现象：真实存在但由其他原因引起）
  finding     一句话结论；evidence 为证据列表；series 为可选的图表数据
模板只决定“怎么检验”，参数（渠道、日期窗口、商品）全部来自异动扫描和事件日历。
"""

PRELUDE = '''
TOTAL = __TOTAL__          # 目标指标的总变动
def conf(p, effect):
    sig = max(0.0, min(1.0, 1 - float(p)))
    mat = min(1.0, abs(effect) / (0.02 * abs(TOTAL))) if TOTAL else 1.0
    return round(sig * mat, 3)
'''


def fill(tpl, **kw):
    """__X__ 换成 Python 字面量（代码里用的参数），«X» 原样换成文字（注释和字符串里用的名称）。"""
    s = PRELUDE + tpl
    for k, v in kw.items():
        s = s.replace(f"__{k.upper()}__", repr(v))
        s = s.replace(f"«{k.upper()}»", "、".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v))
    return s.strip() + "\n"


# ============================================================ 电商

TRAFFIC_DROP = '''
# 假设：«CNAME» 访客数自 «TAU» 起骤降，拉低 GMV
# 方法：突变点前 14 天（剔除大促日）与突变点后对比访客数（Welch t 检验）；转化率和客单价不变时，损失 = 少来的访客 × 突变后转化率 × 客单价
CH, TAU, END, EXCLUDE = __CH__, __TAU__, __END__, __EXCLUDE__
PRE_START = str((pd.Timestamp(TAU) - pd.Timedelta(days=14)).date())
t = q("SELECT dt, uv FROM fact_traffic_daily WHERE channel_id = ? AND dt BETWEEN ? AND ?", (CH, PRE_START, END))
o = q("SELECT dt, COUNT(*) AS orders, SUM(pay_amount) AS gmv FROM fact_orders WHERE channel_id = ? AND dt BETWEEN ? AND ? GROUP BY dt", (CH, PRE_START, END))
df = t.merge(o, on="dt", how="left").fillna(0)
pre = df[(df.dt < TAU) & (~df.dt.isin(EXCLUDE))]
post = df[df.dt >= TAU]
tt, p = stats.ttest_ind(post.uv, pre.uv, equal_var=False)
cvr_pre, cvr_post = pre.orders.sum() / pre.uv.sum(), post.orders.sum() / post.uv.sum()
aov_post = post.gmv.sum() / post.orders.sum()
lost_uv = (pre.uv.mean() - post.uv.mean()) * len(post)
effect = -lost_uv * cvr_post * aov_post
cvr_p = stats.ttest_ind(post.orders / post.uv, pre.orders / pre.uv, equal_var=False).pvalue
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"«CNAME»日均访客由 {pre.uv.mean():,.0f} 降到 {post.uv.mean():,.0f}（{post.uv.mean() / pre.uv.mean() - 1:+.0%}），"
            f"转化率 {cvr_pre:.2%} → {cvr_post:.2%} 基本不变（p = {cvr_p:.2f}），{len(post)} 天少来 {lost_uv:,.0f} 名访客，估算拉低 GMV {effect / 1e4:,.1f} 万元",
    evidence=[f"访客数 t 检验 t = {tt:.1f}，p = {p:.1e}", f"突变后转化率 {cvr_post:.2%}、客单价 {aov_post:,.0f} 元"],
    series=dict(x=list(df.dt), y=list(df.uv), mark=TAU, name="«CNAME»日访客数"))
'''

STOCKOUT = '''
# 假设：«CATNAME»类目 «N» 个商品断货（«S» 至 «E»），拉低 GMV 和客单价
# 方法：断货前 14 天为基线，按全站访客变化做流量校正；毛损失 = 断货商品基线日销 × 断货天数；
#       替代 = 同类目其他商品断货期日销高于基线的部分；净影响 = −(毛损失 − 替代)
SKUS, CAT, S, E, EXCLUDE = __SKUS__, __CAT__, __S__, __E__, __EXCLUDE__
PRE_S = str((pd.Timestamp(S) - pd.Timedelta(days=14)).date())
it = q("""SELECT o.dt, i.product_id, i.pay_amount FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
          JOIN dim_product p ON p.product_id = i.product_id WHERE p.category_id = ? AND o.dt BETWEEN ? AND ?""", (CAT, PRE_S, E))
uv = q("SELECT dt, SUM(uv) AS uv FROM fact_traffic_daily WHERE dt BETWEEN ? AND ? GROUP BY dt", (PRE_S, E)).set_index("dt").uv
it["oos"] = it.product_id.isin(SKUS)
d = it.groupby(["dt", "oos"]).pay_amount.sum().unstack(fill_value=0).reindex(columns=[False, True], fill_value=0)
pre_days = [x for x in d.index if x < S and x not in EXCLUDE]
in_days = [x for x in d.index if S <= x <= E]
adj = uv[in_days].mean() / uv[pre_days].mean()
base_oos, base_other = d.loc[pre_days, True].mean(), d.loc[pre_days, False].mean()
gross_loss = base_oos * adj * len(in_days)
substitution = (d.loc[in_days, False].mean() - base_other * adj) * len(in_days)
effect = -(gross_loss - max(substitution, 0))
cat_daily = d.sum(axis=1)
tt, p = stats.ttest_ind(cat_daily[in_days] / uv[in_days], cat_daily[pre_days] / uv[pre_days], equal_var=False)
oo = q("""SELECT o.dt, o.order_id, SUM(i.pay_amount) AS g FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
          JOIN dim_product p ON p.product_id = i.product_id WHERE p.category_id = ? AND o.dt BETWEEN ? AND ? GROUP BY o.dt, o.order_id""", (CAT, PRE_S, E))
aov_pre, aov_in = oo[oo.dt.isin(pre_days)].g.mean(), oo[oo.dt.isin(in_days)].g.mean()
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"断货 {len(in_days)} 天：断货商品基线日销 {base_oos / 1e4:.1f} 万元，毛损失约 {gross_loss / 1e4:,.1f} 万元，"
            f"其中约 {max(substitution, 0) / 1e4:,.1f} 万元转向同类目其他商品，净拉低 GMV {effect / 1e4:,.1f} 万元；类目客单价 {aov_pre:,.0f} → {aov_in:,.0f} 元",
    evidence=[f"类目每访客 GMV 断货期 vs 断货前 t = {tt:.1f}，p = {p:.1e}", f"断货期全站访客为断货前的 {adj:.0%}（已校正）"],
    series=dict(x=list(d.index), y=list(d[True]), y2=list(d[False]), mark=S, name="断货商品日销 / 同类目其他商品日销"))
'''

BASE_SPIKE = '''
# 假设：基期 «S» 至 «E» 的“«TITLE»”抬高了基期，使环比降幅看起来更大
# 方法：基期内其余日子的中位数作为常态日销，活动日高出常态的部分即基期被抬高的金额
S, E, P_START, P_END = __S__, __E__, __P_START__, __P_END__
d = q("SELECT dt, SUM(pay_amount) AS gmv, COUNT(*) AS orders FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY dt", (P_START, P_END))
spike, normal = d[d.dt.between(S, E)], d[~d.dt.between(S, E)]
baseline = normal.gmv.median()
excess = (spike.gmv - baseline).sum()
effect = -excess
tt, p = stats.ttest_ind(spike.gmv, normal.gmv, equal_var=False)
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"活动 {len(spike)} 天日均 GMV {spike.gmv.mean() / 1e4:,.1f} 万元，是常态日（中位数 {baseline / 1e4:,.1f} 万元）的 {spike.gmv.mean() / baseline:.2f} 倍，"
            f"基期因此多出 {excess / 1e4:,.1f} 万元，对环比变动的贡献为 {effect / 1e4:,.1f} 万元",
    evidence=[f"活动日 vs 常态日 GMV t = {tt:.1f}，p = {p:.1e}"],
    series=dict(x=list(d.dt), y=list(d.gmv), mark=S, name="基期日 GMV"))
'''

PROMO_CATEGORY = '''
# 假设：类目活动“«TITLE»”（«CATNAME»，«S» 至 «E»）给所在周期带来额外 GMV，影响环比
# 方法：«CATNAME»在两个周期内非活动日（剔除全站大促日）的日 GMV 中位数作为常态，活动日高出常态的部分为活动增量；
#      对环比的贡献 = 现期内的增量 − 基期内的增量（活动跨两个周期时两边都算）
CAT, S, E, P0S, P0E, P1S, P1E, EXCLUDE = __CAT__, __S__, __E__, __P0S__, __P0E__, __P1S__, __P1E__, __EXCLUDE__
d = q("""SELECT o.dt, SUM(i.pay_amount) AS gmv FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
         JOIN dim_product p ON p.product_id = i.product_id WHERE p.category_id = ? AND o.dt BETWEEN ? AND ? GROUP BY o.dt""", (CAT, P0S, P1E))
ev = d.dt.between(S, E)
normal = d[~ev & ~d.dt.isin(EXCLUDE)]
baseline = normal.gmv.median()
lift = d[ev].assign(x=lambda t: t.gmv - baseline)
in0, in1 = lift[lift.dt <= P0E].x.sum(), lift[lift.dt >= P1S].x.sum()
effect = in1 - in0
tt, p = stats.ttest_ind(d[ev].gmv, normal.gmv, equal_var=False)
c = conf(p, effect)
result = dict(
    effect=effect, confidence=c, verdict="支持" if c >= 0.6 else "排除", p_value=p,
    finding=f"活动期间«CATNAME»日均 GMV {d[ev].gmv.mean() / 1e4:,.1f} 万元，常态 {baseline / 1e4:,.1f} 万元；"
            f"活动增量落在基期 {in0 / 1e4:,.1f} 万元、现期 {in1 / 1e4:,.1f} 万元，对环比的净贡献 {effect / 1e4:+,.1f} 万元",
    evidence=[f"活动日 vs 常态日 t = {tt:.1f}，p = {p:.1e}"],
    series=dict(x=list(d.dt), y=list(d.gmv), mark=S, name="«CATNAME»日 GMV"))
'''

HOURLY_DROP = '''
# 假设：«DATE» «HOURS» 点订单骤降（系统或支付故障），损失当时段成交
# 方法：同一小时在现期其他日子的订单中位数为期望值，差额 × 该时段客单价 = 损失
DATE, HOURS, P_START, P_END = __DATE__, __HOURS__, __P_START__, __P_END__
h = q("""SELECT dt, CAST(substr(pay_time, 12, 2) AS INTEGER) AS hr, COUNT(*) AS n, SUM(pay_amount) AS g FROM fact_orders
         WHERE dt BETWEEN ? AND ? GROUP BY dt, hr""", (P_START, P_END))
others = h[(h.dt != DATE) & (h.hr.isin(HOURS))]
exp = others.groupby("hr").n.median()
act = h[(h.dt == DATE) & (h.hr.isin(HOURS))].set_index("hr").n.reindex(HOURS, fill_value=0)
aov = others.g.sum() / others.n.sum()
lost = float((exp - act).clip(lower=0).sum())
effect = -lost * aov
z = ((act - exp) / others.groupby("hr").n.std()).min()
p = 2 * stats.norm.sf(abs(z))
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"{DATE} {'、'.join(str(x) for x in HOURS)} 点实际订单 {int(act.sum())} 单，同时段常态约 {exp.sum():.0f} 单，少 {lost:.0f} 单，估算损失 GMV {-effect / 1e4:,.1f} 万元",
    evidence=[f"最低 z 值 {z:.1f}（以其他日子同时段为参照），p = {p:.1e}"],
    series=dict(x=list(range(24)), y=list(h[h.dt == DATE].set_index("hr").n.reindex(range(24), fill_value=0)),
                y2=list(h[h.dt != DATE].groupby("hr").n.median().reindex(range(24), fill_value=0)), name=f"{DATE} 每小时订单 / 常态中位数"))
'''

RELEASE_EFFECT = '''
# 假设：«DATE»“«TITLE»”改变了转化率，影响 GMV
# 方法：发布前后各取 7 个“干净”日子（剔除营销活动日和大促尖峰日，避免把促销带来的转化波动算到发版头上），
#       比较各渠道（剔除同期有流量突变的渠道：«EXCL_NAMES»）合计的日转化率（Welch t 检验）
DATE, EXCL, PROMO_DAYS = __DATE__, __EXCL__, __PROMO_DAYS__
S, E = str((pd.Timestamp(DATE) - pd.Timedelta(days=40)).date()), str((pd.Timestamp(DATE) + pd.Timedelta(days=20)).date())
t = q("SELECT dt, channel_id, uv FROM fact_traffic_daily WHERE dt BETWEEN ? AND ?", (S, E))
o = q("SELECT dt, channel_id, COUNT(*) AS orders, SUM(pay_amount) AS gmv FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY dt, channel_id", (S, E))
df = t.merge(o, on=["dt", "channel_id"], how="left").fillna(0)
df = df[~df.channel_id.isin(EXCL)]
g = df.groupby("dt")[["uv", "orders", "gmv"]].sum()
g["cvr"] = g.orders / g.uv
clean = g[~g.index.isin(PROMO_DAYS)]
pre, post = clean[clean.index < DATE].tail(7), clean[clean.index >= DATE].head(7)
tt, p = stats.ttest_ind(post.cvr, pre.cvr, equal_var=False)
d_cvr = post.cvr.mean() / pre.cvr.mean() - 1
effect = d_cvr * post.gmv.sum() if p < 0.05 else 0.0
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=(f"剔除促销日后，发布前 7 天（{pre.index[0]} 至 {pre.index[-1]}）转化率 {pre.cvr.mean():.2%}，发布后 7 天（{post.index[0]} 至 {post.index[-1]}）"
             f"{post.cvr.mean():.2%}（{d_cvr:+.1%}，p = {p:.2f}）") + ("，差异不显著，与 GMV 下滑无关" if p >= 0.05 else f"，影响约 {effect / 1e4:,.1f} 万元"),
    evidence=[f"日转化率 Welch t 检验 t = {tt:.2f}，p = {p:.2f}", f"剔除的促销日 {len([x for x in g.index if x in PROMO_DAYS])} 天"],
    series=dict(x=list(g.index), y=list(g.cvr), mark=DATE, name="日转化率（剔除流量突变渠道）"))
'''

COMPETITOR = '''
# 假设：“«TITLE»”（«S» 至 «E»）分流了«CATNAME»类目的销售
# 方法：看«CATNAME»占全站 GMV 的比例在活动期是否低于活动前 14 天（只看绝对值会被全站下滑误导）
CAT, S, E = __CAT__, __S__, __E__
PRE = str((pd.Timestamp(S) - pd.Timedelta(days=14)).date())
d = q("""SELECT o.dt, SUM(CASE WHEN p.category_id = ? THEN i.pay_amount ELSE 0 END) AS cat, SUM(i.pay_amount) AS total
         FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_id = i.product_id
         WHERE o.dt BETWEEN ? AND ? GROUP BY o.dt""", (CAT, PRE, E))
d["share"] = d.cat / d.total
pre, during = d[d.dt < S], d[d.dt >= S]
tt, p = stats.ttest_ind(during.share, pre.share, equal_var=False)
d_share = during.share.mean() - pre.share.mean()
effect = d_share * during.total.sum() if (p < 0.05 and d_share < 0) else 0.0
abs_change = during.cat.mean() / pre.cat.mean() - 1
result = dict(
    effect=effect, confidence=conf(p, effect) if d_share < 0 else 0.0, verdict="支持" if (d_share < 0 and conf(p, effect) >= 0.6) else "排除", p_value=p,
    finding=f"«CATNAME»日均 GMV 变化 {abs_change:+.1%}，但占全站比例 {pre.share.mean():.2%} → {during.share.mean():.2%}（p = {p:.2f}），"
            + ("占比没有下降，下滑与全站同步，竞品活动不是原因" if (p >= 0.05 or d_share >= 0) else f"占比显著下降，估算影响 {effect / 1e4:,.1f} 万元"),
    evidence=[f"类目占比 t 检验 t = {tt:.2f}，p = {p:.2f}"],
    series=dict(x=list(d.dt), y=list(d.share), mark=S, name="«CATNAME»占全站 GMV 比例"))
'''

REGION_DERIVED = '''
# 检验：«REGION»跌幅最大，是独立原因，还是«CNAME»限流的派生现象？
# 方法：剔除«CNAME»后再比较各区域的 GMV 变化率；若«REGION»不再突出，则为派生现象
P0S, P0E, P1S, P1E, CH = __P0S__, __P0E__, __P1S__, __P1E__, __CH__
d = q("""SELECT r.region_name AS region, o.channel_id, SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN o.pay_amount ELSE 0 END) AS g0,
         SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN o.pay_amount ELSE 0 END) AS g1
         FROM fact_orders o JOIN dim_region r ON r.region_id = o.region_id GROUP BY region, o.channel_id""", (P0S, P0E, P1S, P1E))
allr = d.groupby("region")[["g0", "g1"]].sum()
ex = d[d.channel_id != CH].groupby("region")[["g0", "g1"]].sum()
allr["rate"] = allr.g1 / allr.g0 - 1
ex["rate"] = ex.g1 / ex.g0 - 1
ch_share = (d[d.channel_id == CH].groupby("region").g0.sum() / allr.g0)
gap_all = allr.rate["«REGION»"] - allr.rate.drop("«REGION»").mean()
gap_ex = ex.rate["«REGION»"] - ex.rate.drop("«REGION»").mean()
derived = abs(gap_ex) < 0.03 and abs(gap_ex) < abs(gap_all) / 2
result = dict(
    effect=0.0, confidence=0.9 if derived else 0.3, verdict="派生" if derived else "支持",
    finding=f"«REGION» GMV 变化 {allr.rate['«REGION»']:+.1%}（其他区域平均 {allr.rate.drop('«REGION»').mean():+.1%}）；"
            f"剔除«CNAME»后为 {ex.rate['«REGION»']:+.1%}（其他区域 {ex.rate.drop('«REGION»').mean():+.1%}）。"
            f"«CNAME»在«REGION»基期 GMV 中占 {ch_share['«REGION»']:.0%}（全国平均 {ch_share.mean():.0%}）。"
            + ("跌幅最大是渠道限流的派生现象，不是独立原因" if derived else "剔除后仍明显偏弱，需要单独排查"),
    evidence=[f"差距：全部渠道 {gap_all:+.1%}，剔除后 {gap_ex:+.1%}"],
    table=[dict(区域=k, 全部渠道变化率=round(allr.rate[k], 4), 剔除后变化率=round(ex.rate[k], 4), 该渠道占比=round(ch_share[k], 4)) for k in allr.index])
'''

NEWUSER_DERIVED = '''
# 检验：新客占比下降，是独立原因（拉新变差），还是«CNAME»限流的派生现象？
# 方法：比较全部渠道与剔除«CNAME»后的新客占比变化
P0S, P0E, P1S, P1E, CH = __P0S__, __P0E__, __P1S__, __P1E__, __CH__
d = q("""SELECT channel_id, CASE WHEN dt BETWEEN ? AND ? THEN 0 ELSE 1 END AS period, COUNT(*) AS n, SUM(is_new_user) AS nu
         FROM fact_orders WHERE dt BETWEEN ? AND ? OR dt BETWEEN ? AND ? GROUP BY channel_id, period""", (P0S, P0E, P0S, P0E, P1S, P1E))
a = d.groupby("period")[["n", "nu"]].sum(); a["s"] = a.nu / a.n
e = d[d.channel_id != CH].groupby("period")[["n", "nu"]].sum(); e["s"] = e.nu / e.n
c = d[d.channel_id == CH].groupby("period")[["n", "nu"]].sum(); c["s"] = c.nu / c.n
derived = abs(e.s[1] - e.s[0]) < 0.01 and (a.s[1] - a.s[0]) < -0.005
result = dict(
    effect=0.0, confidence=0.9 if derived else 0.3, verdict="派生" if derived else "支持",
    finding=f"新客占比 {a.s[0]:.1%} → {a.s[1]:.1%}；剔除«CNAME»后 {e.s[0]:.1%} → {e.s[1]:.1%}；«CNAME»本身新客占比 {c.s[0]:.1%}，"
            + ("下降来自高新客占比渠道的流量减少，属于派生现象" if derived else "剔除后仍下降，拉新本身可能有问题"),
    evidence=[f"剔除后变化 {e.s[1] - e.s[0]:+.2%}"])
'''

CAT_AOV_DERIVED = '''
# 检验：«CATNAME»客单价下降，是独立原因（降价、促销），还是断货的派生现象？
# 方法：只看未断货商品，比较断货前后同类目订单行的件单价
SKUS, CAT, S, E = __SKUS__, __CAT__, __S__, __E__
PRE = str((pd.Timestamp(S) - pd.Timedelta(days=14)).date())
d = q("""SELECT o.dt, i.product_id, i.pay_amount, i.quantity FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
         JOIN dim_product p ON p.product_id = i.product_id WHERE p.category_id = ? AND o.dt BETWEEN ? AND ?""", (CAT, PRE, E))
d["in"] = d.dt >= S
d["oos"] = d.product_id.isin(SKUS)
asp = d.groupby("in").apply(lambda x: x.pay_amount.sum() / x.quantity.sum(), include_groups=False)
asp_other = d[~d.oos].groupby("in").apply(lambda x: x.pay_amount.sum() / x.quantity.sum(), include_groups=False)
share_oos = d[~d["in"]].groupby("oos").pay_amount.sum()[True] / d[~d["in"]].pay_amount.sum()
derived = abs(asp_other[True] / asp_other[False] - 1) < 0.05 and asp[True] / asp[False] - 1 < -0.03
result = dict(
    effect=0.0, confidence=0.9 if derived else 0.3, verdict="派生" if derived else "支持",
    finding=f"类目件单价 {asp[False]:,.0f} → {asp[True]:,.0f} 元（{asp[True] / asp[False] - 1:+.1%}）；未断货商品 {asp_other[False]:,.0f} → {asp_other[True]:,.0f} 元"
            f"（{asp_other[True] / asp_other[False] - 1:+.1%}）。断货商品断货前占类目 GMV {share_oos:.0%}，"
            + ("客单价下降是高价商品断货、顾客改买便宜款造成的，属于断货的派生现象" if derived else "未断货商品价格也在下降，需要单独排查"),
    evidence=[])
'''

# ============================================================ 快消

FMCG_HELPERS = '''
METRIC = __METRIC__
NETM = METRIC == "net_margin"      # 净利率口径扣物流、佣金、营销；毛利率口径只扣产品成本
def by_channel(s, e):
    m0, m1 = s[:7], e[:7]
    a = q("""SELECT s.channel_id AS k, SUM(s.net_revenue) AS rev, SUM(s.gross_amount) AS gross, SUM(s.discount_amount) AS disc,
             SUM(s.sales_qty * c.unit_cogs) AS cogs, SUM(s.sales_qty) AS qty FROM fact_sales_daily s
             JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7) WHERE s.dt BETWEEN ? AND ? GROUP BY k""", (s, e)).set_index("k")
    lg = q("SELECT channel_id AS k, SUM(logistics_cost) AS logi, SUM(shipped_qty) AS shipped FROM fact_logistics_daily WHERE dt BETWEEN ? AND ? GROUP BY k", (s, e)).set_index("k")
    cm = q("SELECT channel_id AS k, SUM(commission_amount) AS comm FROM fact_commission_monthly WHERE month BETWEEN ? AND ? GROUP BY k", (m0, m1)).set_index("k")
    mk = q("SELECT channel_id AS k, SUM(amount) AS mkt FROM fact_marketing_monthly WHERE month BETWEEN ? AND ? GROUP BY k", (m0, m1)).set_index("k")
    df = a.join(lg, how="outer").join(cm, how="outer").join(mk, how="outer").fillna(0)
    df["net"] = df.rev - df.cogs - ((df.logi + df.comm + df.mkt) if NETM else 0)   # 目标指标口径下的利润
    df["margin"] = df.net / df.rev
    return df
P0S, P0E, P1S, P1E = __P0S__, __P0E__, __P1S__, __P1E__
A, B = by_channel(P0S, P0E), by_channel(P1S, P1E)
M0, M1 = A.net.sum() / A.rev.sum(), B.net.sum() / B.rev.sum()
'''

CHANNEL_MIX = '''
# 假设：«CNAME»营收占比上升（渠道结构变化），而该渠道利润率远低于平均，拉低整体«METRICNAME»
# 方法：结构 × 水平两因子 Shapley 分解，取该渠道的结构效应 Δ占比 × (该渠道平均«METRICNAME» − 整体平均«METRICNAME»)
''' + FMCG_HELPERS + '''
CH = __CH__
s0, s1 = A.rev[CH] / A.rev.sum(), B.rev[CH] / B.rev.sum()
m0, m1 = A.margin[CH], B.margin[CH]
ybar = (M0 + M1) / 2
effect = (s1 - s0) * ((m0 + m1) / 2 - ybar)
rate_effect = (m1 - m0) * (s0 + s1) / 2
monthly = q("""SELECT substr(dt, 1, 7) AS month, SUM(CASE WHEN channel_id = ? THEN net_revenue ELSE 0 END) * 1.0 / SUM(net_revenue) AS share
               FROM fact_sales_daily GROUP BY month ORDER BY month""", (CH,))
hist = monthly[monthly.month < P1S[:7]].share
z = (s1 - hist.mean()) / (hist.std() or 1e-9)
p = 2 * stats.norm.sf(abs(z))
gm = 1 - B.cogs[CH] / B.rev[CH]
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"«CNAME»营收占比 {s0:.1%} → {s1:.1%}，该渠道«METRICNAME» {m1:.1%}（毛利率 {gm:.1%}），远低于整体 {M1:.1%}；"
            f"结构效应拉低«METRICNAME» {effect * 100:.2f} 个百分点，渠道自身利润率变化另影响 {rate_effect * 100:+.2f} 个百分点",
    evidence=[f"现期占比相对历史月份的 z = {z:.1f}，p = {p:.1e}",
              f"该渠道费用率：折扣 {B.disc[CH] / B.gross[CH]:.0%}、物流 {B.logi[CH] / B.rev[CH]:.1%}、佣金 {B.comm[CH] / B.rev[CH]:.1%}"],
    series=dict(x=list(monthly.month), y=list(monthly.share), name="«CNAME»营收占比（月）"))
'''

DISCOUNT_LINE = '''
# 假设：«LINE»在现期加大让利（折扣率上升），拉低«METRICNAME»
# 方法：分渠道比较该产品线现期折扣率与常态月份 «REF»（避开大促月）的折扣率，只算同一渠道内多出来的让利 X，
#      这样社区团购等高折扣渠道占比上升带来的“结构性”折扣不会被重复计入；反事实«METRICNAME» = (利润 + X) / (营收 + X)
''' + FMCG_HELPERS + '''
LINE, REF = "«LINE»", __REF__
def line_by_ch(where, args):
    return q("""SELECT s.channel_id AS k, SUM(s.gross_amount) AS gross, SUM(s.discount_amount) AS disc FROM fact_sales_daily s
                JOIN dim_sku d ON d.sku_id = s.sku_id WHERE d.product_line = ? AND """ + where + " GROUP BY k", (LINE,) + args).set_index("k")
ref = line_by_ch("substr(s.dt, 1, 7) IN (%s)" % ",".join("'%s'" % m for m in REF), ())
cur = line_by_ch("s.dt BETWEEN ? AND ?", (P1S, P1E))
r_ref, r_cur = ref.disc / ref.gross, cur.disc / cur.gross
extra = ((r_cur - r_ref).clip(lower=0) * cur.gross).fillna(0)
X = extra.sum()
effect = M1 - (B.net.sum() + X) / (B.rev.sum() + X)
daily = q("""SELECT s.dt, SUM(s.discount_amount) / SUM(s.gross_amount) AS r FROM fact_sales_daily s JOIN dim_sku d ON d.sku_id = s.sku_id
             WHERE d.product_line = ? AND (s.dt BETWEEN ? AND ? OR substr(s.dt, 1, 7) IN (%s)) GROUP BY s.dt ORDER BY s.dt""" % ",".join("'%s'" % m for m in REF),
          (LINE, P1S, P1E))
tt, p = stats.ttest_ind(daily[daily.dt >= P1S].r, daily[daily.dt < P1S].r, equal_var=False)
top = extra.sort_values(ascending=False)
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"«LINE»现期折扣率 {cur.disc.sum() / cur.gross.sum():.1%}，常态月份 {ref.disc.sum() / ref.gross.sum():.1%}；按渠道剔除结构因素后多让利 {X / 1e4:,.1f} 万元，"
            f"拉低«METRICNAME» {effect * 100:.2f} 个百分点",
    evidence=[f"日折扣率 现期 vs 常态月份 t = {tt:.1f}，p = {p:.1e}"] +
             [f"{k}：{r_ref.get(k, 0):.1%} → {r_cur[k]:.1%}，多让利 {v / 1e4:,.1f} 万元" for k, v in top.items() if v > 0][:4],
    series=dict(x=list(daily.dt), y=list(daily.r), name="«LINE»日折扣率"))
'''

BASE_DISCOUNT = '''
# 假设：基期 «MONTH»（“«TITLE»”）让利偏高，压低了基期«METRICNAME»，使环比降幅被低估（反向因素）
# 方法：用相邻月份 «NEIGHBORS» 的分渠道折扣率作为常态，反算基期常态«METRICNAME»；影响 = 常态«METRICNAME» − 实际«METRICNAME»
''' + FMCG_HELPERS + '''
NB = __NEIGHBORS__
nb = q("""SELECT channel_id AS k, SUM(discount_amount) / SUM(gross_amount) AS r FROM fact_sales_daily WHERE substr(dt, 1, 7) IN (%s) GROUP BY k""" % ",".join("'%s'" % m for m in NB)).set_index("k").r
X = ((A.disc / A.gross - nb).clip(lower=0) * A.gross).sum()
cf = (A.net.sum() + X) / (A.rev.sum() + X)
effect = cf - M0
m = q("SELECT substr(dt, 1, 7) AS month, SUM(discount_amount) / SUM(gross_amount) AS r FROM fact_sales_daily GROUP BY month ORDER BY month")
others = m[m.month.isin(NB)].r
z = (m[m.month == P0S[:7]].r.iloc[0] - others.mean()) / (others.std() or 1e-9)
p = 2 * stats.norm.sf(abs(z))
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"基期折扣率 {A.disc.sum() / A.gross.sum():.1%}，常态月份 {others.mean():.1%}；超常让利 {X / 1e4:,.1f} 万元，"
            f"基期常态«METRICNAME»应为 {cf:.2%}（实际 {M0:.2%}），使环比降幅少算 {effect * 100:.2f} 个百分点",
    evidence=[f"基期折扣率相对相邻月份 z = {z:.1f}，p = {p:.1e}"],
    series=dict(x=list(m.month), y=list(m.r), name="月折扣率"))
'''

MATERIAL_PRICE = '''
# 假设：«MNAME»采购价上涨，抬高产品成本，拉低«METRICNAME»
# 方法：现期销量 × BOM 用量 × 单价涨幅 = 多花的成本；反事实«METRICNAME»差即影响
''' + FMCG_HELPERS + '''
MID = __MID__
pr = q("SELECT month, unit_price FROM fact_material_price_monthly WHERE material_id = ? ORDER BY month", (MID,))
p0, p1 = pr[pr.month == P0S[:7]].unit_price.iloc[0], pr[pr.month == P1S[:7]].unit_price.iloc[0]
use = q("""SELECT SUM(s.sales_qty * b.qty_per_unit) AS used FROM fact_sales_daily s JOIN bom b ON b.sku_id = s.sku_id
           WHERE b.material_id = ? AND s.dt BETWEEN ? AND ?""", (MID, P1S, P1E)).used.iloc[0]
extra = use * (p1 - p0)
effect = M1 - (B.net.sum() + extra) / B.rev.sum()
hist = pr[pr.month < P1S[:7]].unit_price
z = (p1 - hist.mean()) / (hist.std() or 1e-9)
p = 2 * stats.norm.sf(min(abs(z), 30))
result = dict(
    effect=effect, confidence=conf(p, effect), verdict="支持" if conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"«MNAME»单价 {p0:.2f} → {p1:.2f} 元（{p1 / p0 - 1:+.0%}），现期用量 {use:,.0f}，多花成本 {extra / 1e4:,.1f} 万元，拉低«METRICNAME» {effect * 100:.2f} 个百分点",
    evidence=[f"单价相对历史月份 z = {z:.1f}"],
    series=dict(x=list(pr.month), y=list(pr.unit_price), name="«MNAME»月度单价"))
'''

COMMISSION = '''
# 假设：«CNAME»佣金率自 «START» 由 «R0» 调到 «R1»，拉低«METRICNAME»
# 方法：费率差 × 该渠道现期营收 = 多付的佣金；反事实«METRICNAME»差即影响
''' + FMCG_HELPERS + '''
CH, R0, R1 = __CH__, __R0__, __R1__
extra = (R1 - R0) * B.rev[CH]
effect = M1 - (B.net.sum() + extra) / B.rev.sum() if NETM else 0.0
actual = B.comm[CH] / B.rev[CH]
p = 0.0 if abs(actual - R1) < 0.005 else 0.5
result = dict(
    effect=effect, confidence=conf(p, effect) if NETM else 0.0, verdict="支持" if NETM and conf(p, effect) >= 0.6 else "排除", p_value=p,
    finding=f"«CNAME»现期营收 {B.rev[CH] / 1e4:,.1f} 万元，实际佣金率 {actual:.1%}，较调整前多付 {extra / 1e4:,.1f} 万元，" +
            (f"拉低«METRICNAME» {effect * 100:.2f} 个百分点" if NETM else "佣金计入净利率、不影响毛利率，对本指标无影响"),
    evidence=[f"佣金表 fact_commission_monthly 的实际费率与费率表一致（{actual:.1%}）"])
'''

LOGISTICS_VENDOR = '''
# 假设：“«TITLE»”（«DATE»）抬高了物流成本，拉低«METRICNAME»
# 方法：不看物流费用总额（会随销量上升），看物流费用率和分渠道每件物流成本在事件前后是否上升
''' + FMCG_HELPERS + '''
DATE = __DATE__
d = q("""SELECT channel_id, CASE WHEN dt < ? THEN 0 ELSE 1 END AS after, SUM(logistics_cost) AS cost, SUM(shipped_qty) AS qty
         FROM fact_logistics_daily WHERE dt BETWEEN ? AND ? GROUP BY channel_id, after""", (DATE, P0S, P1E))
d["unit"] = d.cost / d.qty
pv = d.pivot(index="channel_id", columns="after", values="unit")
chg = (pv[1] / pv[0] - 1)
lr0, lr1 = A.logi.sum() / A.rev.sum(), B.logi.sum() / B.rev.sum()
u0, u1 = A.logi.sum() / A.shipped.sum(), B.logi.sum() / B.shipped.sum()
tt, p = stats.ttest_1samp(chg, 0)
rising = NETM and lr1 > lr0 and chg.mean() > 0.03 and p < 0.05
effect = -(lr1 - lr0) if rising else 0.0
result = dict(
    effect=effect, confidence=conf(p, effect) if rising else 0.0, verdict="支持" if rising else "排除", p_value=p,
    finding=f"物流费用率 {lr0:.2%} → {lr1:.2%}；分渠道每件物流成本事件前后平均变化 {chg.mean():+.1%}（p = {p:.2f}）；"
            f"整体每件物流成本 {u0:.3f} → {u1:.3f} 元。" + ("物流费用率反而下降，物流总额上升来自销量增长，服务商更换不是原因" if not rising else "物流成本确有上升"),
    evidence=[f"分渠道每件成本变化：" + "，".join(f"{k} {v:+.1%}" for k, v in chg.items())])
'''

NEW_PRODUCT = '''
# 假设：新品“«SKUNAME»”（«DATE» 上市）利润率低，拉低整体«METRICNAME»
# 方法：新品主要替代同产品线的老品，影响 ≈ 新品营收占比 × (新品毛利率 − 同线其他 SKU 毛利率)；影响为正或可忽略时排除
''' + FMCG_HELPERS + '''
SKU = __SKU__
d = q("""SELECT s.sku_id, SUM(s.net_revenue) AS rev, SUM(s.sales_qty * c.unit_cogs) AS cogs FROM fact_sales_daily s
         JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
         JOIN dim_sku k ON k.sku_id = s.sku_id
         WHERE k.product_line = (SELECT product_line FROM dim_sku WHERE sku_id = ?) AND s.dt BETWEEN ? AND ? GROUP BY s.sku_id""", (SKU, P1S, P1E)).set_index("sku_id")
new, line = d.loc[SKU], d.drop(SKU)
share = new.rev / B.rev.sum()
gm_new, gm_line = 1 - new.cogs / new.rev, 1 - line.cogs.sum() / line.rev.sum()
effect = share * (gm_new - gm_line)
lowers = effect < -0.001
result = dict(
    effect=effect if lowers else 0.0, confidence=0.9 if lowers else 0.1, verdict="支持" if lowers else "排除", p_value=None,
    finding=f"新品营收占比 {share:.2%}，毛利率 {gm_new:.1%}，同产品线其他 SKU {gm_line:.1%}；对«METRICNAME»影响约 {effect * 100:+.3f} 个百分点，"
            + ("确实拉低了利润率" if lowers else "没有拉低利润率，影响可以忽略"),
    evidence=[f"新品营收 {new.rev / 1e4:,.1f} 万元"])
'''

LOGI_UNIT_DERIVED = '''
# 检验：平均每件物流成本上升，是物流涨价，还是渠道结构（«CNAME»占比上升）的派生现象？
# 方法：把每件物流成本的变化拆成“各渠道单件成本变化”和“渠道发货结构变化”两部分
''' + FMCG_HELPERS + '''
CH = __CH__
u0, u1 = A.logi / A.shipped, B.logi / B.shipped
w0, w1 = A.shipped / A.shipped.sum(), B.shipped / B.shipped.sum()
total = (B.logi.sum() / B.shipped.sum()) - (A.logi.sum() / A.shipped.sum())
rate_part = ((u1 - u0) * (w0 + w1) / 2).sum()
mix_part = ((w1 - w0) * (u0 + u1) / 2).sum()
derived = total > 0 and mix_part > 0.6 * total
result = dict(
    effect=0.0, confidence=0.9 if derived else 0.3, verdict="派生" if derived else "支持",
    finding=f"平均每件物流成本变化 {total:+.3f} 元，其中渠道结构贡献 {mix_part:+.3f} 元、各渠道单件成本贡献 {rate_part:+.3f} 元；"
            f"«CNAME»单件成本 {u1[CH]:.3f} 元，是平均的 {u1[CH] / (B.logi.sum() / B.shipped.sum()):.1f} 倍。"
            + ("上升主要来自渠道结构变化，属于派生现象，不是物流涨价" if derived else "各渠道单件成本本身在上升"),
    evidence=[])
'''

CALENDAR = '''
# 口径因素：现期 «D1» 天、基期 «D0» 天，按总量比较时天数差异本身就会带来变动
# 方法：天数差 × 基期日均值（剔除基期大促日后的常态日均）
P0S, P0E, D0, D1, EXCLUDE = __P0S__, __P0E__, __D0__, __D1__, __EXCLUDE__
d = q("SELECT dt, SUM(pay_amount) AS gmv FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY dt", (P0S, P0E))
normal = d[~d.dt.isin(EXCLUDE)].gmv.mean()
effect = (D1 - D0) * normal
result = dict(effect=effect, confidence=1.0, verdict="支持", p_value=0.0,
              finding=f"现期比基期{'少' if D1 < D0 else '多'} {abs(D1 - D0)} 天，按基期常态日均 {normal / 1e4:,.1f} 万元计，总量口径下带来 {effect / 1e4:+,.1f} 万元变动；"
                      f"日均口径不受影响", evidence=["口径因素，不需要业务整改，汇报时同时给出日均口径"])
'''

SHARE_DERIVED = '''
# 检验：«CNAME»营收占比下降，是该渠道本身变差，还是其他渠道扩张后的被动下降？
# 方法：看该渠道营收本身的变化；基期若有大促，用常态月份 «NORMAL» 的月均营收作对照
''' + FMCG_HELPERS + '''
CH, NORMAL = __CH__, __NORMAL__
s0, s1 = A.rev[CH] / A.rev.sum(), B.rev[CH] / B.rev.sum()
m = q("SELECT substr(dt, 1, 7) AS month, SUM(net_revenue) AS rev FROM fact_sales_daily WHERE channel_id = ? GROUP BY month", (CH,)).set_index("month").rev
g_base = B.rev[CH] / A.rev[CH] - 1
g_norm = B.rev[CH] / m[NORMAL].mean() - 1
growth_other = B.rev.drop(CH).sum() / A.rev.drop(CH).sum() - 1
derived = s1 < s0 and g_norm > -0.05
result = dict(
    effect=0.0, confidence=0.9 if derived else 0.3, verdict="派生" if derived else "支持",
    finding=f"«CNAME»占比 {s0:.1%} → {s1:.1%}；营收较基期 {g_base:+.1%}，较常态月份均值 {g_norm:+.1%}（其余渠道合计较基期 {growth_other:+.1%}）。"
            + ("该渠道营收与常态持平，占比下降是其他渠道扩张造成的被动变化，属于派生现象" if derived else "该渠道营收本身在下降，需要单独排查"),
    evidence=[f"常态月份：{'、'.join(NORMAL)}"])
'''
