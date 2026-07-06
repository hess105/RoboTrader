// RoboTrader dashboard — a PURE CLIENT of the engine API.
// No trading logic, no risk logic, no credentials live here: closing or
// crashing this page has zero effect on the engine, its risk checks, or the
// kill switch. Theme (PAPER slate/blue, LIVE red) follows /status.mode —
// the GUI cannot choose it.
import { useCallback, useEffect, useRef, useState } from 'react'

type Status = {
  mode: string
  halt: string
  paused: boolean
  market_open: boolean
  equity: number
  day_loss_pct: number | null
  drawdown_pct: number | null
  open_positions: number
  health: { ok: boolean }
  strategy: { name: string; params: Record<string, unknown> }
}

async function j<T = any>(url: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(url, opts)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}) as any)
    throw new Error(body.detail || r.statusText)
  }
  return r.json()
}
const post = (url: string, body: unknown = {}) =>
  j(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
const fmt$ = (v: number | null | undefined) => (v == null ? '—' : '$' + Number(v).toFixed(2))
const fmtP = (v: number | null | undefined) => (v == null ? '—' : Number(v).toFixed(2) + '%')

function usePoll<T>(fetcher: () => Promise<T>, ms: number): T | null {
  const [data, setData] = useState<T | null>(null)
  useEffect(() => {
    let alive = true
    const run = () => fetcher().then((d) => alive && setData(d)).catch(() => {})
    run()
    const id = setInterval(run, ms)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [ms, fetcher])
  return data
}

const TABS = [
  ['Dashboard', 'overview'],
  ['Blotter', 'orders & fills'],
  ['Risk', 'limits & halts'],
  ['Config', 'model & risk parameters'],
  ['Results', 'backtest reports'],
  ['Logs', 'audit trail'],
  ['Settings', 'keys & alerts'],
] as const
type Tab = (typeof TABS)[number][0]

export default function App() {
  const [tab, setTab] = useState<Tab>('Dashboard')
  const [reachable, setReachable] = useState(true)
  const [status, setStatus] = useState<Status | null>(null)

  useEffect(() => {
    let alive = true
    const run = () =>
      j<Status>('/status')
        .then((s) => {
          if (!alive) return
          setStatus(s)
          setReachable(true)
          document.body.className = s.mode === 'live' ? 'live' : 'paper'
        })
        .catch(() => alive && setReachable(false))
    run()
    const id = setInterval(run, 2000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const mode = status?.mode ?? 'paper'
  const confirmAction = (text: string) =>
    window.confirm(`${text}\n\nMode: ${mode.toUpperCase()}${mode === 'live' ? ' — REAL MONEY' : ''}`)

  return (
    <>
      <Banner status={status} reachable={reachable} />
      <nav>
        {TABS.map(([t, sub]) => (
          <button key={t} title={sub} className={t === tab ? 'active' : ''}
                  onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>
      <main>
        {tab === 'Dashboard' && <Dashboard status={status} confirmAction={confirmAction} />}
        {tab === 'Blotter' && <Blotter />}
        {tab === 'Risk' && <RiskPanel confirmAction={confirmAction} />}
        {tab === 'Config' && <Config />}
        {tab === 'Results' && <Results />}
        {tab === 'Logs' && <Logs />}
        {tab === 'Settings' && <Settings mode={mode} confirmAction={confirmAction} />}
      </main>
      <KillSwitch />
    </>
  )
}

/* ---------------------------------------------------- top bar ---- */

function Clock() {
  const [now, setNow] = useState(new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  const et = now.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false })
  return <span className="clock">{et} ET</span>
}

function Banner({ status, reachable }: { status: Status | null; reachable: boolean }) {
  const s = status
  const halted = s != null && s.halt !== 'none'
  const led = (label: string, state: 'on' | 'warn' | 'bad' | '') => (
    <span className={'led ' + state}><i />{label}</span>
  )
  return (
    <div id="banner">
      <span className="brand">RoboTrader</span>
      <span className="mode-label">
        {s == null ? 'CONNECTING…' : s.mode === 'live' ? 'LIVE — REAL MONEY' : 'PAPER'}
      </span>
      <div className="leds">
        {led('Broker', !reachable ? 'bad' : s?.health.ok ? 'on' : 'bad')}
        {led(s?.market_open ? 'Market open' : 'Market closed', s?.market_open ? 'on' : '')}
        {led(halted ? `Halt: ${s?.halt}` : 'Clear', halted ? 'bad' : 'on')}
        {led(s?.paused ? 'Paused' : 'Active', s?.paused ? 'warn' : 'on')}
      </div>
      <div className="spacer" />
      <span className="eq-readout">{fmt$(s?.equity)}</span>
      <Clock />
    </div>
  )
}

/* ----------------------------------------------------- charts ---- */

function LineChart({ data, height = 210 }: { data: number[]; height?: number }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current
    if (!c || data.length < 2) return
    const g = c.getContext('2d')!
    const css = getComputedStyle(document.body)
    const accent = css.getPropertyValue('--accent').trim() || '#3b82f6'
    const grid = css.getPropertyValue('--border').trim() || '#2a3345'
    g.clearRect(0, 0, c.width, c.height)
    g.strokeStyle = grid
    g.lineWidth = 1
    for (let i = 1; i < 4; i++) {
      g.beginPath()
      g.moveTo(0, (c.height / 4) * i)
      g.lineTo(c.width, (c.height / 4) * i)
      g.stroke()
    }
    const min = Math.min(...data)
    const span = Math.max(...data) - min || 1
    const px = (i: number) => (i / (data.length - 1)) * c.width
    const py = (y: number) => c.height - 12 - ((y - min) / span) * (c.height - 24)
    // fill under the line
    g.beginPath()
    g.moveTo(0, c.height)
    data.forEach((y, i) => g.lineTo(px(i), py(y)))
    g.lineTo(c.width, c.height)
    g.closePath()
    g.fillStyle = css.getPropertyValue('--accent-soft').trim() || 'rgba(59,130,246,.12)'
    g.fill()
    // the line
    g.strokeStyle = accent
    g.lineWidth = 1.8
    g.beginPath()
    data.forEach((y, i) => (i === 0 ? g.moveTo(px(i), py(y)) : g.lineTo(px(i), py(y))))
    g.stroke()
    g.fillStyle = accent
    g.font = '11px sans-serif'
    g.fillText(data[data.length - 1].toFixed(2), c.width - 70, 16)
  }, [data])
  return <canvas ref={ref} className="chart" width={1100} height={height} />
}

function DrawdownChart({ data, height = 140 }: { data: number[]; height?: number }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current
    if (!c || data.length < 2) return
    const g = c.getContext('2d')!
    const css = getComputedStyle(document.body)
    const bad = css.getPropertyValue('--bad').trim() || '#ef4444'
    const grid = css.getPropertyValue('--border').trim() || '#2a3345'
    g.clearRect(0, 0, c.width, c.height)
    g.strokeStyle = grid
    g.lineWidth = 1
    for (let i = 1; i < 4; i++) {
      g.beginPath()
      g.moveTo(0, (c.height / 4) * i)
      g.lineTo(c.width, (c.height / 4) * i)
      g.stroke()
    }
    let peak = data[0]
    const dd = data.map((e) => {
      peak = Math.max(peak, e)
      return peak > 0 ? -100 * (peak - e) / peak : 0
    })
    const min = Math.min(...dd, 0)
    const span = -min || 1
    const px = (i: number) => (i / (dd.length - 1)) * c.width
    const py = (y: number) => 12 + (-y / span) * (c.height - 24)
    g.beginPath()
    g.moveTo(0, py(0))
    dd.forEach((y, i) => g.lineTo(px(i), py(y)))
    g.lineTo(c.width, py(0))
    g.closePath()
    g.fillStyle = 'rgba(239, 68, 68, 0.15)'
    g.fill()
    g.strokeStyle = bad
    g.lineWidth = 1.6
    g.beginPath()
    dd.forEach((y, i) => (i === 0 ? g.moveTo(px(i), py(y)) : g.lineTo(px(i), py(y))))
    g.stroke()
    g.fillStyle = bad
    g.font = '11px sans-serif'
    g.fillText(min.toFixed(1) + '% max', c.width - 90, 16)
  }, [data])
  return <canvas ref={ref} className="chart" width={1100} height={height} />
}

function Stat({ label, value, tone }: { label: string; value: unknown; tone?: 'pos' | 'neg' }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={'value' + (tone ? ' ' + tone : '')}>
        {value == null || value === '' ? '—' : String(value)}
      </div>
    </div>
  )
}

/* -------------------------------------------------- Dashboard ---- */

function Dashboard({ status, confirmAction }: {
  status: Status | null
  confirmAction: (t: string) => boolean
}) {
  const positions = usePoll(useCallback(() => j<any[]>('/positions'), []), 3000)
  const equity = usePoll(useCallback(() => j<[string, number][]>('/equity?days=120'), []), 30000)
  const gate2 = usePoll(useCallback(() => j<any>('/gate2'), []), 30000)
  const day = status?.day_loss_pct == null ? null : -status.day_loss_pct

  const buckets: Record<string, number> = {}
  for (const p of positions ?? []) {
    buckets[p.bucket ?? 'other'] = (buckets[p.bucket ?? 'other'] ?? 0) + (p.market_value ?? 0)
  }
  const totalMv = Object.values(buckets).reduce((a, b) => a + b, 0)

  return (
    <>
      <div className="grid">
        <div className="card"><h3>Equity</h3><div className="big">{fmt$(status?.equity)}</div></div>
        <div className="card">
          <h3>Day P&L</h3>
          <div className={'big ' + (day == null ? '' : day > 0 ? 'pos' : day < 0 ? 'neg' : '')}>
            {fmtP(day)}
          </div>
        </div>
        <div className="card"><h3>Drawdown</h3><div className="big">{fmtP(status?.drawdown_pct)}</div></div>
        <div className="card"><h3>Open Positions</h3><div className="big">{status?.open_positions ?? '—'}</div></div>
      </div>

      <div className="card">
        <h3>Equity Curve<small>daily closes, last 120 sessions</small></h3>
        {equity && equity.length > 1
          ? <LineChart data={equity.map((e) => e[1])} />
          : <div className="muted">Insufficient history — accrues as the engine runs.</div>}
      </div>

      <div className="cols">
        <div className="card">
          <h3>Open Positions</h3>
          <table>
            <thead>
              <tr><th>Symbol</th><th>Strategy</th><th>Qty</th><th>Entry</th><th>Last</th>
                  <th>Unrealized</th><th>Stop</th></tr>
            </thead>
            <tbody>
              {(positions ?? []).map((p) => (
                <tr key={p.symbol}>
                  <td><b>{p.symbol}</b></td>
                  <td className="muted">{(p.strategy || '').replace('_rotation', '').replace('trend_pullback', 'pullback')}</td>
                  <td>{p.qty}</td>
                  <td>{fmt$(p.avg_entry)}</td>
                  <td>{fmt$(p.current_price)}</td>
                  <td className={p.unrealized_pl > 0 ? 'pos' : p.unrealized_pl < 0 ? 'neg' : ''}>
                    {fmt$(p.unrealized_pl)}
                  </td>
                  <td>{p.stop ? fmt$(p.stop) : '⚠ none'}</td>
                </tr>
              ))}
              {(positions ?? []).length === 0 && (
                <tr><td colSpan={7} className="muted">Flat — no open positions.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div>
          <div className="card">
            <h3>Exposure by Bucket</h3>
            {totalMv === 0 && <div className="muted">No deployed capital.</div>}
            {Object.entries(buckets).map(([b, mv]) => (
              <div key={b} style={{ marginBottom: 10 }}>
                <div className="check"><span>{b}</span><b>{fmt$(mv)}</b></div>
                <div className="bar"><i className="amber" style={{ width: (mv / (totalMv || 1)) * 100 + '%' }} /></div>
              </div>
            ))}
          </div>
          {gate2 && <Gate2Card g={gate2} />}
        </div>
      </div>

      <div className="card">
        <h3>Strategy Control<small>{status?.strategy.name}</small></h3>
        <div className="row">
          <button className="btn" onClick={() => {
            if (!confirmAction('Pause strategy? No new signals will be generated.')) return
            post('/strategy/pause', { note: 'paused via GUI' }).catch((e) => alert(e.message))
          }}>Pause</button>
          <button className="btn" onClick={() => {
            if (!confirmAction('Resume signal generation?')) return
            post('/strategy/resume', { note: 'resumed via GUI' }).catch((e) => alert(e.message))
          }}>Resume</button>
        </div>
        <div className="muted">
          {Object.keys(status?.strategy.params ?? {}).length
            ? `Composite of ${Object.keys(status!.strategy.params).join(' + ')} — full parameters in the Config tab.`
            : 'No active sleeves.'}
        </div>
      </div>
    </>
  )
}

function Gate2Card({ g }: { g: any }) {
  const bar = (label: string, val: number, target: number) => (
    <div style={{ marginBottom: 10 }}>
      <div className="check"><span>{label}</span><b>{val} / {target}</b></div>
      <div className="bar">
        <i className={val >= target ? '' : 'amber'}
           style={{ width: Math.min(100, (val / target) * 100) + '%' }} />
      </div>
    </div>
  )
  return (
    <div className="card">
      <h3>Gate 2 Progress<small>paper → live checklist</small></h3>
      {bar('Trading days', g.trading_days, g.target_days)}
      {bar('Closed trades', g.closed_trades, g.target_trades)}
      {bar('Kill-switch drills', g.drills, g.target_drills)}
      <div className="check"><span>Alert test</span>
        <b>{g.last_alert_test ? g.last_alert_test.slice(0, 10) : 'never'}</b></div>
      <div className="check"><span>Last drill</span>
        <b>{g.last_drill ? g.last_drill.slice(0, 10) : 'never'}</b></div>
    </div>
  )
}

/* ------------------------------------------------------ Config ---- */

function Item({ label, value }: { label: string; value: unknown }) {
  const shown = value == null || value === '' ? '—' : String(value)
  return (
    <div className="item">
      <span>{label.replace(/_/g, ' ')}</span>
      <b>{shown}</b>
    </div>
  )
}

function Config() {
  const cfg = usePoll(useCallback(() => j<any>('/config'), []), 60000)
  if (!cfg) return <div className="card muted">Loading configuration…</div>

  const strategies: [string, Record<string, unknown>][] =
    (cfg.strategies ?? []).map((s: any) => [s.name, s.params])
  const buckets: Record<string, string[]> = cfg.universe?.buckets ?? {}

  return (
    <>
      <div className="card">
        <h3>Account & Universe<small>config/base.yaml</small></h3>
        <div className="kv">
          <Item label="Starting capital" value={fmt$(cfg.account?.starting_capital)} />
          <Item label="Account type" value={cfg.account?.account_type} />
          <Item label="Min avg $ volume" value={fmt$(cfg.universe?.min_avg_dollar_volume)} />
          <Item label="Max spread" value={cfg.universe?.max_spread_pct + '% of mid'} />
        </div>
        {Object.entries(buckets).map(([bucket, syms]) => (
          <div key={bucket} style={{ marginTop: 12 }}>
            <div className="muted">{bucket.replace(/_/g, ' ')}</div>
            <div className="chips">
              {syms.map((s) => <span key={s} className="chip">{s}</span>)}
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>Strategy Configuration<small>strategies/composite.py sleeves</small></h3>
        {strategies.map(([name, params]) => (
          <div key={name} className="subcard">
            <h4>{name}</h4>
            <div className="kv">
              {Object.entries(params).map(([k, v]) => (
                <Item key={k} label={k} value={Array.isArray(v) ? v.join(', ') : v} />
              ))}
            </div>
          </div>
        ))}
        {strategies.length === 0 && <div className="muted">No strategies configured.</div>}
      </div>

      <div className="cols">
        <div className="card">
          <h3>Risk Limits<small>risk/manager.py</small></h3>
          <div className="kv">
            {Object.entries(cfg.risk ?? {})
              .filter(([, v]) => !(v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0))
              .map(([k, v]) => <Item key={k} label={k} value={v} />)}
          </div>
        </div>
        <div>
          <div className="card">
            <h3>Execution<small>order handling & schedule</small></h3>
            <div className="kv">
              <Item label="Entry order" value={cfg.execution?.order_type_entry} />
              <Item label="Entry limit offset" value={cfg.execution?.entry_limit_offset_bps + ' bps'} />
              <Item label="Exit order" value={cfg.execution?.order_type_exit} />
              <Item label="Entry timeout" value={cfg.execution?.order_timeout_sec + 's'} />
              <Item label="Signal time (ET)" value={cfg.execution?.schedule?.signal_time} />
              <Item label="Reconcile time (ET)" value={cfg.execution?.schedule?.reconcile_time} />
            </div>
          </div>
          <div className="card">
            <h3>Monitoring & Alerts</h3>
            <div className="kv">
              <Item label="Disconnect alert" value={cfg.monitoring?.disconnect_alert_min + ' min'} />
              <Item label="Kill on disconnect" value={cfg.monitoring?.kill_after_disconnect_min + ' min'} />
            </div>
            <div className="chips" style={{ marginTop: 10 }}>
              {(cfg.alerts?.channels ?? []).map((c: string) => <span key={c} className="chip">{c}</span>)}
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>Backtest Assumptions<small>backtest/costs.py — validation, not live execution</small></h3>
        <div className="kv">
          <Item label="History start" value={cfg.backtest?.start} />
          <Item label="Commission/share" value={fmt$(cfg.backtest?.commission_per_share)} />
          <Item label="SEC fee / $1M sold" value={fmt$(cfg.backtest?.sec_fee_per_million)} />
          <Item label="FINRA TAF / share" value={fmt$(cfg.backtest?.taf_per_share)} />
          <Item label="Slippage model" value={cfg.backtest?.slippage_model} />
          <Item label="Extra slippage" value={cfg.backtest?.extra_slippage_bps + ' bps'} />
        </div>
      </div>
    </>
  )
}

/* ----------------------------------------------------- Blotter ---- */

function Blotter() {
  const orders = usePoll(useCallback(() => j<any[]>('/orders'), []), 3000)
  return (
    <div className="card">
      <h3>Order & Fill Blotter</h3>
      <table>
        <thead>
          <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Notional</th><th>Qty</th>
              <th>Status</th><th>Client ID</th></tr>
        </thead>
        <tbody>
          {(orders ?? []).map((o) => (
            <tr key={o.client_order_id}>
              <td>{(o.ts || '').slice(0, 19).replace('T', ' ')}</td>
              <td><b>{o.symbol}</b></td>
              <td className={o.side === 'buy' ? 'pos' : 'neg'}>{o.side}</td>
              <td>{o.notional ? fmt$(o.notional) : ''}</td>
              <td>{o.qty ?? ''}</td>
              <td>{o.status}</td>
              <td className="muted">{o.client_order_id}</td>
            </tr>
          ))}
          {(orders ?? []).length === 0 && (
            <tr><td colSpan={7} className="muted">No orders on record.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

/* -------------------------------------------------- Risk panel ---- */

function RiskPanel({ confirmAction }: { confirmAction: (t: string) => boolean }) {
  const risk = usePoll(useCallback(() => j<any>('/risk'), []), 3000)
  const gate2 = usePoll(useCallback(() => j<any>('/gate2'), []), 30000)
  const [note, setNote] = useState('')
  if (!risk) return <div className="card muted">Loading…</div>
  const rows: [string, number | null, number][] = [
    ['Daily loss', risk.day_loss_pct ?? null, risk.limits.daily_loss_halt_pct],
    ['Weekly loss', risk.week_loss_pct ?? null, risk.limits.weekly_loss_halt_pct],
    ['Drawdown', risk.drawdown_pct ?? null, risk.limits.max_drawdown_halt_pct],
  ]
  return (
    <>
      <div className="card">
        <h3>Risk Limits<small>amber at 80%, red at breach</small></h3>
        {rows.map(([label, v, lim]) => {
          const pct = Math.max(0, Math.min(100, ((v ?? 0) / lim) * 100))
          const cls = pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : ''
          return (
            <div key={label} style={{ marginBottom: 14 }}>
              <div className="check"><span>{label}</span><b>{fmtP(v)} of {lim}%</b></div>
              <div className="bar"><i className={cls} style={{ width: pct + '%' }} /></div>
            </div>
          )
        })}
        <div className="check"><span>Halt state</span><b>{risk.halt}</b></div>
        <div className="check"><span>Peak equity</span><b>{fmt$(risk.peak_equity)}</b></div>
        <div className="row" style={{ marginTop: 16 }}>
          <input className="grow" placeholder="post-mortem note (required to clear a halt)"
                 value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="btn" onClick={() => {
            if (!note.trim()) return alert('A written post-mortem note is required.')
            if (!confirmAction('Clear the halt and resume trading?')) return
            post('/halt/reset', { note }).then(() => setNote('')).catch((e) => alert(e.message))
          }}>Clear Halt</button>
        </div>
      </div>
      {gate2 && <Gate2Card g={gate2} />}
    </>
  )
}

/* ----------------------------------------------------- Results ---- */

type SortKey = 'entry_ts' | 'pnl' | 'bars_held'

function Results() {
  const runs = usePoll(useCallback(() => j<any[]>('/backtests'), []), 10000)
  const [detail, setDetail] = useState<any | null>(null)
  const [symbolFilter, setSymbolFilter] = useState('')
  const [reasonFilter, setReasonFilter] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('entry_ts')
  const [sortDir, setSortDir] = useState<1 | -1>(-1)   // -1 = newest/largest first

  const openRun = (id: string) => {
    setSymbolFilter('')
    setReasonFilter('')
    setSortKey('entry_ts')
    setSortDir(-1)
    j('/backtests/' + id).then(setDetail).catch((e) => alert(e.message))
  }
  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortDir((d) => (d === 1 ? -1 : 1))
    else { setSortKey(key); setSortDir(-1) }
  }
  const sortArrow = (key: SortKey) => sortKey === key ? (sortDir === 1 ? '↑' : '↓') : null

  const trades: any[] = detail?.trades ?? []
  const symbols = Array.from(new Set(trades.map((t) => t.symbol))).sort()
  const reasons = Array.from(new Set(trades.map((t) => t.exit_reason))).sort()

  const filtered = trades
    .filter((t) => (!symbolFilter || t.symbol === symbolFilter)
                && (!reasonFilter || t.exit_reason === reasonFilter))
    .sort((a, b) => {
      const av = sortKey === 'entry_ts' ? a.entry_ts : +a[sortKey]
      const bv = sortKey === 'entry_ts' ? b.entry_ts : +b[sortKey]
      return (av < bv ? -1 : av > bv ? 1 : 0) * sortDir
    })
  const wins = filtered.filter((t) => +t.pnl > 0).length
  const winRate = filtered.length ? (100 * wins / filtered.length).toFixed(1) + '%' : '—'
  const shown = filtered.slice(0, 100)

  return (
    <>
      <div className="card">
        <h3>Backtest Results<small>click a run to open</small></h3>
        <table>
          <thead>
            <tr><th>Run</th><th>Trades</th><th>Sharpe</th><th>PF</th><th>Max DD</th>
                <th>Return</th><th>Halts</th></tr>
          </thead>
          <tbody>
            {(runs ?? []).map((r) => (
              <tr key={r.run_id} className="clickable" onClick={() => openRun(r.run_id)}>
                <td>{r.run_id}</td>
                <td>{r.trades}</td>
                <td>{r.sharpe ?? ''}</td>
                <td>{r.profit_factor ?? ''}</td>
                <td>{r.max_drawdown_pct ?? ''}%</td>
                <td>{r.total_return_pct ?? ''}%</td>
                <td>{r.drawdown_halts ?? ''}</td>
              </tr>
            ))}
            {(runs ?? []).length === 0 && (
              <tr><td colSpan={7} className="muted">No reports — run `make backtest`.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {detail && (
        <>
          <div className="card">
            <h3>{detail.run_id}<small>gate-qualifying run metrics</small></h3>
            <div className="stats">
              <Stat label="Trades" value={detail.metrics.trades} />
              <Stat label="Sharpe" value={detail.metrics.sharpe} />
              <Stat label="Profit factor" value={detail.metrics.profit_factor} />
              <Stat label="Max drawdown" value={detail.metrics.max_drawdown_pct + '%'} tone="neg" />
              <Stat label="Total return" value={detail.metrics.total_return_pct + '%'}
                    tone={detail.metrics.total_return_pct >= 0 ? 'pos' : 'neg'} />
              <Stat label="CAGR" value={detail.metrics.cagr_pct + '%'} />
              <Stat label="Win rate" value={detail.metrics.win_rate_pct + '%'} />
              <Stat label="Expectancy" value={detail.metrics.expectancy} />
              <Stat label="Avg win" value={detail.metrics.avg_win} tone="pos" />
              <Stat label="Avg loss" value={detail.metrics.avg_loss} tone="neg" />
              <Stat label="Avg bars held" value={detail.metrics.avg_bars_held} />
              <Stat label="Exposure" value={detail.metrics.exposure_pct + '%'} />
              {detail.metrics.drawdown_halts != null &&
                <Stat label="Breaker halts" value={detail.metrics.drawdown_halts} />}
            </div>
          </div>

          <div className="cols">
            <div className="card">
              <h3>Equity Curve<small>starting equity to close</small></h3>
              <LineChart data={detail.equity.map((e: [string, number]) => e[1])} />
            </div>
            <div className="card">
              <h3>Drawdown<small>% below running peak</small></h3>
              <DrawdownChart data={detail.equity.map((e: [string, number]) => e[1])} />
            </div>
          </div>

          <div className="card">
            <h3>Trade Log<small>{filtered.length} of {trades.length} trades — win rate {winRate}</small></h3>
            <div className="row">
              <select value={symbolFilter} onChange={(e) => setSymbolFilter(e.target.value)}>
                <option value="">All symbols</option>
                {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select value={reasonFilter} onChange={(e) => setReasonFilter(e.target.value)}>
                <option value="">All exit reasons</option>
                {reasons.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="sortable" onClick={() => toggleSort('entry_ts')}>
                    Entry<span className="arrow">{sortArrow('entry_ts')}</span>
                  </th>
                  <th>Exit</th>
                  <th className="sortable" onClick={() => toggleSort('pnl')}>
                    P&L<span className="arrow">{sortArrow('pnl')}</span>
                  </th>
                  <th className="sortable" onClick={() => toggleSort('bars_held')}>
                    Bars<span className="arrow">{sortArrow('bars_held')}</span>
                  </th>
                  <th>Exit Reason</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t: any, i: number) => (
                  <tr key={i}>
                    <td><b>{t.symbol}</b></td>
                    <td>{(t.entry_ts || '').slice(0, 10)}</td>
                    <td>{(t.exit_ts || '').slice(0, 10)}</td>
                    <td className={+t.pnl > 0 ? 'pos' : 'neg'}>{(+t.pnl).toFixed(2)}</td>
                    <td>{t.bars_held}</td>
                    <td>{t.exit_reason}</td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr><td colSpan={6} className="muted">No trades match this filter.</td></tr>
                )}
              </tbody>
            </table>
            {filtered.length > shown.length && (
              <div className="muted" style={{ marginTop: 8 }}>
                Showing the most recent {shown.length} of {filtered.length} filtered trades.
              </div>
            )}
          </div>
        </>
      )}
    </>
  )
}

/* -------------------------------------------------------- Logs ---- */

function Logs() {
  const KINDS = ['', 'risk_reject', 'risk_approve', 'risk_warning', 'halt_change', 'fill',
    'order_queued', 'reconcile', 'kill_switch', 'equity_mark', 'alert_test']
  const [kind, setKind] = useState('')
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<any[]>([])
  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: '300' })
    if (kind) params.set('kind', kind)
    if (q) params.set('q', q)
    j<any[]>('/logs?' + params).then(setRows).catch(() => {})
  }, [kind, q])
  useEffect(load, [load])
  return (
    <div className="card">
      <h3>Audit Log<small>every decision, journaled</small></h3>
      <div className="row">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => <option key={k} value={k}>{k || 'all kinds'}</option>)}
        </select>
        <input className="grow" placeholder="filter…" value={q}
               onChange={(e) => setQ(e.target.value)} />
        <button className="btn" onClick={load}>Search</button>
      </div>
      <table>
        <thead>
          <tr><th>Time</th><th>Kind</th><th>Symbol</th><th>Reason / Detail</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{(r.ts || '').slice(0, 19).replace('T', ' ')}</td>
              <td>{r.kind}</td>
              <td>{r.symbol ?? ''}</td>
              <td>{[r.reason, r.detail].filter(Boolean).join(' — ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ---------------------------------------------------- Settings ---- */

function Settings({ mode, confirmAction }: { mode: string; confirmAction: (t: string) => boolean }) {
  const [keyMode, setKeyMode] = useState('paper')
  const [keyId, setKeyId] = useState('')
  const [secret, setSecret] = useState('')
  return (
    <>
      <div className="card">
        <h3>Alert Test<small>verify alerts reach your phone</small></h3>
        <div className="row">
          <button className="btn" onClick={() =>
            post('/alerts/test').then(() => alert('Test alert dispatched — check Telegram/email.'))
              .catch((e) => alert(e.message))
          }>Send Test Alert</button>
          <span className="muted">Gate 2 requires a verified end-to-end alert.</span>
        </div>
      </div>
      <div className="card">
        <h3>Broker API Keys<small>write-only, straight to OS keychain</small></h3>
        <div className="row">
          <select value={keyMode} onChange={(e) => setKeyMode(e.target.value)}>
            <option value="paper">paper</option>
            <option value="live">live</option>
          </select>
          <input type="password" autoComplete="off" placeholder="key id"
                 value={keyId} onChange={(e) => setKeyId(e.target.value)} />
          <input type="password" autoComplete="off" placeholder="secret"
                 value={secret} onChange={(e) => setSecret(e.target.value)} />
          <button className="btn" onClick={() => {
            if (!confirmAction(`Store ${keyMode.toUpperCase()} API keys in the OS keychain?`)) return
            post('/keys', { mode: keyMode, key_id: keyId, secret_key: secret })
              .then(() => {
                setKeyId('')
                setSecret('')
                alert('Stored. Keys will not be displayed again.')
              })
              .catch((e) => alert(e.message))
          }}>Store</button>
        </div>
        <div className="muted">Keys are never echoed back after entry.</div>
      </div>
      <div className="card">
        <h3>Mode</h3>
        <p>Mode is <b>{mode.toUpperCase()}</b>. There is deliberately <b>no live-mode
        switch in this dashboard</b>. Going live requires restarting the engine from
        its own terminal:</p>
        <pre>make live</pre>
        <p className="muted">…which runs the tests, prints the gate warning, and demands the
        typed confirmation phrase. A remote dashboard cannot consent to real money.
        See docs/GATES.md.</p>
      </div>
    </>
  )
}

/* -------------------------------------------------- kill switch ---- */

function KillSwitch() {
  const [open, setOpen] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [reason, setReason] = useState('')
  return (
    <>
      <button id="kill" onClick={() => { setConfirmText(''); setOpen(true) }}>
        Kill Switch — Flatten All
      </button>
      {open && (
        <div className="overlay">
          <div className="modal">
            <h3>Flatten everything?</h3>
            <p>Cancels all open orders, market-sells every position, and halts
               trading until a manual reset. Type <b>FLATTEN</b> to confirm.</p>
            <input placeholder="FLATTEN" value={confirmText}
                   onChange={(e) => setConfirmText(e.target.value)} />
            <input placeholder="reason (journaled)" value={reason}
                   onChange={(e) => setReason(e.target.value)} />
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn" style={{ borderColor: 'var(--bad)', color: 'var(--bad)' }}
                onClick={() => {
                  if (confirmText !== 'FLATTEN') return alert('Type FLATTEN exactly.')
                  post('/killswitch', { confirm: 'FLATTEN', reason: reason || 'GUI kill switch' })
                    .then((r: any) =>
                      alert(r.flat ? 'Flat confirmed. Trading halted.' : 'NOT FLAT — intervene manually!'))
                    .catch((e) => alert('Kill switch error: ' + e.message))
                  setOpen(false)
                }}>Flatten & Halt</button>
              <button className="btn" onClick={() => setOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
