# Max Pain Visualiser

A Streamlit web app: type a ticker (`NVDA`, `SPY`, `AAPL`, …) and get a max pain gravity diagram built from live option-chain open interest.
Made with `yfinance`, `pandas`/`numpy` and `plotly`.

## Run

```bash
pip install -r requirements.txt
```

```bash
python -m streamlit run app.py
```

## The chart

- **x axis** — the underlying's price at expiration.
- **y axis** — *gravity*, how strongly that price pulls the underlying, on a 0–1 scale. It peaks at **1.0** at the max pain price.

Dashed purple line = max pain. Dotted grey line = current spot.

Below it: writer payout split into calls vs puts (a V whose bottom sits exactly on the max pain price), and open interest per strike with puts drawn downward.

## The maths

At expiration every option settles at intrinsic value, so the cash option *writers* owe *holders* if the stock settles at price `S` is:

```
pain(S) = 100 * Σ_K CallOI(K) * max(0, S − K)
        + 100 * Σ_K PutOI(K)  * max(0, K − S)
```

**Max pain** is the `S` minimising `pain(S)` — the settlement price at which the largest dollar amount of open interest expires worthless.

`pain(S)` is piecewise linear with breakpoints only at strikes, so its minimum is always attained *at a strike*. The app therefore evaluates the curve on the strike grid, which is exact rather than an approximation.

**Gravity** is that curve rescaled for readability:

```
gravity(S) = (pain_max − pain(S)) / (pain_max − pain_min)
```

A tall narrow peak means the pull is concentrated on one price; a broad plateau means the chain barely favours any level.

## Accuracy notes

- Open interest comes from Yahoo Finance and settles overnight — it reflects
  the prior session's close, which is the standard input for max pain. Volume is
  shown alongside it in the data table but is deliberately *not* used in the
  calculation.
- Yahoo omits `openInterest` for illiquid and deep-ITM strikes; those are treated
  as `0`, not dropped, so the strike grid stays intact.
- Calls and puts are outer-joined on strike, so a strike listed on only one side
  still contributes.
- Contract multiplier is 100.
- By default **every** listed strike feeds the calculation. The sidebar's strike
  window only crops the view unless you tick *Limit calculation to this window* —
  cropping the input changes the answer, so it is opt-in.
- Selecting several expirations sums their open interest per strike.
- Results are cached for 5 minutes to stay under Yahoo's rate limiter; the
  *Refresh data* button clears the cache.


## Files

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI and charts |
| `maxpain.py` | Max pain maths + Yahoo Finance retrieval (no Streamlit imports) |

## Note on `yfinance` and TLS

`yfinance` defaults to `curl_cffi`, whose certificate verification is broken on
some Windows installs (`CertificateVerifyError`). `maxpain.make_session()` hands
`yfinance` a plain `requests` session instead, which uses `certifi` and works
everywhere.

---

Educational tool, not investment advice. Max pain is a descriptive statistic
about open interest, not a forecast.
