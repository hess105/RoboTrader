"""Strategy registry and factory.

Config supports either a single `strategy:` block or a `strategies:` list;
the factory returns one Strategy either way (a CompositeStrategy when there
are multiple sleeves). A sleeve's params may name `buckets:` instead of
symbols — resolved here against universe.buckets so the universe stays
defined in exactly one place.
"""
from __future__ import annotations

from strategies.base import Strategy
from strategies.composite import CompositeStrategy
from strategies.momentum_rotation import MomentumRotation
from strategies.trend_pullback import TrendPullback

REGISTRY: dict[str, type[Strategy]] = {
    TrendPullback.name: TrendPullback,
    MomentumRotation.name: MomentumRotation,
}


def build_strategy(cfg: dict) -> Strategy:
    entries = cfg.get("strategies") or [cfg["strategy"]]
    children = [REGISTRY[e["name"]](_resolve_params(e["params"], cfg)) for e in entries]
    return children[0] if len(children) == 1 else CompositeStrategy(children)


def _resolve_params(params: dict, cfg: dict) -> dict:
    params = dict(params)
    if "buckets" in params:
        buckets = cfg["universe"]["buckets"]
        params["symbols"] = [s for b in params.pop("buckets") for s in buckets[b]]
    return params
