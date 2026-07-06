"""Local bar cache: one parquet file per symbol plus a JSON manifest of the
date range each symbol's cache *covers* (the requested range, not the data
range — a request through today may legitimately return bars only through
yesterday, and that request is still satisfied).

put() merges with any existing frame (dedup on index, sorted), so tail
top-ups after the first full fetch are cheap. Corporate-action re-adjustment
should invalidate() the symbol wholesale — adjusted history changes
retroactively, so partial patching is unsafe.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


class BarCache:
    def __init__(self, cache_dir: str = "data/cache"):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.dir / "manifest.json"
        self._manifest: dict = (
            json.loads(self._manifest_path.read_text())
            if self._manifest_path.exists() else {}
        )

    def coverage(self, symbol: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        cov = self._manifest.get(symbol)
        if not cov:
            return None
        return pd.Timestamp(cov["start"]), pd.Timestamp(cov["end"])

    def get(self, symbol: str, start, end) -> pd.DataFrame | None:
        """Bars for [start, end] if fully covered, else None."""
        cov = self.coverage(symbol)
        if cov is None or cov[0] > pd.Timestamp(start) or cov[1] < pd.Timestamp(end):
            return None
        df = pd.read_parquet(self._path(symbol))
        return df.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def put(self, symbol: str, frame: pd.DataFrame, start, end) -> None:
        """Merge `frame` into the cache and mark [start, end] as covered."""
        path = self._path(symbol)
        if path.exists():
            old = pd.read_parquet(path)
            frame = pd.concat([old, frame])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame.to_parquet(path)
        cov = self.coverage(symbol)
        new_start = min(pd.Timestamp(start), cov[0]) if cov else pd.Timestamp(start)
        new_end = max(pd.Timestamp(end), cov[1]) if cov else pd.Timestamp(end)
        self._manifest[symbol] = {"start": str(new_start.date()), "end": str(new_end.date())}
        self._manifest_path.write_text(json.dumps(self._manifest, indent=1))

    def invalidate(self, symbol: str) -> None:
        self._path(symbol).unlink(missing_ok=True)
        self._manifest.pop(symbol, None)
        self._manifest_path.write_text(json.dumps(self._manifest, indent=1))

    def _path(self, symbol: str) -> Path:
        return self.dir / f"{symbol.replace('/', '_').replace('.', '_')}.parquet"
