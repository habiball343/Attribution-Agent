"""贡献率分解算法。三种方法都满足“各项贡献之和严格等于总变动”。

1. 乘法模型（LMDI-I）：Y = x1 × x2 × ... × xn
     贡献_k = L(Y1, Y0) × ln(xk1 / xk0)，L(a, b) = (a − b) / (ln a − ln b)
   对数平均权重让各因子贡献相加恰好等于 Y1 − Y0，没有交叉残差。
2. 加法模型（差额分析）：Y = Σ ±x_k，贡献_k = ±(xk1 − xk0)。
3. 结构 × 水平（两因子 Shapley）：Y = Σ_i s_i × m_i（s 为占比，m 为分组指标）
     结构效应_i = Δs_i × ((m_i0 + m_i1)/2 − Ȳ)，水平效应_i = Δm_i × (s_i0 + s_i1)/2，Ȳ = (Y0 + Y1)/2
   因为 Σ Δs_i = 0，减去 Ȳ 不改变总和，但让单个分组的结构效应有了可比的含义（占比上升的高/低于平均组）。
"""
import math


def logmean(a, b):
    if a == b:
        return a
    if a <= 0 or b <= 0:
        raise ValueError("LMDI 要求各期数值为正")
    return (a - b) / (math.log(a) - math.log(b))


def lmdi(factors0, factors1):
    """factors0/1: {名称: 数值}，乘积即目标指标。返回 {名称: 贡献}。"""
    y0 = math.prod(factors0.values())
    y1 = math.prod(factors1.values())
    w = logmean(y1, y0)
    return {k: w * math.log(factors1[k] / factors0[k]) for k in factors0}


def additive(parts0, parts1, signs):
    """parts: {名称: 数值}；signs: {名称: +1/-1}。返回 {名称: 贡献}。"""
    return {k: signs[k] * (parts1[k] - parts0[k]) for k in parts0}


def mix_rate(groups0, groups1):
    """groups: {分组: (权重, 指标)}，权重为绝对量（如营收），函数内部换算成占比。
    返回 {分组: {"mix": 结构效应, "rate": 水平效应, "s0","s1","m0","m1"}} 和 (Y0, Y1)。"""
    keys = sorted(set(groups0) | set(groups1))
    w0 = sum(groups0.get(k, (0, 0))[0] for k in keys)
    w1 = sum(groups1.get(k, (0, 0))[0] for k in keys)
    y0 = sum(groups0[k][0] * groups0[k][1] for k in groups0) / w0
    y1 = sum(groups1[k][0] * groups1[k][1] for k in groups1) / w1
    ybar = (y0 + y1) / 2
    out = {}
    for k in keys:
        s0 = groups0.get(k, (0, 0))[0] / w0
        s1 = groups1.get(k, (0, 0))[0] / w1
        m0 = groups0.get(k, (0, None))[1]
        m1 = groups1.get(k, (0, None))[1]
        m0 = m1 if m0 is None else m0
        m1 = m0 if m1 is None else m1
        out[k] = dict(mix=(s1 - s0) * ((m0 + m1) / 2 - ybar), rate=(m1 - m0) * (s0 + s1) / 2, s0=s0, s1=s1, m0=m0, m1=m1)
    return out, (y0, y1)


def check_sum(contribs, total, tol=1e-4):
    """守恒校验：相对误差小于万分之一（设计目标小于 0.01%）。"""
    s = sum(contribs)
    err = abs(s - total) / max(abs(total), 1e-12)
    return dict(sum=s, total=total, diff=s - total, rel_error=err, ok=err < tol)
