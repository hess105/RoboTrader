# GUI — React (Vite) dashboard, pure client of the engine API

The GUI is a **pure client** of [service/api.py](../service/api.py) (REST on
127.0.0.1:8765). It holds zero trading logic, zero risk logic, and no broker
credentials. Closing it, freezing it, or killing the browser has no effect on
the engine, its risk checks, or the kill switch — those live in the engine
daemon (`make paper`).

## Running it

```
make paper       # start the engine daemon (serves API + built GUI on :8765)
make gui         # dev server on :5173 with hot reload, proxying to the engine
make gui-build   # production build; the engine serves dist/ at :8765
```

## What's where

- `web/src/App.tsx` — the entire dashboard: banner, tabs, kill switch.
- `web/src/index.css` — theme. **PAPER = blue chrome, LIVE = pulsing red
  chrome with a "LIVE — REAL MONEY" banner.** The theme is driven by
  `/status.mode`; the GUI cannot choose it.
- `web/vite.config.ts` — dev proxy so API paths hit the engine.

## Theme

Clean, professional trading dashboard: dark slate panels with a blue accent
for **PAPER**, switching to a red/amber alert chrome for **LIVE — REAL
MONEY**. Rounded cards with subtle borders and shadows, a status LED row
(Broker / Market / Halt / Active), a live ET clock, filled equity chart, and
thin progress gauges. The theme follows `/status.mode` — the client cannot
choose it.

## Screens

- **Dashboard** — equity/day-P&L/drawdown/position tiles, live equity chart
  (`/equity`), open positions with owning strategy and stop levels (⚠ none
  flagged), exposure-by-bucket bars, **Gate 2 progress** card (trading days,
  closed trades, kill drills, alert-test date — all journal-derived via
  `/gate2`), strategy pause/resume with mode-aware confirmation.
- **Blotter** — live order/fill table from the journal.
- **Risk** — each limit as a gauge, amber at 80%, red at breach; halt
  clearing requires a typed post-mortem note (enforced server-side); Gate 2
  card repeated here.
- **Results** — backtest runs with gate metrics; click a run for the equity
  curve and trade log.
- **Logs** — audit event search by kind + free text.
- **Settings** — alert test (`POST /alerts/test` — Gate 2 requires a
  verified end-to-end alert), write-only API key entry, and the mode note:
  no live-mode switch exists in the dashboard.
- **Kill switch** — fixed corner button, typed-FLATTEN modal; the API
  independently requires the same token, so the modal is UX, not the guard.
