/* 经营归因分析系统 前端（Vue 3 + Element Plus + ECharts，单文件，无构建步骤） */
const { createApp, reactive, ref, computed, onMounted, onBeforeUnmount, watch, nextTick } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;

/* ================================================================== 接口与工具 */
const store = reactive({ token: localStorage.getItem('attr_token') || '', user: null, system: null, page: 'dashboard', pending: null, reportId: null });

async function request(method, url, body) {
  const headers = {};
  if (store.token) headers.Authorization = 'Bearer ' + store.token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401) { logout(); throw new Error('登录已过期，请重新登录'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
}
const api = { get: u => request('GET', u), post: (u, b) => request('POST', u, b || {}), put: (u, b) => request('PUT', u, b || {}),
  patch: (u, b) => request('PATCH', u, b || {}), del: u => request('DELETE', u) };
const can = p => !!store.user && store.user.perms.includes(p);
function logout() { store.token = ''; store.user = null; localStorage.removeItem('attr_token'); }
const toast = e => ElMessage.error(e.message || String(e));
marked.use({ renderer: { html() { return ''; } }, breaks: true });
const md = t => marked.parse(t || '');

const C = { blue: '#3a6ea5', navy: '#14213d', red: '#c8553d', green: '#3c8d5a', amber: '#e09f3e', grey: '#8a94a6', teal: '#2a9d8f', purple: '#7b6ca8' };
const PALETTE = ['#3a6ea5', '#e09f3e', '#2a9d8f', '#c8553d', '#7b6ca8', '#5c8001', '#b56576', '#457b9d'];
const isEco = d => d === 'ecommerce';
const wan = v => v == null ? '—' : (v / 1e4).toLocaleString('zh-CN', { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + ' 万';
const swan = v => v == null ? '—' : (v >= 0 ? '+' : '') + wan(v);
const pct = (v, n = 2) => v == null ? '—' : (v * 100).toFixed(n) + '%';
const spct = (v, n = 1) => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(n) + '%';
const spp = (v, n = 2) => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(n) + ' pp';
const num = v => v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
function fmtVal(key, v, eco) {
  if (v == null) return '—';
  if (!eco) return ['rev', 'revenue'].includes(key) ? wan(v) : pct(v);
  if (['cvr', 'share', 'new_user_share', 'refund_rate'].includes(key)) return pct(v);
  if (['uv', 'orders', 'order_cnt'].includes(key)) return num(v);
  if (['aov', 'asp'].includes(key)) return v.toFixed(1) + ' 元';
  if (key === 'ipo') return v.toFixed(3);
  return wan(v);
}
const fmtEff = (v, eco) => eco ? swan(v) : spp(v);
const csvDownload = (name, header, rows) => {
  const esc = x => { const s = x == null ? '' : String(x); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const text = '﻿' + [header, ...rows].map(r => r.map(esc).join(',')).join('\n');
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' })); a.download = name + '.csv'; a.click();
};
const RANK_TAG = { '主要': 'danger', '次要': 'warning', '轻微': 'info', '反向因素': 'success', '口径因素': 'info' };
const VERDICT_TAG = { '支持': 'danger', '排除': 'info', '派生': 'warning' };
const SCOPE_TAG = { '内部原因': '', '外部诱因': 'warning', '日历与口径': 'info' };

/* ================================================================== 图表组件 */
const EChart = {
  props: { option: Object, height: { type: [String, Number], default: 280 } },
  emits: ['click'],
  setup(props, { emit }) {
    const el = ref(null); let chart = null; let ro = null;
    const render = () => { if (chart && props.option) chart.setOption(props.option, true); };
    onMounted(() => {
      chart = echarts.init(el.value);
      chart.on('click', p => emit('click', p));
      render();
      ro = new ResizeObserver(() => chart && chart.resize()); ro.observe(el.value);
    });
    onBeforeUnmount(() => { ro && ro.disconnect(); chart && chart.dispose(); chart = null; });
    watch(() => props.option, render, { deep: true });
    return { el, style: computed(() => ({ height: typeof props.height === 'number' ? props.height + 'px' : props.height, width: '100%' })) };
  },
  template: `<div ref="el" :style="style"></div>`
};

function waterfallOption(dec, eco) {
  if (!dec) return null;
  const conv = v => eco ? v / 1e4 : v * 100;
  const items = [['基期', conv(dec.v0), 'total'], ...dec.factors.map(f => [f.name, conv(f.contrib), 'delta']), ['现期', conv(dec.v1), 'total']];
  let run = 0; const base = [], up = [], down = [], tot = [];
  items.forEach(([n, v, k]) => {
    if (k === 'total') { tot.push(v); base.push('-'); up.push('-'); down.push('-'); run = v; }
    else { tot.push('-'); const lo = v >= 0 ? run : run + v; base.push(lo); (v >= 0 ? up : down).push(Math.abs(v)); (v >= 0 ? down : up).push('-'); run += v; }
  });
  const vals = items.map((x, i) => x[2] === 'total' ? x[1] : base[i]);
  const minV = eco ? 0 : Math.max(0, Math.min(...vals.filter(v => v !== '-')) - 3);
  const lab = i => items[i][2] === 'total' ? items[i][1].toFixed(eco ? 1 : 2) : (items[i][1] >= 0 ? '+' : '') + items[i][1].toFixed(eco ? 1 : 2);
  return {
    grid: { left: 50, right: 20, top: 30, bottom: 30 }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: ps => { const i = ps[0].dataIndex; const [n, v, k] = items[i]; return `${n}：${k === 'total' ? v.toFixed(2) : (v >= 0 ? '+' : '') + v.toFixed(2)} ${eco ? '万元' : (k === 'total' ? '%' : '个百分点')}` + (k === 'delta' && dec.change ? `<br/>贡献率 ${(dec.factors[i - 1].share * 100).toFixed(1)}%` : ''); } },
    xAxis: { type: 'category', data: items.map(x => x[0]), axisLabel: { interval: 0 } },
    yAxis: { type: 'value', min: minV, name: eco ? '万元' : '%', splitLine: { lineStyle: { type: 'dashed' } } },
    series: [
      { type: 'bar', stack: 'w', data: base, itemStyle: { color: 'transparent' }, silent: true },
      { type: 'bar', stack: 'w', data: up, itemStyle: { color: C.green }, label: { show: true, position: 'top', formatter: p => lab(p.dataIndex) } },
      { type: 'bar', stack: 'w', data: down, itemStyle: { color: C.red }, label: { show: true, position: 'bottom', formatter: p => lab(p.dataIndex) } },
      { type: 'bar', stack: 'w', data: tot, itemStyle: { color: C.blue }, label: { show: true, position: 'top', formatter: p => lab(p.dataIndex) }, barWidth: '45%' },
    ],
  };
}

function trendOption(series, events, opts = {}) {
  if (!series) return null;
  const s = Array.isArray(series) ? series : [series];
  const pctAxis = s.some(x => x.pct) || opts.pct;
  const marks = (events || []).filter(e => s[0].x.length && e.start <= s[0].x[s[0].x.length - 1] && e.end >= s[0].x[0]).slice(0, 12).map((e, i) => [
    { name: e.event_id + ' ' + e.title, xAxis: e.start < s[0].x[0] ? s[0].x[0] : e.start, itemStyle: { color: i % 2 ? 'rgba(224,159,62,.10)' : 'rgba(58,110,165,.08)' },
      label: { show: true, position: 'insideTop', fontSize: 10, color: '#6b7280', formatter: e.event_id } },
    { xAxis: e.end > s[0].x[s[0].x.length - 1] ? s[0].x[s[0].x.length - 1] : e.end }]);
  return {
    grid: { left: 56, right: s.some(x => x.axis) ? 56 : 18, top: 30, bottom: 50 }, legend: { top: 0, textStyle: { fontSize: 11 } },
    tooltip: { trigger: 'axis', valueFormatter: v => v == null ? '—' : (pctAxis ? (v * 100).toFixed(2) + '%' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })) },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 6 }],
    xAxis: { type: 'category', data: s[0].x },
    yAxis: [{ type: 'value', scale: true, axisLabel: { formatter: v => pctAxis ? (v * 100).toFixed(0) + '%' : (Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : v) } },
      { type: 'value', scale: true, show: s.some(x => x.axis), splitLine: { show: false } }],
    series: s.map((x, i) => ({ name: x.name, type: 'line', data: x.y, yAxisIndex: x.axis || 0, showSymbol: false, smooth: false, lineStyle: { width: i ? 1.4 : 2 },
      color: PALETTE[i], markArea: i === 0 && marks.length ? { silent: true, data: marks } : undefined,
      markLine: i === 0 && opts.mark ? { symbol: 'none', data: [{ xAxis: opts.mark }], lineStyle: { color: C.red, type: 'dashed' } } : undefined })),
  };
}

const sparkOption = (sp, color) => ({ grid: { left: 0, right: 0, top: 4, bottom: 0 }, xAxis: { type: 'category', show: false, data: sp.x },
  yAxis: { type: 'value', show: false, scale: true }, series: [{ type: 'line', data: sp.y, showSymbol: false, lineStyle: { width: 1.4, color }, areaStyle: { color, opacity: .08 } }] });

/* ================================================================== 登录 */
const LoginPage = {
  setup() {
    const f = reactive({ username: 'analyst', password: 'analyst123' }); const loading = ref(false);
    const demo = [['analyst', 'analyst123', '经营分析师'], ['manager', 'manager123', '业务负责人'], ['admin', 'admin123', '数据管理员']];
    async function login() {
      loading.value = true;
      try { const r = await api.post('/api/login', f); store.token = r.token; store.user = r.user; localStorage.setItem('attr_token', r.token); store.page = 'dashboard'; await loadSystem(); }
      catch (e) { toast(e); } finally { loading.value = false; }
    }
    return { f, loading, login, demo };
  },
  template: `
  <div class="login-wrap"><div class="login-card">
    <div class="login-brand"><svg viewBox="0 0 32 32" width="34" height="34"><rect width="32" height="32" rx="7" fill="#14213d"/><path d="M6 22 L12 14 L17 18 L25 8" stroke="#f2a541" stroke-width="3" fill="none"/></svg>
      <div><div class="login-title">经营归因分析系统</div><div class="login-sub">指标拆解 · 假设检验 · 事件关联 · What-If 推演</div></div></div>
    <el-form @submit.prevent="login" label-position="top">
      <el-form-item label="用户名"><el-input v-model="f.username" /></el-form-item>
      <el-form-item label="密码"><el-input v-model="f.password" type="password" show-password @keyup.enter="login" /></el-form-item>
      <el-button type="primary" :loading="loading" style="width:100%" @click="login">登录</el-button>
    </el-form>
    <div class="login-demo">演示账号：<el-link v-for="d in demo" :key="d[0]" @click="f.username=d[0]; f.password=d[1]" style="margin-right:10px">{{d[2]}}（{{d[0]}}）</el-link></div>
  </div></div>`
};
async function loadSystem() { try { store.system = await api.get('/api/system'); } catch (e) { /* ignore */ } }

/* ================================================================== 指标异动大盘 */
const DashboardPage = {
  components: { EChart },
  setup() {
    const st = reactive({ domain: 'ecommerce', data: null, loading: false, metric: null });
    async function load() {
      st.loading = true;
      try { st.data = await api.get('/api/dashboard?domain=' + st.domain); st.metric = st.data.cards[0].key; } catch (e) { toast(e); } finally { st.loading = false; }
    }
    onMounted(load);
    watch(() => st.domain, load);
    const eco = computed(() => isEco(st.domain));
    const mainOption = computed(() => {
      const d = st.data; if (!d) return null;
      const s = d.series;
      const series = eco.value ? [{ name: '日 GMV', x: s.x, y: s.gmv }, { name: '日访客数', x: s.x, y: s.uv, axis: 1 }]
        : [{ name: '日毛利率', x: s.x, y: s.gross_margin, pct: true }, { name: '日折扣率', x: s.x, y: s.discount_rate, pct: true }];
      return trendOption(series, d.events);
    });
    function cardVal(c) { return c.unit === '%' ? pct(c.value) : c.unit === '元' && (c.key === 'aov') ? c.value.toFixed(1) + ' 元' : c.unit === '元' ? wan(c.value) + '元' : num(c.value); }
    function cardMom(c) { return c.ratio ? spp(c.mom) : (c.unit === '%' ? spct(c.mom) : spct(c.mom)); }
    function analyze(q) { store.pending = { question: q, domain: st.domain }; store.page = 'workbench'; }
    return { st, eco, mainOption, cardVal, cardMom, analyze, sparkOption, can, C };
  },
  template: `
  <div class="page" v-loading="st.loading">
    <div class="page-head">
      <div><h2>指标异动大盘</h2><div class="muted" v-if="st.data">{{st.data.title}}｜{{st.data.current}} 对比 {{st.data.base}}｜月环比超过 {{(st.data.thresholds.month*100).toFixed(0)}}%、单日跌幅超过 {{(st.data.thresholds.day*100).toFixed(0)}}% 标红</div></div>
      <el-radio-group v-model="st.domain"><el-radio-button value="ecommerce">青禾优选电商</el-radio-button><el-radio-button value="fmcg">禾悦食品快消</el-radio-button></el-radio-group>
    </div>
    <div class="kpi-grid" v-if="st.data">
      <div v-for="c in st.data.cards" :key="c.key" class="kpi" :class="{flag: c.flag}">
        <div class="kpi-top"><span class="kpi-name">{{c.name}}</span><el-tag v-if="c.flag" type="danger" size="small" effect="dark">异动</el-tag></div>
        <div class="kpi-val">{{cardVal(c)}}</div>
        <div class="kpi-sub"><span :class="c.mom < 0 ? 'neg' : 'pos'">{{cardMom(c)}}</span> <span class="muted">较 {{st.data.base}}{{c.mom_basis === '日均' ? '（日均）' : ''}}</span></div>
        <div class="kpi-sub muted" v-if="c.worst_day">近 30 天最大单日跌幅 {{spct(c.worst_dod)}}（{{c.worst_day}}）</div>
        <div class="kpi-sub muted" v-if="c.vs_best != null && c.ratio">较半年最好月份 {{spp(c.vs_best)}}</div>
        <e-chart :option="sparkOption(c.spark, c.flag ? C.red : C.blue)" :height="42" />
        <el-button v-if="can('analysis:run')" size="small" :type="c.flag ? 'danger' : 'default'" plain class="kpi-btn" @click="analyze(c.question)">智能归因</el-button>
      </div>
    </div>
    <div class="grid-2" v-if="st.data">
      <el-card shadow="never" class="span-2"><template #header><b>日趋势与业务事件</b><span class="muted">（阴影为事件日历中的事件，可拖动底部滑块缩放）</span></template>
        <e-chart :option="mainOption" :height="300" /></el-card>
      <el-card shadow="never"><template #header><b>异动清单</b><span class="muted">（自动扫描：突变、尖峰、断货、小时级骤降、结构与费率变化）</span></template>
        <div v-for="(a, i) in st.data.anomalies" :key="i" class="anom"><el-tag size="small" :type="a.level==='高' ? 'danger' : 'warning'">{{a.level}}</el-tag><span>{{a.text}}</span></div>
        <div class="muted" v-if="!st.data.anomalies.length">暂无异动</div>
        <el-button v-if="can('analysis:run')" type="primary" style="margin-top:10px" @click="analyze(st.data.default_question)">对主指标发起归因：{{st.data.default_question}}</el-button>
      </el-card>
      <el-card shadow="never"><template #header><b>业务事件</b></template>
        <el-table :data="st.data.events" size="small" max-height="300">
          <el-table-column prop="event_id" label="编号" width="64" /><el-table-column prop="title" label="事件" />
          <el-table-column prop="event_type" label="类型" width="86" /><el-table-column label="日期" width="170"><template #default="{row}">{{row.start}}{{row.end !== row.start ? ' 至 ' + row.end.slice(5) : ''}}</template></el-table-column>
        </el-table></el-card>
    </div>
  </div>`,
  methods: { pct, spct, spp }
};

/* ================================================================== 归因工作台 */
const wb = reactive({ sessions: [], sid: null, session: null, messages: [], state: {}, snapshots: [], running: false, input: '', domain: 'ecommerce',
  tab: 'overview', left: true, right: true, snapshot: null, focusIdx: -1, evidence: [], whatifBusy: false, pivot: { d1: '', d2: '', data: null }, sub: null,
  hypFilter: '全部', codeDlg: { show: false, h: null, code: '', running: false, out: null } });

async function loadSessions() { try { wb.sessions = await api.get('/api/sessions'); } catch (e) { toast(e); } }
async function openSession(sid) {
  try {
    const r = await api.get('/api/sessions/' + sid);
    wb.sid = sid; wb.session = r.session; wb.state = r.state || {}; wb.snapshots = r.snapshots; wb.snapshot = null;
    wb.messages = r.messages.map(m => ({ role: m.role, text: m.content, steps: (m.events || []).filter(e => e.event === 'step' && e.status !== 'running'),
      route: (m.events || []).find(e => e.event === 'route'), notices: (m.events || []).filter(e => e.event === 'notice').map(e => e.text), done: true }));
    wb.domain = r.session.domain || wb.domain; wb.focusIdx = (wb.state.drill_history || []).length - 1; wb.pivot.data = null; wb.sub = null;
    if (r.running) ElMessage.info('该会话的分析仍在后台执行，稍后刷新查看结果');
  } catch (e) { toast(e); }
}
async function newSession(domain) {
  const r = await api.post('/api/sessions', { domain }); await loadSessions(); await openSession(r.id); return r.id;
}

function suggestions(state) {
  const c = state.conclusion; if (!c) return [];
  const out = [];
  const eco = isEco(state.intent.domain);
  for (const x of c.causes.filter(x => ['主要', '次要'].includes(x.rank)).slice(0, 2)) {
    const m = /(渠道|类目|产品线)=([^；]+)/.exec(x.slice || '');
    if (m) out.push(eco ? `${m[2]}为什么变化这么大？` : `${m[2]}的利润结构怎么了？`);
  }
  out.push(eco ? '如果信息流恢复 80%、补货到位 100% 会怎样？' : '如果社区团购折扣降到 30%、佣金率降到 8% 会怎样？');
  if (c.excluded.length) out.push(`为什么排除${c.excluded[0].title.replace(/[“”]/g, '').slice(0, 12)}？`);
  out.push(eco ? '列出 9 月各区域的 GMV 和订单数' : '9 月各产品线的营收和毛利率');
  return out.slice(0, 5);
}

async function ask(question) {
  const q = (question || wb.input).trim(); if (!q || wb.running) return;
  if (!wb.sid) await newSession(wb.domain);
  wb.input = ''; wb.snapshot = null;
  wb.messages.push({ role: 'user', text: q });
  const msg = reactive({ role: 'assistant', text: '', steps: [], hyps: [], notices: [], route: null, done: false, error: null });
  wb.messages.push(msg); wb.running = true;
  scrollChat();
  try {
    const res = await fetch(`/api/sessions/${wb.sid}/ask`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + store.token },
      body: JSON.stringify({ question: q, domain: wb.messages.length <= 2 ? wb.domain : null }) });
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || res.status); }
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith('data:')) continue;
        onEvent(msg, JSON.parse(chunk.slice(5)));
      }
    }
  } catch (e) { msg.error = String(e.message || e); }
  msg.done = true; wb.running = false; loadSessions();
  const r = await api.get('/api/sessions/' + wb.sid).catch(() => null); if (r) { wb.snapshots = r.snapshots; wb.session = r.session; }
}

function onEvent(msg, ev) {
  const S = wb.state;
  switch (ev.event) {
    case 'route': msg.route = ev; break;
    case 'step': { const k = msg.steps.findIndex(s => s.key === ev.key); if (k >= 0) msg.steps[k] = ev; else msg.steps.push(ev); scrollChat(); break; }
    case 'notice': msg.notices.push(ev.text); break;
    case 'intent': wb.state = { intent: ev.intent, domain_title: ev.domain_title }; wb.domain = ev.intent.domain; wb.focusIdx = -1; wb.pivot.data = null; wb.sub = null; break;
    case 'signals': Object.assign(wb.state, { signals: ev.signals, series: ev.series, events: ev.events }); break;
    case 'decomp': Object.assign(wb.state, { decomp: ev.decomp, tree: ev.tree }); wb.tab = 'decomp'; break;
    case 'drill': Object.assign(wb.state, { drills: ev.drills, cross: ev.cross, sankey: ev.sankey }); break;
    case 'hypotheses': wb.state.hypotheses = ev.hypotheses; msg.hyps = ev.hypotheses.map(h => ({ id: h.id, title: h.title, group: h.group, verdict: null })); wb.tab = 'hyp'; break;
    case 'hyp_result': {
      const h = ev.hypothesis; const L = wb.state.hypotheses || []; const k = L.findIndex(x => x.id === h.id); if (k >= 0) L[k] = h;
      const m = msg.hyps.find(x => x.id === h.id); if (m) { m.verdict = h.result ? h.result.verdict : '失败'; m.conf = h.result ? h.result.confidence : 0; }
      break;
    }
    case 'conclusion': Object.assign(wb.state, { conclusion: ev.conclusion, event_table: ev.event_table }); wb.tab = 'overview'; break;
    case 'actions': Object.assign(wb.state, { actions: ev.actions, whatif: ev.whatif, whatif_result: ev.whatif_result, whatif_compare: ev.whatif_compare }); break;
    case 'delta': msg.text += ev.text; scrollChat(); break;
    case 'answer': msg.text = ev.text; break;
    case 'drill_focus': wb.state.drill_history = [...(wb.state.drill_history || []), ev.focus]; wb.state.focus = ev.path; wb.focusIdx = wb.state.drill_history.length - 1; wb.tab = 'drill'; break;
    case 'whatif_result': wb.state.whatif_result = ev.whatif_result; wb.state.whatif_values = ev.values; wb.tab = 'whatif'; break;
    case 'query_result': wb.state.queries = [...(wb.state.queries || []), ev.result]; wb.tab = 'query'; break;
    case 'evidence': wb.evidence = ev.hypotheses.map(h => h.id); wb.tab = 'hyp'; wb.hypFilter = '追问相关'; break;
    case 'state': wb.state = ev.state; if (wb.focusIdx >= (ev.state.drill_history || []).length) wb.focusIdx = (ev.state.drill_history || []).length - 1; break;
    case 'error': msg.error = ev.text; break;
  }
}
function scrollChat() { nextTick(() => { const el = document.querySelector('.chat-body'); if (el) el.scrollTop = el.scrollHeight; }); }

const STEP_ICON = { running: '⏳', done: '✓', error: '✗' };

const WorkbenchPage = {
  components: { EChart },
  setup() {
    onMounted(async () => {
      await loadSessions();
      if (store.pending) { const p = store.pending; store.pending = null; wb.domain = p.domain; await newSession(p.domain); ask(p.question); }
      else if (!wb.sid && wb.sessions.length) openSession(wb.sessions[0].id);
    });
    const S = computed(() => wb.snapshot ? wb.snapshot.state : wb.state);
    const eco = computed(() => S.value.intent ? isEco(S.value.intent.domain) : isEco(wb.domain));
    const examples = computed(() => isEco(wb.domain) ? ['9 月 GMV 比 8 月下降了多少？为什么？', '9 月客单价比 8 月为什么下降？', '8 月 GMV 比 7 月为什么上升？']
      : ['9 月净利率比 6 月下降了多少个百分点？为什么？', '9 月毛利率比 6 月下降了多少？为什么？', '8 月净利率比 7 月为什么下降？']);
    const sugg = computed(() => wb.running ? [] : suggestions(wb.state));
    async function rename(s) {
      try { const { value } = await ElMessageBox.prompt('会话名称', '重命名', { inputValue: s.title }); await api.put('/api/sessions/' + s.id, { title: value }); loadSessions(); } catch (e) { if (e !== 'cancel') toast(e); }
    }
    async function remove(s) {
      try { await ElMessageBox.confirm(`删除会话“${s.title}”？`, '确认', { type: 'warning' }); await api.del('/api/sessions/' + s.id); if (wb.sid === s.id) { wb.sid = null; wb.messages = []; wb.state = {}; } loadSessions(); } catch (e) { if (e !== 'cancel') toast(e); }
    }
    async function viewSnap(sn) {
      try { const r = await api.get(`/api/sessions/${wb.sid}/snapshots/${sn.id}`); wb.snapshot = { id: sn.id, turn: r.turn, question: r.question, state: r.state }; } catch (e) { toast(e); }
    }
    async function makeReport() {
      try { const r = await api.post(`/api/sessions/${wb.sid}/report`, {}); store.reportId = r.id; store.page = 'reports'; } catch (e) { toast(e); }
    }
    function startNew() { wb.sid = null; wb.session = null; wb.messages = []; wb.state = {}; wb.snapshots = []; wb.snapshot = null; }
    return { wb, S, eco, examples, sugg, ask, openSession, rename, remove, viewSnap, makeReport, startNew, md, can, STEP_ICON, VERDICT_TAG, wan, swan, pct, spp, fmtVal, fmtEff };
  },
  template: `
  <div class="wb" :class="{'no-left': !wb.left, 'no-right': !wb.right}">
    <!-- 左栏：会话与指标导航 -->
    <aside class="wb-left" v-show="wb.left">
      <div class="wb-left-head"><b>归因会话</b><el-button size="small" type="primary" @click="startNew" v-if="can('analysis:run')">新建分析</el-button></div>
      <div class="sess-list">
        <div v-for="s in wb.sessions" :key="s.id" class="sess" :class="{active: s.id===wb.sid}" @click="openSession(s.id)">
          <div class="sess-title">{{s.title}}<el-tag v-if="s.running" size="small" type="warning">执行中</el-tag></div>
          <div class="sess-meta">{{s.metric || '未分析'}} · {{s.turns}} 轮 · {{(s.updated_at||'').slice(5,16)}}</div>
          <div class="sess-ops" v-if="can('analysis:run')" @click.stop><el-link @click="rename(s)">重命名</el-link><el-link type="danger" @click="remove(s)">删除</el-link></div>
        </div>
        <div class="muted" v-if="!wb.sessions.length" style="padding:10px">还没有会话</div>
      </div>
      <div class="target" v-if="S.intent && S.decomp">
        <div class="target-h">当前目标指标</div>
        <div class="target-name">{{S.decomp.name}}<span class="muted">｜{{S.domain_title}}</span></div>
        <div class="target-row"><span>{{S.intent.base.label}}</span><b>{{fmtVal(S.intent.metric, S.decomp.v0, eco)}}</b></div>
        <div class="target-row"><span>{{S.intent.current.label}}</span><b>{{fmtVal(S.intent.metric, S.decomp.v1, eco)}}</b></div>
        <div class="target-row"><span>变动</span><b :class="S.decomp.change<0?'neg':'pos'">{{fmtEff(S.decomp.change, eco)}}<template v-if="S.decomp.change_rate!=null">（{{(S.decomp.change_rate*100).toFixed(1)}}%）</template></b></div>
        <div class="target-row" v-if="S.decomp.daily_change_rate!=null"><span>日均口径</span><b>{{(S.decomp.daily_change_rate*100).toFixed(1)}}%</b></div>
        <div class="target-row" v-if="S.focus && S.focus.length"><span>下钻路径</span><b>{{S.focus.map(f=>f[2]).join(' → ')}}</b></div>
      </div>
      <div class="snaps" v-if="wb.snapshots.length">
        <div class="target-h">分析快照（每轮自动保存）</div>
        <div v-for="sn in wb.snapshots" :key="sn.id" class="snap" :class="{active: wb.snapshot && wb.snapshot.id===sn.id}" @click="viewSnap(sn)">
          <span class="snap-turn">#{{sn.turn}}</span><span class="snap-q">{{sn.question}}</span></div>
      </div>
    </aside>

    <!-- 中栏：对话与探索 -->
    <section class="wb-mid">
      <div class="mid-head">
        <el-button text size="small" @click="wb.left=!wb.left">{{wb.left ? '◀ 收起' : '▶ 会话'}}</el-button>
        <div class="mid-title">{{wb.session ? wb.session.title : '新的归因分析'}}</div>
        <el-select v-if="!wb.messages.length" v-model="wb.domain" size="small" style="width:150px"><el-option label="青禾优选电商" value="ecommerce"/><el-option label="禾悦食品快消" value="fmcg"/></el-select>
        <el-button text size="small" @click="wb.right=!wb.right">{{wb.right ? '收起看板 ▶' : '◀ 看板'}}</el-button>
      </div>
      <div class="chat-body">
        <div v-if="!wb.messages.length" class="empty">
          <h3>用一句话发起归因</h3><p class="muted">系统会依次完成：识别异动 → 指标拆解 → 维度下钻 → 假设生成 → 沙箱检验 → 业务事件关联 → 策略建议。之后可以继续追问某个渠道、类目，或做 What-If 推演。</p>
          <div class="ex" v-for="e in examples" :key="e" @click="ask(e)">{{e}}</div>
        </div>
        <div v-for="(m, i) in wb.messages" :key="i" :class="['msg', m.role]">
          <div v-if="m.role==='user'" class="bubble">{{m.text}}</div>
          <div v-else class="answer">
            <div class="route" v-if="m.route && m.route.route!=='analyze'">↳ {{m.route.label}}</div>
            <div class="steps" v-if="m.steps.length">
              <div v-for="s in m.steps" :key="s.key" :class="['step', s.status]"><span class="step-ic">{{STEP_ICON[s.status]}}</span><b>{{s.name}}</b>
                <span class="step-ms" v-if="s.ms!=null">{{s.ms}} ms</span><div class="step-sum" v-if="s.summary">{{s.summary}}</div></div>
            </div>
            <div class="hyp-stream" v-if="m.hyps && m.hyps.length">
              <div v-for="h in m.hyps" :key="h.id" class="hs"><span class="hs-id">{{h.id}}</span><span class="hs-t">{{h.title}}</span>
                <el-tag size="small" v-if="h.verdict" :type="VERDICT_TAG[h.verdict]||'info'">{{h.verdict}}<template v-if="h.verdict==='支持'"> {{h.conf.toFixed(2)}}</template></el-tag>
                <span v-else class="muted">沙箱执行中…</span></div>
            </div>
            <div v-for="n in m.notices" class="notice">{{n}}</div>
            <div class="md" v-html="md(m.text)"></div>
            <div v-if="!m.done && !m.text" class="muted typing">分析中…</div>
            <div v-if="m.error" class="err">{{m.error}}</div>
          </div>
        </div>
      </div>
      <div class="chat-foot">
        <div class="sugg" v-if="sugg.length && can('analysis:run')"><span class="muted">继续追问：</span><el-tag v-for="s in sugg" :key="s" class="sugg-tag" effect="plain" @click="ask(s)">{{s}}</el-tag></div>
        <div class="input-row" v-if="can('analysis:run')">
          <el-input v-model="wb.input" type="textarea" :autosize="{minRows:1,maxRows:4}" placeholder="输入归因问题或追问，例如：信息流广告为什么掉这么多？ / 如果社区团购折扣降到 30% 会怎样？" @keydown.enter.exact.prevent="ask()" :disabled="wb.running" />
          <el-button type="primary" :loading="wb.running" @click="ask()">发送</el-button>
        </div>
      </div>
    </section>

    <!-- 右栏：动态归因看板 -->
    <section class="wb-right" v-show="wb.right"><board-panel /></section>
  </div>`
};

/* ------------------------------------------------------------------ 右栏看板 */
const BoardPanel = {
  components: { EChart },
  setup() {
    const S = computed(() => wb.snapshot ? wb.snapshot.state : wb.state);
    const eco = computed(() => S.value.intent ? isEco(S.value.intent.domain) : true);
    const ro = computed(() => !!wb.snapshot || !can('analysis:run'));
    /* 拆解 */
    const wfOption = computed(() => waterfallOption(S.value.decomp, eco.value));
    const subOption = computed(() => wb.sub ? waterfallOption(wb.sub, eco.value) : null);
    const treeOption = computed(() => {
      const t = S.value.tree, d = S.value.decomp; if (!t || !d) return null;
      const contrib = {}; (d.factors || []).forEach(f => { contrib[f.key] = f.contrib; (f.children || []).forEach(c => contrib[c.key] = c.contrib); });
      const map = n => ({ name: n.name, code: n.code, value: contrib[n.code], children: (n.children || []).map(map),
        label: { formatter: () => n.name + (contrib[n.code] != null ? '\n' + fmtEff(contrib[n.code], eco.value) : (n.code === t.code ? '\n' + fmtEff(d.change, eco.value) : '')) },
        itemStyle: { color: contrib[n.code] == null ? C.blue : contrib[n.code] < 0 ? C.red : C.green } });
      return { tooltip: { formatter: p => (p.data.name + (p.data.code ? '（' + p.data.code + '）' : '')) }, series: [{ type: 'tree', data: [map(t)], orient: 'LR', top: 10, bottom: 10, left: 70, right: 130,
        symbolSize: 10, initialTreeDepth: 3, expandAndCollapse: false, label: { position: 'left', fontSize: 11, lineHeight: 15 }, leaves: { label: { position: 'right' } } }] };
    });
    async function clickTree(p) {
      const code = p.data && p.data.code; if (!code || !wb.sid || !S.value.intent || code === S.value.intent.metric) { wb.sub = null; return; }
      if (['mix', 'price', 'cost'].includes(code)) return;
      try { const r = await api.get(`/api/sessions/${wb.sid}/decompose?metric=${code}`); wb.sub = r.factors && r.factors.length ? r : null; if (!wb.sub) ElMessage.info(`${p.data.name}是原子指标，没有下一层拆解`); } catch (e) { toast(e); }
    }
    /* 下钻 */
    const drillDim = ref(0);
    const drillOption = computed(() => {
      const d = (S.value.drills || [])[drillDim.value]; if (!d) return null;
      const rows = [...d.rows].reverse();
      if (eco.value) return { grid: { left: 90, right: 60, top: 10, bottom: 20 }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } }, xAxis: { type: 'value', axisLabel: { formatter: v => (v / 1e4).toFixed(0) + '万' } },
        yAxis: { type: 'category', data: rows.map(r => r.key) }, series: [{ type: 'bar', data: rows.map(r => ({ value: r.change, itemStyle: { color: r.change < 0 ? C.red : C.green } })),
          label: { show: true, position: 'right', formatter: p => swan(p.value) } }] };
      return { grid: { left: 130, right: 30, top: 30, bottom: 20 }, legend: { top: 0 }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: v => spp(v) },
        xAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(1) } }, yAxis: { type: 'category', data: rows.map(r => r.key) },
        series: [{ name: '结构效应', type: 'bar', stack: 's', data: rows.map(r => r.mix), color: C.amber }, { name: '水平效应', type: 'bar', stack: 's', data: rows.map(r => r.rate), color: C.blue }] };
    });
    const focus = computed(() => (S.value.drill_history || [])[wb.focusIdx]);
    const focusOption = computed(() => focus.value ? trendOption(focus.value.series, focus.value.events) : null);
    const sankeyOption = computed(() => { const s = S.value.sankey; if (!s) return null;
      return { title: { text: s.title, textStyle: { fontSize: 12, fontWeight: 'normal' } }, tooltip: {}, series: [{ type: 'sankey', top: 30, data: s.nodes, links: s.links, emphasis: { focus: 'adjacency' },
        lineStyle: { color: 'gradient', opacity: .35 }, label: { fontSize: 11 }, nodeGap: 6 }] }; });
    const dimOpts = computed(() => { const d = S.value.intent ? (isEco(S.value.intent.domain) ? { channel: '渠道', category: '类目', region: '区域', user_type: '新老客', product: '商品' } : { channel: '渠道', product_line: '产品线', sku: 'SKU' }) : {}; return d; });
    async function loadPivot() {
      if (!wb.pivot.d1) { const k = Object.keys(dimOpts.value); wb.pivot.d1 = k[0]; wb.pivot.d2 = k[1]; }
      try { wb.pivot.data = await api.get(`/api/sessions/${wb.sid}/pivot?d1=${wb.pivot.d1}` + (wb.pivot.d2 ? `&d2=${wb.pivot.d2}` : '')); } catch (e) { toast(e); }
    }
    function exportPivot() {
      const p = wb.pivot.data; if (!p) return; const rows = [];
      p.rows.forEach(r => { rows.push([r.key, '', r.v0, r.v1, r.change, r.contrib_share, r.gm_change_pp ?? '']); (r.children || []).forEach(c => rows.push([r.key, c.key, c.v0, c.v1, c.change, c.contrib_share, c.gm_change_pp ?? ''])); });
      csvDownload('透视表_' + p.d1_name + (p.d2_name ? '_' + p.d2_name : ''), [p.d1_name, p.d2_name || '', '基期' + p.measure, '现期' + p.measure, '差额', '贡献率', '毛利率变动'], rows);
    }
    const heat = (row, col) => {
      if (!['change', 'contrib_share', 'gm_change_pp'].includes(col.property)) return {};
      const v = row[col.property]; if (v == null) return {};
      const scale = col.property === 'change' ? Math.min(1, Math.abs(v) / Math.max(1, Math.abs(wb.pivot.data.total))) : Math.min(1, Math.abs(v) * (col.property === 'gm_change_pp' ? 10 : 1));
      return { background: (v < 0 ? 'rgba(200,85,61,' : 'rgba(60,141,90,') + (0.08 + scale * 0.45).toFixed(2) + ')' };
    };
    /* 假设 */
    const hyps = computed(() => {
      const L = S.value.hypotheses || []; const f = wb.hypFilter;
      if (f === '全部') return L; if (f === '追问相关') return L.filter(h => wb.evidence.includes(h.id));
      if (['支持', '排除', '派生'].includes(f)) return L.filter(h => h.result && h.result.verdict === f);
      return L.filter(h => h.group === f);
    });
    async function review(h, accepted) {
      try { wb.state = await api.patch(`/api/sessions/${wb.sid}/hypotheses/${h.id}`, { accepted }); ElMessage.success('结论、行动项与 What-If 已按审核结果重算'); } catch (e) { toast(e); }
    }
    function openCode(h) { Object.assign(wb.codeDlg, { show: true, h, code: h.code, running: false, out: null }); }
    async function rerun() {
      wb.codeDlg.running = true;
      try { const r = await api.post(`/api/sessions/${wb.sid}/hypotheses/${wb.codeDlg.h.id}/rerun`, { code: wb.codeDlg.code }); wb.codeDlg.out = r;
        if (r.ok) { wb.state = r.state; ElMessage.success('已重新执行并更新结论'); } } catch (e) { toast(e); } finally { wb.codeDlg.running = false; }
    }
    const miniOption = h => { const s = h.result && h.result.series; if (!s || !s.x) return null;
      return { grid: { left: 40, right: 8, top: 8, bottom: 18 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: s.x, axisLabel: { fontSize: 9 } },
        yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9, formatter: v => Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : (Math.abs(v) < 1 ? (v * 100).toFixed(0) + '%' : v) } },
        series: [{ type: 'line', data: s.y, showSymbol: false, color: C.blue, markLine: s.mark ? { symbol: 'none', data: [{ xAxis: s.mark }], lineStyle: { color: C.red } } : undefined }] }; };
    /* 证据链 */
    const chainOption = computed(() => S.value.series ? trendOption(S.value.series, S.value.events) : null);
    /* What-If */
    let timer = null;
    const wvals = reactive({});
    watch(() => S.value.whatif, w => { Object.keys(wvals).forEach(k => delete wvals[k]); if (w) w.levers.forEach(l => wvals[l.key] = (S.value.whatif_values || {})[l.key] ?? l.default); }, { immediate: true });
    function simulate(name) {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        if (ro.value) return;
        try { const r = await api.post(`/api/sessions/${wb.sid}/whatif`, { values: { ...wvals }, name: typeof name === 'string' ? name : null });
          wb.state.whatif_result = r.result; wb.state.saved_scenarios = r.saved; } catch (e) { toast(e); }
      }, typeof name === 'string' ? 0 : 180);
    }
    function resetLevers() { S.value.whatif.levers.forEach(l => wvals[l.key] = l.default); simulate(); }
    function applyPreset(p) { S.value.whatif.levers.forEach(l => wvals[l.key] = p.values[l.key] ?? l.default); simulate(); }
    async function saveScenario() {
      try { const { value } = await ElMessageBox.prompt('情景名称', '保存当前参数为情景', { inputValue: '情景 ' + ((S.value.saved_scenarios || []).length + 1) }); simulate(value); } catch (e) { /* cancel */ }
    }
    async function delScenario(s) { try { const r = await api.del(`/api/sessions/${wb.sid}/scenarios/${encodeURIComponent(s.name)}`); wb.state.saved_scenarios = r.saved; } catch (e) { toast(e); } }
    const wr = computed(() => S.value.whatif_result);
    const fw = v => eco.value ? wan(v) + '元' : pct(v);
    const contribOption = computed(() => { const r = wr.value; if (!r) return null; const c = r.contrib.filter(x => Math.abs(x.delta) > 1e-9);
      return { grid: { left: 150, right: 70, top: 6, bottom: 20 }, xAxis: { type: 'value', axisLabel: { formatter: v => eco.value ? (v / 1e4).toFixed(0) + '万' : (v * 100).toFixed(1) } },
        yAxis: { type: 'category', data: c.map(x => x.name) }, series: [{ type: 'bar', data: c.map(x => ({ value: x.delta, itemStyle: { color: x.delta < 0 ? C.red : C.green } })),
          label: { show: true, position: 'right', formatter: p => fmtEff(p.value, eco.value) } }] }; });
    const sensKey = ref('');
    const sensOption = computed(() => { const r = wr.value; if (!r || !r.curves) return null; const k = sensKey.value || Object.keys(r.curves)[0]; const cv = r.curves[k]; if (!cv) return null;
      const lv = S.value.whatif.levers.find(l => l.key === k);
      return { grid: { left: 60, right: 16, top: 26, bottom: 30 }, title: { text: '敏感度：' + cv.name, textStyle: { fontSize: 12, fontWeight: 'normal' } }, tooltip: { trigger: 'axis', valueFormatter: v => fw(v) },
        xAxis: { type: 'category', data: cv.x.map(x => +x.toFixed(2)), name: lv ? lv.unit : '' }, yAxis: { type: 'value', scale: true, axisLabel: { formatter: v => eco.value ? (v / 1e4).toFixed(0) + '万' : (v * 100).toFixed(1) + '%' } },
        series: [{ type: 'line', data: cv.y, color: C.blue, markLine: { symbol: 'none', data: [{ yAxis: r.base, label: { formatter: '基线' } }], lineStyle: { color: C.grey, type: 'dashed' } } }] }; });
    const scenRows = computed(() => [...(S.value.whatif_compare || []).map(x => ({ ...x, kind: '预置' })), ...(S.value.saved_scenarios || []).map(x => ({ ...x, kind: '已保存' }))]);
    const scenOption = computed(() => { const L = scenRows.value; if (!L.length || !wr.value) return null;
      const conv = v => eco.value ? v / 1e4 : v * 100;
      return { grid: { left: 40, right: 16, top: 20, bottom: 70 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: ['基线', ...L.map(x => x.name)], axisLabel: { interval: 0, rotate: 18, fontSize: 10 } },
        yAxis: { type: 'value', scale: true, name: eco.value ? '万元' : '%' },
        series: [{ type: 'bar', data: [conv(wr.value.base), ...L.map(x => conv(x.predicted))].map(v => +v.toFixed(2)), color: C.blue, barWidth: '40%' },
          { type: 'custom', renderItem: (p, api2) => { const x = api2.coord([api2.value(0), api2.value(1)]); const y2 = api2.coord([api2.value(0), api2.value(2)]);
            return { type: 'group', children: [{ type: 'line', shape: { x1: x[0], y1: x[1], x2: y2[0], y2: y2[1] }, style: { stroke: C.navy, lineWidth: 1.2 } },
              { type: 'line', shape: { x1: x[0] - 5, y1: x[1], x2: x[0] + 5, y2: x[1] }, style: { stroke: C.navy } }, { type: 'line', shape: { x1: y2[0] - 5, y1: y2[1], x2: y2[0] + 5, y2: y2[1] }, style: { stroke: C.navy } }] }; },
            data: L.map((x, i) => [i + 1, conv(x.band[0]), conv(x.band[1])]), z: 3 }] }; });
    function levelLabel(l) { const v = wvals[l.key]; return (v ?? l.default) + (l.unit || ''); }
    /* 查数 */
    const qf = reactive({ q: '', mode: '', busy: false });
    async function runSql() {
      if (!qf.q.trim()) return; qf.busy = true;
      try { const r = await api.post('/api/sql', { question: qf.q, domain: S.value.intent ? S.value.intent.domain : wb.domain, mode: qf.mode || null }); wb.state.queries = [...(wb.state.queries || []), r]; }
      catch (e) { toast(e); } finally { qf.busy = false; }
    }
    function exportQuery(r) { csvDownload('查询结果', r.columns, r.rows); }
    const sqlModes = computed(() => (store.system && store.system.sql_modes) || []);
    const rankGroups = computed(() => { const c = S.value.conclusion; if (!c) return []; const order = ['主要', '次要', '反向因素', '口径因素', '轻微'];
      return order.map(r => ({ rank: r, items: c.causes.filter(x => x.rank === r) })).filter(g => g.items.length); });
    const maxEff = computed(() => Math.max(1e-12, ...((S.value.conclusion || {}).causes || []).map(c => Math.abs(c.effect))));
    return { store, wb, S, eco, ro, wfOption, subOption, treeOption, clickTree, drillDim, drillOption, focus, focusOption, sankeyOption, dimOpts, loadPivot, exportPivot, heat,
      hyps, review, openCode, rerun, miniOption, chainOption, wvals, simulate, resetLevers, applyPreset, saveScenario, delScenario, wr, fw, contribOption, sensKey, sensOption,
      scenRows, scenOption, levelLabel, qf, runSql, exportQuery, sqlModes, rankGroups, maxEff, can, md, fmtVal, fmtEff, wan, swan, pct, spct, spp, num,
      RANK_TAG, VERDICT_TAG, SCOPE_TAG, makeReport: async () => { try { const r = await api.post(`/api/sessions/${wb.sid}/report`, {}); store.reportId = r.id; store.page = 'reports'; } catch (e) { toast(e); } } };
  },
  template: `
  <div class="board">
    <div class="board-head">
      <el-tabs v-model="wb.tab" class="board-tabs">
        <el-tab-pane label="结论" name="overview" /><el-tab-pane label="拆解" name="decomp" /><el-tab-pane label="下钻" name="drill" /><el-tab-pane label="假设" name="hyp" />
        <el-tab-pane label="证据链" name="chain" /><el-tab-pane label="What-If" name="whatif" /><el-tab-pane label="行动" name="actions" /><el-tab-pane label="查数" name="query" />
      </el-tabs>
      <el-button size="small" type="primary" plain :disabled="!S.conclusion || !can('report:edit') || !!wb.snapshot" @click="makeReport">生成报告</el-button>
    </div>
    <div class="snap-banner" v-if="wb.snapshot">正在查看第 {{wb.snapshot.turn}} 轮快照：{{wb.snapshot.question}} <el-link type="primary" @click="wb.snapshot=null">返回最新</el-link></div>
    <div class="board-body" v-if="!S.intent"><div class="empty muted">发起分析后，这里会展示因子瀑布图、下钻透视、假设看板、证据链、What-If 模拟和行动清单。</div></div>
    <div class="board-body" v-else>

      <!-- 结论 -->
      <div v-show="wb.tab==='overview'">
        <div v-if="!S.conclusion" class="muted">分析进行中…</div>
        <template v-else>
          <div class="headline">{{S.conclusion.headline}}</div>
          <div class="explain-bar">已解释 <b>{{(S.conclusion.explained_share*100).toFixed(0)}}%</b>，其他（自然波动与未识别因素）{{S.conclusion.residual_text}}；
            内部原因 {{S.conclusion.internal.length}} 条、外部诱因 {{S.conclusion.external.length}} 条</div>
          <div v-for="g in rankGroups" :key="g.rank" class="rank-group">
            <div class="rank-h"><el-tag :type="RANK_TAG[g.rank]" effect="dark" size="small">{{g.rank}}</el-tag></div>
            <div v-for="c in g.items" :key="c.id" class="cause">
              <div class="cause-top"><span class="cid">{{c.id}}</span><b>{{c.title}}</b><span class="cause-eff" :class="c.effect<0?'neg':'pos'">{{c.effect_text}}</span></div>
              <div class="bar"><div class="bar-in" :style="{width: (Math.abs(c.effect)/maxEff*100)+'%', background: c.effect<0 ? '#c8553d' : '#3c8d5a'}"></div><span class="bar-l">{{(c.share*100).toFixed(0)}}%</span></div>
              <div class="cause-meta"><el-tag size="small" :type="SCOPE_TAG[c.scope]">{{c.scope}}</el-tag><el-tag size="small" type="info">{{c.nature}}</el-tag>
                <el-tag size="small" v-for="e in c.events" :key="e.event_id" type="warning" effect="plain">{{e.event_id}} {{e.title}}</el-tag><span class="muted">置信度 {{c.confidence.toFixed(2)}}</span>
                <el-tag size="small" v-if="c.source!=='检查库'" type="success">{{c.source}}</el-tag></div>
              <div class="cause-f">{{c.finding}}</div>
            </div>
          </div>
          <el-collapse class="mini-collapse">
            <el-collapse-item :title="'已排除的干扰项（' + S.conclusion.excluded.length + '）'" name="ex">
              <div v-for="x in S.conclusion.excluded" :key="x.id" class="ex-item"><b>{{x.id}} {{x.title}}</b><div class="muted">{{x.finding}}</div></div></el-collapse-item>
            <el-collapse-item :title="'派生现象（' + S.conclusion.derived.length + '）'" name="de">
              <div v-for="x in S.conclusion.derived" :key="x.id" class="ex-item"><b>{{x.phenomenon}}</b> → 属于 <el-tag size="small">{{x.parent}} {{x.parent_title}}</el-tag><div class="muted">{{x.finding}}</div></div></el-collapse-item>
          </el-collapse>
        </template>
      </div>

      <!-- 拆解 -->
      <div v-show="wb.tab==='decomp'">
        <div v-if="S.decomp">
          <div class="sec-h">因子贡献瀑布图 <span class="muted">{{S.decomp.method}}｜贡献之和 − 总变动 = {{S.decomp.check ? S.decomp.check.diff.toExponential(1) : '—'}}（{{S.decomp.check && S.decomp.check.ok ? '守恒' : '—'}}）</span></div>
          <e-chart :option="wfOption" :height="260" />
          <el-table :data="S.decomp.factors" size="small" row-key="key" :tree-props="{children:'children'}" default-expand-all>
            <el-table-column prop="name" label="因子" min-width="110" />
            <el-table-column label="基期"><template #default="{row}">{{fmtVal(row.key, row.v0, eco)}}</template></el-table-column>
            <el-table-column label="现期"><template #default="{row}">{{fmtVal(row.key, row.v1, eco)}}</template></el-table-column>
            <el-table-column label="贡献"><template #default="{row}"><span :class="row.contrib<0?'neg':'pos'">{{fmtEff(row.contrib, eco)}}</span></template></el-table-column>
            <el-table-column label="贡献率"><template #default="{row}">{{(row.share*100).toFixed(1)}}%</template></el-table-column>
          </el-table>
          <div class="sec-h">指标拆解树（杜邦式） <span class="muted">点击节点查看该子指标的下一层拆解</span></div>
          <e-chart :option="treeOption" :height="230" @click="clickTree" />
          <div v-if="wb.sub"><div class="sec-h">{{wb.sub.name}} 的拆解 <span class="muted">{{wb.sub.method}}</span><el-link @click="wb.sub=null" style="margin-left:8px">关闭</el-link></div><e-chart :option="subOption" :height="220" /></div>
        </div>
      </div>

      <!-- 下钻 -->
      <div v-show="wb.tab==='drill'">
        <template v-if="focus">
          <div class="sec-h">追问下钻 <el-radio-group v-model="wb.focusIdx" size="small" style="margin-left:8px">
            <el-radio-button v-for="(f,i) in S.drill_history" :key="i" :value="i">{{f.name}}</el-radio-button></el-radio-group></div>
          <el-table :data="focus.metrics" size="small"><el-table-column prop="name" label="指标" />
            <el-table-column label="基期"><template #default="{row}">{{fmtVal(row.key, row.v0, eco)}}</template></el-table-column>
            <el-table-column label="现期"><template #default="{row}">{{fmtVal(row.key, row.v1, eco)}}</template></el-table-column>
            <el-table-column label="变化"><template #default="{row}"><span :class="(row.change_pp ?? row.change_rate) < 0 ? 'neg':'pos'">{{row.change_pp!=null ? spp(row.change_pp) : spct(row.change_rate)}}</span></template></el-table-column></el-table>
          <ul class="points"><li v-for="p in focus.points" :key="p">{{p}}</li></ul>
          <e-chart :option="focusOption" :height="220" />
          <div v-for="b in focus.breakdowns" :key="b.by"><div class="sec-h">{{focus.name}} · 按{{b.by_name}}</div>
            <el-table :data="b.rows" size="small" max-height="220">
              <el-table-column prop="k" :label="b.by_name" min-width="120" show-overflow-tooltip />
              <template v-if="eco"><el-table-column label="基期"><template #default="{row}">{{wan(row.v0)}}</template></el-table-column><el-table-column label="现期"><template #default="{row}">{{wan(row.v1)}}</template></el-table-column>
                <el-table-column label="差额" sortable :sort-by="r=>r.change"><template #default="{row}"><span :class="row.change<0?'neg':'pos'">{{swan(row.change)}}</span></template></el-table-column>
                <el-table-column label="占切片变动"><template #default="{row}">{{(row.contrib_share*100).toFixed(0)}}%</template></el-table-column></template>
              <template v-else><el-table-column label="营收"><template #default="{row}">{{wan(row.rev0)}} → {{wan(row.rev1)}}</template></el-table-column>
                <el-table-column label="折扣率变化"><template #default="{row}">{{spp(row.disc_change_pp)}}</template></el-table-column>
                <el-table-column label="毛利率变化" sortable :sort-by="r=>r.gm_change_pp"><template #default="{row}"><span :class="row.gm_change_pp<0?'neg':'pos'">{{spp(row.gm_change_pp)}}</span></template></el-table-column></template>
            </el-table></div>
          <div v-if="focus.related.length" class="sec-h">相关假设</div>
          <div v-for="r in focus.related" :key="r.id" class="ex-item"><el-tag size="small" :type="VERDICT_TAG[r.verdict]">{{r.verdict}}</el-tag> <b>{{r.id}} {{r.title}}</b><div class="muted">{{r.finding}}</div></div>
          <el-divider />
        </template>
        <div class="sec-h">维度贡献 <el-radio-group v-model="drillDim" size="small" style="margin-left:8px"><el-radio-button v-for="(d,i) in S.drills" :key="i" :value="i">{{d.dim_name}}</el-radio-button></el-radio-group></div>
        <div class="muted small" v-if="S.drills && S.drills[drillDim]">{{S.drills[drillDim].method}}</div>
        <e-chart :option="drillOption" :height="240" />
        <template v-if="S.cross"><div class="sec-h">交叉下钻 TOP 组合（{{S.cross.dims.join(' × ')}}）</div>
          <el-table :data="S.cross.rows" size="small"><el-table-column prop="d1" :label="S.cross.dims[0]" /><el-table-column prop="d2" :label="S.cross.dims[1]" />
            <el-table-column label="差额"><template #default="{row}"><span class="neg">{{swan(row.change)}}</span></template></el-table-column>
            <el-table-column label="贡献率"><template #default="{row}">{{(row.contrib_share*100).toFixed(1)}}%</template></el-table-column></el-table></template>
        <div class="sec-h">多维透视表
          <el-select v-model="wb.pivot.d1" size="small" style="width:100px;margin-left:8px" placeholder="行维度"><el-option v-for="(v,k) in dimOpts" :key="k" :label="v" :value="k" /></el-select>
          <el-select v-model="wb.pivot.d2" size="small" style="width:100px;margin-left:4px" placeholder="子维度" clearable><el-option v-for="(v,k) in dimOpts" :key="k" :label="v" :value="k" /></el-select>
          <el-button size="small" @click="loadPivot" style="margin-left:4px">生成</el-button><el-button size="small" @click="exportPivot" :disabled="!wb.pivot.data">导出 CSV</el-button></div>
        <el-table v-if="wb.pivot.data" :data="wb.pivot.data.rows" size="small" row-key="id" :tree-props="{children:'children'}" :cell-style="({row,column})=>heat(row,column)" max-height="360" border>
          <el-table-column prop="key" :label="wb.pivot.data.d1_name + (wb.pivot.data.d2_name ? ' / ' + wb.pivot.data.d2_name : '')" min-width="150" show-overflow-tooltip />
          <el-table-column prop="v0" :label="'基期' + wb.pivot.data.measure" sortable><template #default="{row}">{{wan(row.v0)}}</template></el-table-column>
          <el-table-column prop="v1" :label="'现期' + wb.pivot.data.measure" sortable><template #default="{row}">{{wan(row.v1)}}</template></el-table-column>
          <el-table-column prop="change" label="差额" sortable><template #default="{row}">{{swan(row.change)}}</template></el-table-column>
          <el-table-column prop="contrib_share" label="贡献率" sortable><template #default="{row}">{{(row.contrib_share*100).toFixed(1)}}%</template></el-table-column>
          <el-table-column v-if="wb.pivot.data.has_margin" prop="gm_change_pp" label="毛利率变动" sortable><template #default="{row}">{{spp(row.gm_change_pp)}}</template></el-table-column>
        </el-table>
        <div class="sec-h">结构桑基图</div>
        <e-chart :option="sankeyOption" :height="320" />
      </div>

      <!-- 假设 -->
      <div v-show="wb.tab==='hyp'">
        <el-radio-group v-model="wb.hypFilter" size="small" class="hyp-filter">
          <el-radio-button v-for="f in ['全部','支持','排除','派生','归因假设','事件检验','派生现象检验','大模型补充假设'].concat(wb.evidence.length?['追问相关']:[])" :key="f" :value="f">{{f}}</el-radio-button></el-radio-group>
        <div v-for="h in hyps" :key="h.id" class="hyp">
          <div class="hyp-top"><span class="cid">{{h.id}}</span><el-tag size="small" type="info">{{h.group}}</el-tag><b class="hyp-title">{{h.title}}</b>
            <el-tag v-if="h.result" size="small" effect="dark" :type="VERDICT_TAG[h.result.verdict]">{{h.result.verdict}}</el-tag>
            <el-tag v-else-if="h.error" size="small" type="danger">执行失败</el-tag><span v-else class="muted">执行中…</span>
            <el-tag v-if="h.accepted===true" size="small" type="success">已采纳</el-tag><el-tag v-if="h.accepted===false" size="small" type="danger">已驳回</el-tag>
            <el-tag v-if="h.source!=='检查库'" size="small" type="success" effect="plain">{{h.source}}</el-tag>
            <el-tag v-if="h.source==='大模型' && h.accepted==null && h.result && h.result.verdict==='支持'" size="small" type="warning" effect="plain">待采纳，未计入贡献</el-tag></div>
          <div class="muted small">切片：{{h.slice}}｜信号：{{h.signal}}</div>
          <div v-if="h.overlap" class="notice small">与检查库已支持的 {{h.overlap}} 可能是同一原因，采纳前请确认，避免重复计入</div>
          <template v-if="h.result">
            <div class="hyp-q"><span>置信度</span><el-progress :percentage="Math.round(h.result.confidence*100)" :stroke-width="8" style="width:140px" :color="h.result.confidence>=0.6?'#c8553d':'#9aa3b2'" />
              <span>影响 <b :class="h.result.effect<0?'neg':'pos'">{{fmtEff(h.result.effect, eco)}}</b></span><span>p = {{h.result.p_value==null?'—':(h.result.p_value<0.001?h.result.p_value.toExponential(1):h.result.p_value.toFixed(3))}}</span><span class="muted">{{h.elapsed_ms}} ms</span></div>
            <div class="hyp-f">{{h.result.finding}}</div>
            <ul class="ev-list"><li v-for="e in h.result.evidence" :key="e">{{e}}</li></ul>
            <e-chart v-if="miniOption(h)" :option="miniOption(h)" :height="110" />
          </template>
          <div v-if="h.error" class="err small">{{h.error}}</div>
          <div v-if="h.repaired" class="muted small">首次执行报错（{{(h.first_error||'').slice(0,120)}}），已把报错交给大模型修正后重新执行</div>
          <div class="hyp-ev" v-if="h.events.length">关联事件：<el-tag v-for="e in h.events" :key="e.event_id" size="small" type="warning" effect="plain">{{e.event_id}} {{e.title}}（相关度 {{e.relevance}}{{e.external?'，外部':''}}）</el-tag></div>
          <div class="hyp-ops"><el-link @click="openCode(h)">查看检验代码</el-link>
            <template v-if="!ro"><el-link type="success" @click="review(h,true)">采纳</el-link><el-link type="danger" @click="review(h,false)">驳回</el-link><el-link v-if="h.accepted!=null" @click="review(h,null)">恢复默认</el-link></template></div>
        </div>
      </div>

      <!-- 证据链 -->
      <div v-show="wb.tab==='chain'">
        <div v-if="S.conclusion">
          <div class="sec-h">因果解释链 <span class="muted">事件 → 机制 → 指标因子 → 影响</span></div>
          <div v-for="x in S.conclusion.chain" :key="x.cause" class="chain">
            <div class="chain-node ev" :class="{ext: x.external}"><div class="chain-k">{{x.external ? '外部诱因' : '业务事件'}}</div>{{x.event}}</div><div class="chain-arrow">→</div>
            <div class="chain-node"><div class="chain-k">机制</div>{{x.mechanism}}</div><div class="chain-arrow">→</div>
            <div class="chain-node"><div class="chain-k">影响因子</div>{{x.factor}}</div><div class="chain-arrow">→</div>
            <div class="chain-node eff"><div class="chain-k">{{x.rank}}｜置信度 {{x.confidence.toFixed(2)}}</div><b>{{x.title}}</b><br/>{{x.effect}}</div>
          </div>
          <div class="sec-h">指标走势与事件</div>
          <e-chart :option="chainOption" :height="250" />
          <div class="sec-h">分析窗口内的业务事件 <span class="muted">（异动前后 {{(store.system||{}).event_window_days||3}} 日内、类型与切片相容才关联）</span></div>
          <el-table :data="S.event_table" size="small">
            <el-table-column label="事件" min-width="150"><template #default="{row}">{{row.event_id}} {{row.title}} <el-tag v-if="row.source==='补录'" size="small">补录</el-tag></template></el-table-column>
            <el-table-column prop="event_type" label="类型" width="80" />
            <el-table-column label="日期" width="150"><template #default="{row}">{{row.start}}{{row.end!==row.start?' 至 '+row.end.slice(5):''}}</template></el-table-column>
            <el-table-column label="状态" min-width="120"><template #default="{row}"><el-tag size="small" :type="row.status.startsWith('已关联')?'danger':row.status.startsWith('已检验')?'info':'warning'">{{row.status}}</el-tag></template></el-table-column>
            <el-table-column label="假设" width="60"><template #default="{row}">{{row.hypothesis||'—'}}</template></el-table-column>
            <el-table-column label="相关度" width="70"><template #default="{row}">{{row.relevance==null?'—':row.relevance}}</template></el-table-column>
          </el-table>
        </div>
      </div>

      <!-- What-If -->
      <div v-show="wb.tab==='whatif'">
        <template v-if="S.whatif && wr">
          <div class="wi-res"><div><div class="muted small">{{S.whatif.note}}</div>
            <div class="wi-big">{{fw(wr.predicted)}} <span :class="wr.delta<0?'neg':'pos'">{{fmtEff(wr.delta, eco)}}</span></div>
            <div class="muted small">95% 区间 {{fw(wr.band[0])}} ~ {{fw(wr.band[1])}}（参数 ±30% 与历史波动）</div></div>
            <div class="wi-btns" v-if="!ro"><el-button size="small" @click="resetLevers">重置</el-button><el-button size="small" type="primary" plain @click="saveScenario">保存为情景</el-button></div></div>
          <div class="presets" v-if="!ro"><span class="muted small">预置情景：</span><el-tag v-for="p in S.whatif.presets" :key="p.name" class="sugg-tag" effect="plain" @click="applyPreset(p)">{{p.name}}</el-tag></div>
          <div v-for="l in S.whatif.levers" :key="l.key" class="lever">
            <div class="lever-h"><b>{{l.name}}</b><span class="lever-v">{{levelLabel(l)}}</span><el-tag v-if="l.cause" size="small" type="danger" effect="plain">对应 {{l.cause}}</el-tag><span class="muted small">{{l.desc}}</span></div>
            <el-slider v-model="wvals[l.key]" :min="l.min" :max="l.max" :step="l.step" :disabled="ro" @input="simulate" :marks="{[l.default]: '现状'}" />
          </div>
          <div class="sec-h">各杠杆贡献</div><e-chart :option="contribOption" :height="Math.max(120, (wr.contrib.length)*30)" />
          <div class="sec-h">敏感度曲线 <el-select v-model="sensKey" size="small" style="width:200px;margin-left:8px" placeholder="选择杠杆"><el-option v-for="(c,k) in wr.curves" :key="k" :label="c.name" :value="k" /></el-select></div>
          <e-chart :option="sensOption" :height="200" />
          <div class="sec-h">情景对比</div>
          <e-chart :option="scenOption" :height="230" />
          <el-table :data="scenRows" size="small"><el-table-column label="情景" min-width="160"><template #default="{row}">{{row.name}} <el-tag size="small" type="info">{{row.kind}}</el-tag></template></el-table-column>
            <el-table-column label="预测"><template #default="{row}">{{fw(row.predicted)}}</template></el-table-column>
            <el-table-column label="较基线"><template #default="{row}"><span :class="row.delta<0?'neg':'pos'">{{fmtEff(row.delta, eco)}}</span></template></el-table-column>
            <el-table-column label="95% 区间" min-width="150"><template #default="{row}">{{fw(row.band[0])}} ~ {{fw(row.band[1])}}</template></el-table-column>
            <el-table-column width="60"><template #default="{row}"><el-link v-if="row.kind==='已保存' && !ro" type="danger" @click="delScenario(row)">删除</el-link></template></el-table-column></el-table>
        </template>
      </div>

      <!-- 行动 -->
      <div v-show="wb.tab==='actions'">
        <div class="muted small">每条行动绑定一个量化驱动因子；预期收益 = 影响折算到月 × 可挽回比例。</div>
        <div v-for="a in S.actions" :key="a.cause" class="action">
          <div class="action-top"><el-tag :type="a.priority==='P0'?'danger':'warning'" effect="dark" size="small">{{a.priority}}</el-tag><b>{{a.owner}}</b><span class="muted">｜{{a.period}}</span><span class="action-amt">{{a.amount_text}}</span></div>
          <div>{{a.action}}</div>
          <div class="muted small">对应 {{a.cause}} {{a.cause_title}}｜驱动因子：{{a.driver}}｜{{a.basis}}</div>
        </div>
        <div class="muted" v-if="S.actions && !S.actions.length">没有可干预的原因（例如只有基期效应、口径因素）。</div>
      </div>

      <!-- 查数 -->
      <div v-show="wb.tab==='query'">
        <div class="q-row" v-if="can('analysis:run')"><el-input v-model="qf.q" size="small" placeholder="直接问数据，例如：9 月各渠道的订单数和客单价" @keyup.enter="runSql" />
          <el-select v-model="qf.mode" size="small" style="width:190px" placeholder="默认模型"><el-option v-for="m in sqlModes" :key="m.key" :label="m.name + (m.available?'':'（未配置）')" :value="m.key" :disabled="!m.available" /></el-select>
          <el-button size="small" type="primary" :loading="qf.busy" @click="runSql">查询</el-button></div>
        <div v-for="(r,i) in [...(S.queries||[])].reverse()" :key="i" class="qres">
          <div><b>{{r.question}}</b> <el-tag size="small" type="info">{{r.mode==='rule'?'规则模板（离线）':r.mode==='sft'?'微调模型':'通用大模型'}}</el-tag> <span class="muted small" v-if="r.elapsed_ms">{{r.elapsed_ms}} ms</span></div>
          <pre class="sql" v-if="r.sql">{{r.sql}}</pre>
          <div v-if="r.trace && r.trace.length>1" class="muted small">第一次执行报错已自动修正：{{r.trace[0].error}}</div>
          <el-table v-if="r.type==='sql'" :data="r.rows.map(x=>Object.fromEntries(r.columns.map((c,j)=>[c,x[j]])))" size="small" max-height="260" border>
            <el-table-column v-for="c in r.columns" :key="c" :prop="c" :label="c" min-width="100"><template #default="{row}">{{typeof row[c]==='number' ? (Math.abs(row[c])<1 ? row[c].toFixed(4) : row[c].toLocaleString('zh-CN',{maximumFractionDigits:2})) : row[c]}}</template></el-table-column></el-table>
          <div v-else class="notice">{{r.message || r.error}}</div>
          <el-link v-if="r.type==='sql'" @click="exportQuery(r)">导出 CSV（{{r.total_rows}} 行）</el-link>
        </div>
      </div>
    </div>

    <el-dialog v-model="wb.codeDlg.show" :title="wb.codeDlg.h ? wb.codeDlg.h.id + ' 检验代码（只读沙箱执行）' : ''" width="760px">
      <el-input v-model="wb.codeDlg.code" type="textarea" :rows="20" class="code" :readonly="ro" />
      <div v-if="wb.codeDlg.out && !wb.codeDlg.out.ok" class="err small">{{wb.codeDlg.out.error}}</div>
      <template #footer><span class="muted small" style="float:left">沙箱：只读连接、AST 白名单、超时 {{(store.system||{}).sandbox_timeout}} 秒、内存 {{(store.system||{}).sandbox_memory_mb}} MB</span>
        <el-button @click="wb.codeDlg.show=false">关闭</el-button><el-button v-if="!ro" type="primary" :loading="wb.codeDlg.running" @click="rerun">修改后重新执行</el-button></template>
    </el-dialog>
  </div>`
};

/* ================================================================== 业务事件日历 */
const EventsPage = {
  components: { EChart },
  setup() {
    const st = reactive({ domain: 'ecommerce', data: null, types: [], range: null, dlg: false, form: {}, editing: null });
    async function load() { try { st.data = await api.get('/api/events?domain=' + st.domain); } catch (e) { toast(e); } }
    onMounted(load); watch(() => st.domain, load);
    const list = computed(() => (st.data ? st.data.events : []).filter(e => (!st.types.length || st.types.includes(e.event_type)) &&
      (!st.range || (e.end_date >= st.range[0] && e.start_date <= st.range[1]))));
    const ganttOption = computed(() => {
      const L = list.value; if (!L.length) return null;
      const types = [...new Set(L.map(e => e.event_type))];
      const day = s => new Date(s + 'T00:00:00').getTime();
      return { grid: { left: 80, right: 20, top: 10, bottom: 40 }, tooltip: { formatter: p => `${p.data.e.event_id} ${p.data.e.title}<br/>${p.data.e.start_date} 至 ${p.data.e.end_date}<br/>${p.data.e.description || ''}` },
        dataZoom: [{ type: 'slider', height: 14, bottom: 6 }, { type: 'inside' }],
        xAxis: { type: 'time' }, yAxis: { type: 'category', data: types },
        series: [{ type: 'custom', renderItem: (params, a) => { const y = a.value(0); const s = a.coord([a.value(1), y]); const e = a.coord([a.value(2), y]); const h = a.size([0, 1])[1] * 0.5;
          return { type: 'group', children: [{ type: 'rect', shape: { x: s[0], y: s[1] - h / 2, width: Math.max(e[0] - s[0], 6), height: h }, style: { fill: PALETTE[params.dataIndex % PALETTE.length], opacity: .85 } },
            { type: 'text', style: { text: a.value(3), x: s[0] + 2, y: s[1] - h / 2 - 12, fontSize: 10, fill: '#333' } }] }; },
          encode: { x: [1, 2], y: 0 }, data: L.map(e => ({ value: [types.indexOf(e.event_type), day(e.start_date), day(e.end_date) + 86400000, e.event_id + ' ' + e.title], e })) }] };
    });
    function add() { st.editing = null; st.form = { domain: st.domain, start_date: '', end_date: '', event_type: '营销活动', title: '', description: '', channel_id: null, category_id: null, product_line: null }; st.dlg = true; }
    function edit(e) { st.editing = e.id; st.form = { domain: st.domain, ...e }; st.dlg = true; }
    async function save() {
      try { if (st.editing) await api.put('/api/events/' + st.editing, st.form); else await api.post('/api/events', st.form); st.dlg = false; ElMessage.success('已保存，后续归因会自动纳入事件匹配'); load(); } catch (e) { toast(e); }
    }
    async function del(e) { try { await ElMessageBox.confirm(`删除事件“${e.title}”？`, '确认', { type: 'warning' }); await api.del('/api/events/' + e.id); load(); } catch (x) { if (x !== 'cancel') toast(x); } }
    return { st, list, ganttOption, add, edit, save, del, can };
  },
  template: `
  <div class="page">
    <div class="page-head"><div><h2>业务事件日历</h2><div class="muted">营销节点、大促、投放调整、系统故障、竞品与政策变化。数仓事件只读，管理员可补录；归因时自动匹配异动窗口前后 3 日的事件。</div></div>
      <div><el-radio-group v-model="st.domain"><el-radio-button value="ecommerce">电商</el-radio-button><el-radio-button value="fmcg">快消</el-radio-button></el-radio-group>
        <el-button v-if="can('events:edit')" type="primary" style="margin-left:10px" @click="add">补录事件</el-button></div></div>
    <div class="filters" v-if="st.data"><el-select v-model="st.types" multiple collapse-tags placeholder="按事件类型筛选" style="width:260px" clearable><el-option v-for="t in st.data.types" :key="t" :label="t" :value="t" /></el-select>
      <el-date-picker v-model="st.range" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始" end-placeholder="结束" style="margin-left:8px" /></div>
    <el-card shadow="never"><template #header><b>时间线</b></template><e-chart :option="ganttOption" :height="300" /></el-card>
    <el-card shadow="never" style="margin-top:12px"><el-table :data="list" size="small">
      <el-table-column prop="event_id" label="编号" width="70" /><el-table-column prop="title" label="事件" min-width="160" />
      <el-table-column prop="event_type" label="类型" width="90" /><el-table-column label="日期" width="190"><template #default="{row}">{{row.start_date}} 至 {{row.end_date}}</template></el-table-column>
      <el-table-column label="影响范围" width="140"><template #default="{row}">{{[row.channel_id, row.category_id, row.product_line].filter(Boolean).join(' / ') || '全局'}}</template></el-table-column>
      <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
      <el-table-column label="来源" width="70"><template #default="{row}"><el-tag size="small" :type="row.source==='补录'?'warning':'info'">{{row.source}}</el-tag></template></el-table-column>
      <el-table-column width="110" v-if="can('events:edit')"><template #default="{row}"><template v-if="row.editable"><el-link @click="edit(row)">编辑</el-link> <el-link type="danger" @click="del(row)">删除</el-link></template></template></el-table-column>
    </el-table></el-card>
    <el-dialog v-model="st.dlg" :title="st.editing ? '编辑事件' : '补录事件'" width="560px">
      <el-form label-width="80px" v-if="st.data">
        <el-form-item label="标题"><el-input v-model="st.form.title" /></el-form-item>
        <el-form-item label="类型"><el-select v-model="st.form.event_type"><el-option v-for="t in st.data.types" :key="t" :label="t" :value="t" /></el-select></el-form-item>
        <el-form-item label="日期"><el-date-picker v-model="st.form.start_date" value-format="YYYY-MM-DD" placeholder="开始" style="width:48%" /> <el-date-picker v-model="st.form.end_date" value-format="YYYY-MM-DD" placeholder="结束" style="width:48%" /></el-form-item>
        <el-form-item label="渠道"><el-select v-model="st.form.channel_id" clearable placeholder="全渠道"><el-option v-for="c in st.data.dims.channels" :key="c.value" :label="c.label" :value="c.value" /></el-select></el-form-item>
        <el-form-item label="类目" v-if="st.data.dims.categories"><el-select v-model="st.form.category_id" clearable placeholder="全类目"><el-option v-for="c in st.data.dims.categories" :key="c.value" :label="c.label" :value="c.value" /></el-select></el-form-item>
        <el-form-item label="产品线" v-if="st.data.dims.product_lines"><el-select v-model="st.form.product_line" clearable placeholder="全产品线"><el-option v-for="c in st.data.dims.product_lines" :key="c.value" :label="c.label" :value="c.value" /></el-select></el-form-item>
        <el-form-item label="描述"><el-input v-model="st.form.description" type="textarea" :rows="3" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="st.dlg=false">取消</el-button><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>
  </div>`
};

/* ================================================================== 指标字典 */
const MetricsPage = {
  components: { EChart },
  setup() {
    const st = reactive({ domain: 'ecommerce', data: null, dlg: false, form: {}, editing: null });
    async function load() { try { st.data = await api.get('/api/metrics?domain=' + st.domain); } catch (e) { toast(e); } }
    onMounted(load); watch(() => st.domain, load);
    const treeOption = computed(() => { const t = st.data && st.data.tree; if (!t) return null;
      const map = n => ({ name: n.name + (n.formula ? '\n' + n.formula : ''), children: (n.children || []).map(map) });
      return { tooltip: {}, series: [{ type: 'tree', data: [map(t)], orient: 'TB', top: 30, bottom: 70, left: 20, right: 20, symbolSize: 9, expandAndCollapse: false,
        label: { fontSize: 11, position: 'top', lineHeight: 14 }, leaves: { label: { position: 'bottom', rotate: 0 } } }] }; });
    function add() { st.editing = null; st.form = { domain: st.domain, code: '', name: '', kind: '派生', aliases: '', formula: '', decomposition: '', parent: null, model: '', description: '' }; st.dlg = true; }
    function edit(m) { st.editing = m.id; st.form = { ...m }; st.dlg = true; }
    async function save() { try { if (st.editing) await api.put('/api/metrics/' + st.editing, st.form); else await api.post('/api/metrics', st.form); st.dlg = false; load(); } catch (e) { toast(e); } }
    return { st, treeOption, add, edit, save, can };
  },
  template: `
  <div class="page">
    <div class="page-head"><div><h2>指标字典</h2><div class="muted">原子指标、派生指标、复合指标的口径与默认拆解层级。拆解树由公式引擎执行（乘法模型用 LMDI，加法模型用差额分析）。</div></div>
      <div><el-radio-group v-model="st.domain"><el-radio-button value="ecommerce">电商</el-radio-button><el-radio-button value="fmcg">快消</el-radio-button></el-radio-group>
        <el-button v-if="can('metrics:edit')" type="primary" style="margin-left:10px" @click="add">新增指标</el-button></div></div>
    <el-card shadow="never" v-if="st.data"><template #header><b>默认拆解树：{{st.data.tree.name}}</b><span class="muted">（{{st.data.tree.method}}；{{st.data.tree.formula}}）</span></template><e-chart :option="treeOption" :height="300" /></el-card>
    <el-card shadow="never" style="margin-top:12px" v-if="st.data"><el-table :data="st.data.metrics" size="small">
      <el-table-column prop="code" label="编码" width="130" /><el-table-column prop="name" label="名称" width="110" />
      <el-table-column label="类型" width="70"><template #default="{row}"><el-tag size="small" :type="row.kind==='复合'?'danger':row.kind==='原子'?'':'info'">{{row.kind}}</el-tag></template></el-table-column>
      <el-table-column prop="formula" label="口径 / 公式" min-width="220" show-overflow-tooltip /><el-table-column prop="decomposition" label="拆解" min-width="180" show-overflow-tooltip />
      <el-table-column prop="model" label="拆解模型" width="110" /><el-table-column prop="parent" label="上级" width="90" /><el-table-column prop="aliases" label="别名" width="150" show-overflow-tooltip />
      <el-table-column width="60" v-if="can('metrics:edit')"><template #default="{row}"><el-link @click="edit(row)">编辑</el-link></template></el-table-column></el-table></el-card>
    <el-dialog v-model="st.dlg" :title="st.editing ? '编辑指标' : '新增指标'" width="560px"><el-form label-width="80px">
      <el-form-item label="编码"><el-input v-model="st.form.code" :disabled="!!st.editing" /></el-form-item><el-form-item label="名称"><el-input v-model="st.form.name" /></el-form-item>
      <el-form-item label="类型"><el-radio-group v-model="st.form.kind"><el-radio value="原子">原子</el-radio><el-radio value="派生">派生</el-radio><el-radio value="复合">复合</el-radio></el-radio-group></el-form-item>
      <el-form-item label="口径"><el-input v-model="st.form.formula" type="textarea" :rows="2" /></el-form-item><el-form-item label="拆解公式"><el-input v-model="st.form.decomposition" /></el-form-item>
      <el-form-item label="拆解模型"><el-select v-model="st.form.model" clearable><el-option label="乘法（LMDI）" value="乘法（LMDI）" /><el-option label="加法（差额）" value="加法（差额）" /><el-option label="Shapley" value="Shapley" /></el-select></el-form-item>
      <el-form-item label="上级指标"><el-input v-model="st.form.parent" /></el-form-item><el-form-item label="别名"><el-input v-model="st.form.aliases" placeholder="逗号分隔" /></el-form-item>
      <el-form-item label="说明"><el-input v-model="st.form.description" type="textarea" :rows="2" /></el-form-item></el-form>
      <template #footer><el-button @click="st.dlg=false">取消</el-button><el-button type="primary" @click="save">保存</el-button></template></el-dialog>
  </div>`
};

/* ================================================================== 报告归档与预览 */
const ReportsPage = {
  components: { EChart },
  setup() {
    const st = reactive({ list: [], cur: null, edit: false, notes: {}, draft: {} });
    async function load() { try { st.list = await api.get('/api/reports'); if (store.reportId) { open(store.reportId); store.reportId = null; } } catch (e) { toast(e); } }
    async function open(id) { try { st.cur = await api.get('/api/reports/' + id); st.edit = false; } catch (e) { toast(e); } }
    onMounted(load);
    const notesOf = k => (st.cur.annotations || []).filter(a => a.section === k);
    async function addNote(k) { const t = (st.notes[k] || '').trim(); if (!t) return;
      try { const r = await api.post(`/api/reports/${st.cur.id}/annotations`, { section: k, text: t }); st.cur.annotations = r.annotations; st.notes[k] = ''; } catch (e) { toast(e); } }
    function startEdit() { st.draft = { title: st.cur.title, summary: st.cur.summary, texts: Object.fromEntries(st.cur.sections.map(s => [s.key, s.text])) }; st.edit = true; }
    async function saveEdit() { try { st.cur = await api.put('/api/reports/' + st.cur.id, { title: st.draft.title, summary: st.draft.summary, section_texts: st.draft.texts }); st.edit = false; load(); } catch (e) { toast(e); } }
    async function setStatus(s) { try { st.cur = await api.put('/api/reports/' + st.cur.id, { status: s }); load(); ElMessage.success(s === '已发布' ? '已发布，管理层可在报告列表和简报中查看' : '已撤回为草稿'); } catch (e) { toast(e); } }
    async function exp(fmt) {
      try { const res = await fetch(`/api/reports/${st.cur.id}/export?fmt=${fmt}`, { headers: { Authorization: 'Bearer ' + store.token } });
        if (!res.ok) throw new Error((await res.json()).detail); const b = await res.blob(); const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = st.cur.title + '.' + fmt; a.click(); } catch (e) { toast(e); }
    }
    function back() { if (st.cur.session_id) { store.page = 'workbench'; openSession(st.cur.session_id); } }
    const wf = s => s.chart && s.chart.type === 'waterfall' ? waterfallOption({ v0: s.chart.start, v1: s.chart.end, factors: s.chart.items.map(([n, v]) => ({ name: n, contrib: v, share: 0 })) }, s.chart.eco) : null;
    return { st, open, notesOf, addNote, startEdit, saveEdit, setStatus, exp, back, wf, can, md, print: () => window.print() };
  },
  template: `
  <div class="page">
    <div v-if="!st.cur">
      <div class="page-head"><div><h2>报告归档</h2><div class="muted">{{can('report:edit') ? '全部报告（草稿与已发布）' : '已发布的归因报告'}}</div></div></div>
      <el-card shadow="never"><el-table :data="st.list" size="small" @row-click="r=>open(r.id)" style="cursor:pointer">
        <el-table-column prop="title" label="标题" min-width="220" /><el-table-column label="指标域" width="90"><template #default="{row}">{{row.domain==='ecommerce'?'电商':'快消'}}</template></el-table-column>
        <el-table-column label="状态" width="80"><template #default="{row}"><el-tag size="small" :type="row.status==='已发布'?'success':'info'">{{row.status}}</el-tag></template></el-table-column>
        <el-table-column prop="author" label="作者" width="140" /><el-table-column prop="updated_at" label="更新时间" width="160" /></el-table>
        <div class="muted" v-if="!st.list.length" style="padding:20px">还没有报告。在归因工作台完成分析后点“生成报告”。</div></el-card>
    </div>
    <div v-else class="report">
      <div class="report-bar noprint">
        <el-button size="small" @click="st.cur=null">← 返回列表</el-button>
        <el-button size="small" v-if="can('report:edit') && st.cur.session_id" @click="back">打开会话</el-button>
        <span class="spacer"></span>
        <template v-if="can('report:edit')"><el-button size="small" v-if="!st.edit" @click="startEdit">编辑</el-button><el-button size="small" v-else type="primary" @click="saveEdit">保存</el-button>
          <el-button size="small" :type="st.cur.status==='已发布'?'warning':'success'" @click="setStatus(st.cur.status==='已发布'?'草稿':'已发布')">{{st.cur.status==='已发布'?'撤回':'发布'}}</el-button></template>
        <template v-if="can('report:export')"><el-button size="small" @click="exp('md')">Markdown</el-button><el-button size="small" @click="exp('pdf')">PDF</el-button><el-button size="small" @click="exp('xlsx')">Excel</el-button></template>
        <el-button size="small" @click="print">打印</el-button>
      </div>
      <div class="paper">
        <h1 v-if="!st.edit">{{st.cur.title}}</h1><el-input v-else v-model="st.draft.title" size="large" />
        <div class="muted">生成于 {{st.cur.created}}｜{{st.cur.author}}｜<el-tag size="small" :type="st.cur.status==='已发布'?'success':'info'">{{st.cur.status}}</el-tag></div>
        <div class="summary" v-if="!st.edit"><b>摘要</b>　{{st.cur.summary}}</div><el-input v-else v-model="st.draft.summary" type="textarea" :rows="3" />
        <div v-for="s in st.cur.sections" :key="s.key" class="rsec">
          <h3>{{s.title}}</h3>
          <div v-if="!st.edit" class="md" v-html="md(s.text)"></div><el-input v-else v-model="st.draft.texts[s.key]" type="textarea" :autosize="{minRows:2,maxRows:12}" />
          <e-chart v-if="wf(s)" :option="wf(s)" :height="240" />
          <div v-for="t in s.tables" :key="t.name" v-show="t.rows.length"><div class="tname">{{t.name}}</div>
            <table class="rtable"><thead><tr><th v-for="h in t.header" :key="h">{{h}}</th></tr></thead><tbody><tr v-for="(r,i) in t.rows" :key="i"><td v-for="(c,j) in r" :key="j">{{c}}</td></tr></tbody></table></div>
          <div v-for="a in notesOf(s.key)" :key="a.created+a.text" class="note">批注｜{{a.author}}｜{{a.created}}：{{a.text}}</div>
          <div class="note-in noprint"><el-input v-model="st.notes[s.key]" size="small" placeholder="添加批注（协同审阅）" @keyup.enter="addNote(s.key)" /><el-button size="small" @click="addNote(s.key)">批注</el-button></div>
        </div>
      </div>
    </div>
  </div>`
};

/* ================================================================== 归因简报（管理层） */
const BriefsPage = {
  setup() {
    const st = reactive({ subs: [], briefs: [], loading: false });
    async function load() { st.loading = true; try { st.subs = await api.get('/api/subscriptions'); st.briefs = await api.get('/api/briefs'); } catch (e) { toast(e); } finally { st.loading = false; } }
    onMounted(load);
    const subbed = d => st.subs.find(s => s.domain === d);
    async function toggle(d, f) { try { if (subbed(d) && !f) await api.del('/api/subscriptions/' + d); else await api.post('/api/subscriptions', { domain: d, frequency: f || '每周' }); load(); } catch (e) { toast(e); } }
    function openReport(id) { store.reportId = id; store.page = 'reports'; }
    return { st, subbed, toggle, openReport, pct, spct, spp, wan };
  },
  template: `
  <div class="page" v-loading="st.loading">
    <div class="page-head"><div><h2>归因简报</h2><div class="muted">订阅后按周期汇总：核心指标、异动清单、最近一份已发布报告的结论与行动项。</div></div></div>
    <el-card shadow="never"><template #header><b>我的订阅</b></template>
      <div v-for="d in [['ecommerce','青禾优选电商'],['fmcg','禾悦食品快消']]" :key="d[0]" class="sub-row"><b>{{d[1]}}</b>
        <el-radio-group :model-value="subbed(d[0]) ? subbed(d[0]).frequency : ''" size="small" @change="f=>toggle(d[0], f)"><el-radio-button value="每日">每日</el-radio-button><el-radio-button value="每周">每周</el-radio-button><el-radio-button value="每月">每月</el-radio-button></el-radio-group>
        <el-button v-if="subbed(d[0])" size="small" link type="danger" @click="toggle(d[0], null)">取消订阅</el-button></div></el-card>
    <el-card v-for="b in st.briefs" :key="b.domain" shadow="never" style="margin-top:12px"><template #header><b>{{b.title}} 简报</b><span class="muted">（{{b.frequency}}）</span></template>
      <div class="brief-kpis"><div v-for="c in b.cards" :key="c.name" class="bk" :class="{flag:c.flag}"><div class="muted small">{{c.name}}</div>
        <b>{{c.unit==='%' ? pct(c.value) : c.key==='aov' ? c.value.toFixed(1) + ' 元' : c.unit==='元' ? wan(c.value) + '元' : c.value.toLocaleString()}}</b><div :class="c.mom<0?'neg':'pos'" class="small">{{c.ratio ? spp(c.mom) : spct(c.mom)}}</div></div></div>
      <div class="sec-h">异动</div><div v-for="a in b.anomalies" :key="a.text" class="anom small">· {{a.text}}</div>
      <template v-if="b.report"><div class="sec-h">最近报告：<el-link type="primary" @click="openReport(b.report.id)">{{b.report.title}}</el-link><span class="muted small">（{{b.report.published_at}} 发布）</span></div>
        <div class="small">{{b.report.summary}}</div>
        <div class="sec-h">行动项</div><div v-for="a in b.actions" :key="a[2]" class="small">· [{{a[0]}}] {{a[1]}}：{{a[2]}}（{{a[3]}}，{{a[4]}}）</div></template>
      <div v-else class="muted small">该指标域还没有已发布的报告</div>
    </el-card>
  </div>`
};

/* ================================================================== 系统设置 */
const SettingsPage = {
  setup() {
    const st = reactive({ sys: null, form: {}, audit: [], runs: [], tab: 'model' });
    async function load() {
      try { st.sys = await api.get('/api/system'); store.system = st.sys;
        st.form = { sql_mode: st.sys.sql_mode, confidence_threshold: st.sys.confidence_threshold, sandbox_timeout: st.sys.sandbox_timeout, sandbox_memory_mb: st.sys.sandbox_memory_mb };
        if (can('audit:view')) { st.audit = await api.get('/api/audit'); st.runs = await api.get('/api/runlogs'); } } catch (e) { toast(e); }
    }
    onMounted(load);
    async function save() {
      const body = can('settings:edit') ? st.form : { sql_mode: st.form.sql_mode };
      try { st.sys = await api.put('/api/settings', body); store.system = st.sys; ElMessage.success('已保存'); } catch (e) { toast(e); }
    }
    return { st, save, can };
  },
  template: `
  <div class="page" v-if="st.sys">
    <div class="page-head"><div><h2>系统设置</h2><div class="muted">模型调用、SQL 模型切换、判定阈值与沙箱配额；操作审计与运行日志。</div></div></div>
    <el-tabs v-model="st.tab">
      <el-tab-pane label="模型与阈值" name="model">
        <el-card shadow="never"><el-descriptions :column="1" border size="small">
          <el-descriptions-item label="分析大模型">{{st.sys.llm_model}}<el-tag size="small" :type="st.sys.llm_online?'success':'info'" style="margin-left:8px">{{st.sys.llm_online?'在线':'离线'}}</el-tag><span class="muted small" style="margin-left:6px">{{st.sys.llm_base_url}}</span></el-descriptions-item>
          <el-descriptions-item label="自然语言查数模型"><el-radio-group v-model="st.form.sql_mode"><el-radio v-for="m in st.sys.sql_modes" :key="m.key" :value="m.key" :disabled="!m.available">{{m.name}}{{m.available?'':'（未配置）'}}</el-radio></el-radio-group>
            <div class="muted small">微调模型：配套项目 Text2SQL-Qwen-SFT 用 QLoRA 微调的 Qwen3.5-9B，用 vLLM 部署后把地址填到 .env 的 SQL_LLM_BASE_URL。两者共用同一份提示词。{{st.sys.sql_llm_base_url ? '当前地址：' + st.sys.sql_llm_base_url : ''}}</div></el-descriptions-item>
          <el-descriptions-item label="置信度阈值"><el-input-number v-model="st.form.confidence_threshold" :min="0.1" :max="0.95" :step="0.05" :disabled="!can('settings:edit')" size="small" /><span class="muted small">　低于阈值的假设自动排除</span></el-descriptions-item>
          <el-descriptions-item label="主要 / 次要原因">影响占总变动 ≥ {{(st.sys.major_share*100).toFixed(0)}}% 为主要，≥ {{(st.sys.minor_share*100).toFixed(0)}}% 为次要（环境变量 MAJOR_SHARE / MINOR_SHARE）</el-descriptions-item>
          <el-descriptions-item label="事件匹配窗口">异动前后 {{st.sys.event_window_days}} 日</el-descriptions-item>
          <el-descriptions-item label="沙箱配额">超时 <el-input-number v-model="st.form.sandbox_timeout" :min="2" :max="120" size="small" :disabled="!can('settings:edit')" /> 秒　内存 <el-input-number v-model="st.form.sandbox_memory_mb" :min="256" :max="8192" :step="256" size="small" :disabled="!can('settings:edit')" /> MB<div class="muted small">只读连接、AST 白名单（禁止 import 非白名单模块、文件与网络、eval/exec、双下划线属性）、独立子进程、超时强制结束</div></el-descriptions-item>
          <el-descriptions-item label="数据源">{{st.sys.warehouse}}（{{st.sys.domains.map(d=>d.title + ' ' + d.periods[0] + '~' + d.periods[d.periods.length-1]).join('；')}}）</el-descriptions-item>
          <el-descriptions-item label="大模型用量">{{st.sys.llm_usage.calls}} 次调用，输入 {{st.sys.llm_usage.prompt_tokens}} / 输出 {{st.sys.llm_usage.completion_tokens}} tokens（本次启动以来）</el-descriptions-item>
        </el-descriptions><el-button type="primary" style="margin-top:12px" @click="save">保存</el-button></el-card>
      </el-tab-pane>
      <el-tab-pane label="Agent 编排图" name="graph"><el-card shadow="never"><div class="muted small">LangGraph 状态图（Mermaid 源码，可粘贴到 mermaid.live 渲染）</div><pre class="sql">{{st.sys.graph}}</pre></el-card></el-tab-pane>
      <el-tab-pane label="运行日志" name="runs" v-if="can('audit:view')"><el-table :data="st.runs" size="small">
        <el-table-column type="expand"><template #default="{row}"><div v-for="s in row.steps" :key="s.key" class="small">{{s.name}}｜{{s.ms}} ms｜{{s.summary}}</div></template></el-table-column>
        <el-table-column prop="created_at" label="时间" width="160" /><el-table-column prop="user_name" label="用户" width="140" /><el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
        <el-table-column prop="route" label="分支" width="80" /><el-table-column prop="status" label="状态" width="60" /><el-table-column prop="ms" label="耗时 ms" width="80" />
        <el-table-column prop="llm_calls" label="模型调用" width="80" /><el-table-column prop="error" label="错误" show-overflow-tooltip /></el-table></el-tab-pane>
      <el-tab-pane label="操作审计" name="audit" v-if="can('audit:view')"><el-table :data="st.audit" size="small">
        <el-table-column prop="created_at" label="时间" width="160" /><el-table-column prop="user_name" label="用户" width="150" /><el-table-column prop="action" label="操作" width="110" />
        <el-table-column prop="target" label="对象" width="140" /><el-table-column prop="detail" label="详情" show-overflow-tooltip /></el-table></el-tab-pane>
    </el-tabs>
  </div>`
};

/* ================================================================== 外壳 */
const NAV = [['dashboard', '指标异动大盘', ['dash:view']], ['workbench', '归因工作台', ['analysis:run', 'analysis:view']], ['events', '业务事件日历', ['events:view']],
  ['metrics', '指标字典', ['metrics:view']], ['reports', '报告归档', ['report:view', 'report:edit']], ['briefs', '归因简报', ['brief:subscribe']], ['settings', '系统设置', ['settings:edit', 'audit:view', 'analysis:run']]];

const App = {
  components: { LoginPage, DashboardPage, WorkbenchPage, EventsPage, MetricsPage, ReportsPage, BriefsPage, SettingsPage },
  setup() {
    onMounted(async () => { if (store.token) { try { store.user = await api.get('/api/me'); await loadSystem(); } catch (e) { logout(); } } });
    const nav = computed(() => NAV.filter(n => n[2].some(can)));
    const comp = computed(() => ({ dashboard: 'DashboardPage', workbench: 'WorkbenchPage', events: 'EventsPage', metrics: 'MetricsPage', reports: 'ReportsPage', briefs: 'BriefsPage', settings: 'SettingsPage' })[store.page]);
    return { store, nav, comp, logout };
  },
  template: `
  <login-page v-if="!store.user" />
  <div v-else class="shell">
    <header class="top"><div class="brand"><svg viewBox="0 0 32 32" width="24" height="24"><rect width="32" height="32" rx="7" fill="#f2a541"/><path d="M6 22 L12 14 L17 18 L25 8" stroke="#14213d" stroke-width="3" fill="none"/></svg>经营归因分析系统</div>
      <nav><a v-for="n in nav" :key="n[0]" :class="{on: store.page===n[0]}" @click="store.page=n[0]">{{n[1]}}</a></nav>
      <div class="me"><el-tag size="small" :type="store.system && store.system.llm_online ? 'success' : 'info'" effect="dark">{{store.system && store.system.llm_online ? '大模型在线' : '离线规则模式'}}</el-tag>
        <span>{{store.user.name}}</span><el-link @click="logout" style="color:#cbd5e1">退出</el-link></div></header>
    <main class="main"><component :is="comp" /></main>
  </div>`
};

const app = createApp(App);
app.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
app.component('BoardPanel', BoardPanel);
app.mount('#app');
