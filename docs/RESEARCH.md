# Research Review — Can Something "Blow This Out of the Water"?

Short answer up front, because it matters more than any individual finding
below: **no strategy category found in this research reliably beats a
Sharpe ~1.1, walk-forward-validated, diversified swing strategy without
either (a) adding infrastructure this account/broker setup doesn't have, or
(b) quietly reintroducing the overfitting risk this project has spent real
effort guarding against.** That's not a discouraging conclusion — it means
the current strategy (`docs/STRATEGY.md`) is already sitting in a
defensible, evidence-consistent range, and the highest-value next moves are
incremental, not a replacement.

## Why "blow it out of the water" is the wrong bar

The published evidence on backtest-to-live translation is blunt about this:

- A large-scale study of 888 real algorithmic strategies (built on the
  Quantopian platform) found in-sample Sharpe ratio had essentially **zero
  predictive power** for out-of-sample performance (R² < 0.025).
- Backtested Sharpe ratios "typically overstate live performance by 30–50%
  or more"; a backtested 2.0 often realizes as 1.0–1.4 live. A backtested
  Sharpe **above 3 should make you suspicious of methodology errors**
  (look-ahead bias, survivorship, multiple-testing), not excited.
- AQR found a simple moving-average strategy's Sharpe fell from 1.2
  in-sample to **-0.2** on fresh data.

This system's current numbers (Sharpe 1.09 in-sample / 1.08 out-of-sample,
profit factor 2.10, max drawdown 12.1%, 4/4 walk-forward folds profitable,
survives 2x cost stress) are unusual specifically *because* they already
cleared the out-of-sample and walk-forward bar most backtests never face.
Anything proposed as a replacement needs to clear the same bar, not just
look better on an in-sample chart — which is exactly what the project's own
gate discipline (README Rule 1, `docs/GATES.md`) already enforces. The
research doesn't suggest changing that discipline; it suggests being
skeptical of anything that promises to beat it easily.

## What the alternatives actually look like, researched

**Dual/absolute momentum & trend-following** (the closest published
comparison to the existing `momentum_rotation` sleeve): documented
long-run Sharpe ratios cluster around **0.86–1.17** across variants (Gary
Antonacci's GEM-style dual momentum, 12-month lookback studies). That's the
*same neighborhood* as what's already running, not an upgrade — momentum is
one of the most robustly documented anomalies in finance, but it isn't an
undiscovered edge waiting to double this system's Sharpe.

**Volatility risk premium / options income** (selling puts or covered
calls against the volatility risk premium): documented Sharpe ~**0.7–1.0**,
generally *below* what this system already achieves, with a well-known,
serious tail risk — "put sellers historically incurring losses up to
-800%" in stress events, with strong serial correlation on large down days.
**More importantly, this is not implementable at this account's actual
size**: a single cash-secured put or covered call on any liquid underlying
(even something cheap like an ETF at ~$50–80/share) requires 100-share
collateral — $5,000–$8,000+ notional per contract, against a ~$500
starting account. This isn't a "not recommended," it's a "not possible."

**Statistical arbitrage / pairs trading**: retail-feasible only in narrow
conditions, and the research is explicit that **execution quality, not
strategy design, is usually the deciding factor** — correlations break
suddenly at event risk, and the edge decays as more participants find it,
requiring continuous new-signal research rather than a set-and-forget
config. This system's entire data/execution path is daily bars through a
REST API (Alpaca) built for exactly the kind of swing/position trading it
already does — stat arb would need a materially different, lower-latency
execution stack, cointegration monitoring, and ongoing signal research just
to be attempted, let alone to work.

**Machine learning strategies**: the honest 2025-2026 read is mixed —
"hybrid ML-rules systems outperform pure AI" in practitioner writeups, with
a cited realistic target of "Sharpe over 1.5" — genuinely higher than this
system's current number. But ML strategies are also squarely the category
the 888-strategy overfitting study is most damning about, and they conflict
directly with this project's own stated philosophy: `config/base.yaml`'s
comments already reason explicitly about "no dozens of knobs," and
README Rule 1 treats every parameter as something that has to re-earn Gate
1. A model with hundreds or thousands of learned parameters is a much
larger overfitting surface than the ~10 hand-picked parameters currently
in `config/base.yaml`, with far less of it human-auditable in the way this
project's audit-log/gate philosophy assumes.

## What would actually help — realistic, incremental, fits the existing architecture

**1. Turn on volatility targeting and test it through the sweep tooling
that already exists.** `risk/manager.py` already implements `vol_scale()`
(shrinks new-position risk when trailing realized vol exceeds
`vol_target_pct`) — it's just off (`vol_target_pct: 0`) in the validated
config. The seminal result here (Moreira & Muir 2017, "Volatility-Managed
Portfolios") found vol-timing produced large Sharpe improvements across
several equity factors. But this isn't a slam dunk to just flip on: a
broader follow-up study across 103 strategies found **no statistically
robust Sharpe improvement** once implementation realism was accounted for,
with the original result's real-time-implementable versions performing
worse out-of-sample. Treat this exactly like any other parameter change —
run it through `scripts/sweep_full.py` or a dedicated sweep, judge it OOS,
and let it earn its way in rather than assuming the 2017 paper's headline
number transfers.

**2. A genuinely uncorrelated third sleeve is worth more than a "better"
existing one.** For N equal-Sharpe, uncorrelated strategies combined,
portfolio Sharpe scales roughly with √N — this is why the two-sleeve
composite already outperforms either sleeve alone. The hard part is
"genuinely uncorrelated": trend_pullback and momentum_rotation already
split the regime space (choppy vs. trending) reasonably cleanly. A third
sleeve is only worth adding if it earns money in a *third* regime neither
current sleeve covers — not because three sleeves sounds more sophisticated
than two. This is a research project in itself, not a quick config tweak.

**3. Tighten the overfitting controls in the sweep tooling itself.** Given
the 888-strategy finding above, the project's existing IS→OOS→walk-forward
discipline (`scripts/sweep_params.py`, `scripts/sweep_allocation.py`,
`scripts/sweep_full.py`) is already better practice than most of what the
research describes retail traders actually doing. The next step up in
rigor is combinatorial purged cross-validation (CPCV) — testing every
combination of train/test splits rather than one IS/OOS split — which would
catch overfitting the current single-split walk-forward could still miss,
at the cost of meaningfully more compute per sweep.

## What's explicitly not recommended, and why

| Approach | Why not, here |
|---|---|
| Options / volatility risk premium | Capital-infeasible at current account size (100-share collateral vs. ~$500 equity); Sharpe is lower than current anyway; documented severe tail risk. |
| Statistical arbitrage / pairs trading | Requires a different execution stack (low-latency, cointegration monitoring) than the daily-bar/REST-API design this system is built around; edges decay and need continuous research, not a config. |
| Replacing the rule-based core with ML | Directly conflicts with this project's own "no dozens of knobs," audit-everything, gate-before-you-scale philosophy; the overfitting research is most damning specifically about this category. |
| Chasing a "blow it out of the water" backtest number | The research is unambiguous that impressive in-sample numbers are the least trustworthy signal available — the current system's credibility comes from already having survived OOS/walk-forward, which most alternatives haven't been subjected to at all. |

## Bottom line

The existing two-sleeve composite is not a strategy to be embarrassed of
relative to the researched alternatives — it's already performing in the
range serious published momentum/trend-following work documents, and it's
already cleared a validation bar (OOS + walk-forward + cost-stress) that
the research says most retail and even professionally-developed strategies
never clear. The realistic path to a materially better number is
incremental (volatility targeting tested rigorously, a genuinely
uncorrelated third sleeve if one can be found, tighter cross-validation on
the sweep tooling) — not a wholesale strategy swap into a different asset
class or modeling paradigm that this account size and infrastructure can't
actually support.

## Sources

- [Quant Trading Strategies 2026 | Quantt](https://www.quantt.co.uk/resources/quant-trading-strategies-guide)
- [The Ultimate Winning Trading Strategy: 7 Proven Systems (Medium)](https://medium.com/@fxmbrand/the-ultimate-winning-trading-strategy-7-proven-systems-that-actually-work-in-2026-9210b40f76c2)
- [Simple versus Advanced Systematic Trading Strategies — QuantStart](https://www.quantstart.com/articles/simple-versus-advanced-systematic-trading-strategies-which-is-better/)
- [Accelerating Dual Momentum — PortfolioDB](https://portfoliodb.co/portfolios/accelerating-dual-momentum/)
- [Fragility Case Study: Dual Momentum GEM — Flirting with Models](https://blog.thinknewfound.com/2019/01/fragility-case-study-dual-momentum-gem/)
- [Dual Momentum Rotation Strategy (S&P 500) — trendinvestorpro.com](https://trendinvestorpro.com/dual-momo-spx-details-perf-latest/)
- [Backtesting Momentum Strategies Across Lookback/Holding Periods — NextInvest](https://nextinvest.org/post_detail/8089150d-e2ff-4e99-8eb8-19746983e885)
- [Volatility Risk Premium (VRP) — DayTrading.com](https://www.daytrading.com/volatility-risk-premium-vrp)
- [Understanding the Volatility Risk Premium — AQR](https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf)
- [Harvesting the Volatility Risk Premium Globally — The Hedge Fund Journal](https://thehedgefundjournal.com/harvesting-the-volatility-risk-premium-globally/)
- [Forex Pairs Trading & Statistical Arbitrage Explained (2026)](https://bjftradinggroup.com/forex-pairs-trading-statistical-arbitrage/)
- [Statistical Arbitrage Strategies 2026 — Quantt](https://www.quantt.co.uk/resources/statistical-arbitrage-strategies)
- [What Is Overfitting in Trading Strategies? — LuxAlgo](https://www.luxalgo.com/blog/what-is-overfitting-in-trading-strategies/)
- [Algorithmic strategies: managing the overfitting bias — Macrosynergy](https://macrosynergy.com/research/algorithmic-strategies-managing-overfitting-bias/)
- [Understanding Sharpe Ratios When Selecting Trading Algorithms — Breaking Alpha](https://breakingalpha.io/insights/understanding-sharpe-ratios-selecting-trading-algorithms)
- [Machine Learning in Trading: 2026 Strategies & Data — AI Superior](https://aisuperior.com/machine-learning-in-trading/)
- [ML Trading Strategies 2026 — Lunefi](https://lunefi.com/blog/machine-learning-trading-strategies-2026-trends-stats-insights)
- [Volatility-Managed Portfolios — Moreira & Muir, SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2659431)
- [Volatility Managed Portfolios — NBER Working Paper](https://www.nber.org/papers/w22208)
- [On the performance of volatility-managed portfolios — Lehigh](https://www.lehigh.edu/~xuy219/research/COWY.pdf)
- [On the performance of volatility-managed equity factors — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S092753982400094X)
