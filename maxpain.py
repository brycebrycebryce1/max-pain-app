"""Max pain calculation and Yahoo Finance option-chain retrieval.

Pure Python. No AI, no paid API keys.

Max pain theory
---------------
At expiration every option settles at its intrinsic value. The aggregate cash
that option *writers* must pay to *holders* if the underlying settles at price
S is:

    pain(S) = 100 * sum_K CallOI(K) * max(0, S - K)
            + 100 * sum_K PutOI(K)  * max(0, K - S)

The "max pain" price is the S that minimises pain(S) -- the settlement price at
which the least money changes hands from writers to holders, i.e. the price at
which the largest dollar amount of open interest expires worthless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

CONTRACT_MULTIPLIER = 100


# --------------------------------------------------------------------------
# Calculation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MaxPainResult:
    """Outcome of a max pain computation over one option chain."""

    curve: pd.DataFrame          # price, call_pain, put_pain, total_pain, gravity
    max_pain: float              # settlement price minimising total_pain
    min_pain_value: float        # total_pain at max_pain (dollars)
    max_pain_value: float        # worst-case total_pain across evaluated prices
    total_call_oi: int
    total_put_oi: int

    @property
    def put_call_oi_ratio(self) -> float:
        return self.total_put_oi / self.total_call_oi if self.total_call_oi else float("nan")


def compute_pain_curve(
    strikes: np.ndarray,
    call_oi: np.ndarray,
    put_oi: np.ndarray,
    eval_prices: np.ndarray | None = None,
    multiplier: int = CONTRACT_MULTIPLIER,
) -> MaxPainResult:
    """Compute writer payout (``pain``) across candidate settlement prices.

    ``eval_prices`` defaults to the strikes themselves, which is the standard
    definition: the minimum of pain(S) is always attained at a strike because
    pain(S) is piecewise linear with breakpoints only at strikes.
    """
    strikes = np.asarray(strikes, dtype=float)
    call_oi = np.asarray(call_oi, dtype=float)
    put_oi = np.asarray(put_oi, dtype=float)

    if strikes.size == 0:
        raise ValueError("no strikes supplied")
    if not (strikes.shape == call_oi.shape == put_oi.shape):
        raise ValueError("strikes, call_oi and put_oi must be the same length")

    prices = strikes if eval_prices is None else np.asarray(eval_prices, dtype=float)
    prices = np.sort(prices)

    # payout matrices: rows = candidate settlement prices, cols = strikes
    diff = prices[:, None] - strikes[None, :]
    call_pain = np.clip(diff, 0.0, None) @ call_oi * multiplier
    put_pain = np.clip(-diff, 0.0, None) @ put_oi * multiplier
    total = call_pain + put_pain

    lo, hi = float(total.min()), float(total.max())
    span = hi - lo
    # Gravity: 1.0 at the max pain price, 0.0 at the most expensive settlement.
    gravity = np.ones_like(total) if span <= 0 else (hi - total) / span

    curve = pd.DataFrame(
        {
            "price": prices,
            "call_pain": call_pain,
            "put_pain": put_pain,
            "total_pain": total,
            "gravity": gravity,
        }
    )

    return MaxPainResult(
        curve=curve,
        max_pain=float(prices[int(np.argmin(total))]),
        min_pain_value=lo,
        max_pain_value=hi,
        total_call_oi=int(call_oi.sum()),
        total_put_oi=int(put_oi.sum()),
    )


def build_strike_table(calls: pd.DataFrame, puts: pd.DataFrame) -> pd.DataFrame:
    """Merge raw call/put chains into one row per strike with clean OI columns."""
    def _side(df: pd.DataFrame, label: str) -> pd.DataFrame:
        cols = {"strike": "strike", "openInterest": f"{label}_oi", "volume": f"{label}_volume"}
        out = df.reindex(columns=list(cols)).rename(columns=cols)
        # Yahoo omits openInterest for illiquid/deep-ITM strikes; absent OI is 0.
        out[f"{label}_oi"] = pd.to_numeric(out[f"{label}_oi"], errors="coerce").fillna(0.0)
        out[f"{label}_volume"] = pd.to_numeric(out[f"{label}_volume"], errors="coerce").fillna(0.0)
        out["strike"] = pd.to_numeric(out["strike"], errors="coerce")
        return out.dropna(subset=["strike"]).groupby("strike", as_index=False).sum()

    table = _side(calls, "call").merge(_side(puts, "put"), on="strike", how="outer")
    return table.fillna(0.0).sort_values("strike").reset_index(drop=True)


# --------------------------------------------------------------------------
# Data retrieval (Yahoo Finance via yfinance)
# --------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def make_session():
    """A plain ``requests`` session for yfinance.

    yfinance defaults to curl_cffi, whose certificate handling is broken on some
    Windows installs; ``requests`` uses certifi and works everywhere.
    """
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json,text/html,*/*"})
    return session


def _retry(fn, attempts: int = 4, base_delay: float = 1.5):
    """Retry ``fn`` through Yahoo's rate limiter with exponential backoff."""
    from yfinance.exceptions import YFRateLimitError

    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except YFRateLimitError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise RuntimeError(
        "Yahoo Finance is rate limiting this machine. Wait a minute and retry."
    ) from last


def get_ticker(symbol: str, session=None):
    import yfinance as yf

    return yf.Ticker(symbol.strip().upper(), session=session or make_session())


def fetch_expirations(symbol: str, session=None) -> list[str]:
    ticker = get_ticker(symbol, session)
    expirations = list(_retry(lambda: ticker.options))
    if not expirations:
        raise ValueError(f"No listed options found for '{symbol.upper()}'.")
    return expirations


def fetch_spot(symbol: str, session=None) -> float:
    ticker = get_ticker(symbol, session)

    def _read() -> float:
        info = ticker.fast_info
        for key in ("lastPrice", "last_price", "regularMarketPrice", "previousClose"):
            try:
                value = info[key]
            except (KeyError, TypeError):
                continue
            if value:
                return float(value)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"No price data for '{symbol.upper()}'.")
        return float(hist["Close"].iloc[-1])

    return _retry(_read)


def fetch_chain(symbol: str, expiration: str, session=None) -> pd.DataFrame:
    """Return the merged per-strike open-interest table for one expiration."""
    ticker = get_ticker(symbol, session)
    chain = _retry(lambda: ticker.option_chain(expiration))
    table = build_strike_table(chain.calls, chain.puts)
    if table.empty:
        raise ValueError(f"Empty option chain for {symbol.upper()} {expiration}.")
    return table
