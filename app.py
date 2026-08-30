"""Streamlit max pain explorer -- enter a ticker, get a max pain gravity diagram.

Runs entirely offline of any AI service: yfinance for data, pandas/numpy for the
maths, plotly for the chart.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import maxpain

st.set_page_config(page_title="Max Pain Visualiser", layout="wide")

CALL_COLOR = "#2e86de"
PUT_COLOR = "#e74c3c"
GRAVITY_COLOR = "#8e44ad"


# --------------------------------------------------------------------------
# Cached data access (TTL keeps Yahoo's rate limiter happy)
# --------------------------------------------------------------------------

@st.cache_resource
def _session():
    return maxpain.make_session()


@st.cache_data(ttl=300, show_spinner=False)
def load_expirations(symbol: str) -> list[str]:
    return maxpain.fetch_expirations(symbol, _session())


@st.cache_data(ttl=300, show_spinner=False)
def load_spot(symbol: str) -> float:
    return maxpain.fetch_spot(symbol, _session())


@st.cache_data(ttl=300, show_spinner=False)
def load_chain(symbol: str, expiration: str) -> pd.DataFrame:
    return maxpain.fetch_chain(symbol, expiration, _session())


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

st.sidebar.header("Settings")
symbol = st.sidebar.text_input("Ticker", value="NVDA").strip().upper()

if not symbol:
    st.info("Enter a ticker symbol in the sidebar to begin.")
    st.stop()

try:
    expirations = load_expirations(symbol)
    spot = load_spot(symbol)
except Exception as exc:  # noqa: BLE001 - surface any data problem to the user
    st.error(f"Could not load data for **{symbol}**: {exc}")
    st.stop()

chosen = st.sidebar.multiselect(
    "Expiration(s)",
    expirations,
    default=[expirations[0]],
    help="Selecting several expirations aggregates their open interest.",
)
if not chosen:
    st.warning("Select at least one expiration.")
    st.stop()

window = st.sidebar.slider(
    "Strike window (% around spot)", min_value=5, max_value=100, value=30, step=5,
    help="Narrows both the chart and, optionally, the strikes included in the calculation.",
)
use_window = st.sidebar.checkbox("Limit calculation to this window", value=False)
if st.sidebar.button("Refresh data"):
    st.cache_data.clear()
    st.rerun()

# --------------------------------------------------------------------------
# Data + calculation
# --------------------------------------------------------------------------

try:
    with st.spinner(f"Fetching option chains for {symbol}..."):
        frames = [load_chain(symbol, exp) for exp in chosen]
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load option chain: {exc}")
    st.stop()

table = (
    pd.concat(frames, ignore_index=True)
    .groupby("strike", as_index=False)
    .sum()
    .sort_values("strike")
    .reset_index(drop=True)
)

lo, hi = spot * (1 - window / 100), spot * (1 + window / 100)
calc_table = table[table.strike.between(lo, hi)] if use_window else table
if calc_table.empty:
    st.error("No strikes fall inside the selected window. Widen it.")
    st.stop()

result = maxpain.compute_pain_curve(
    calc_table.strike.to_numpy(), calc_table.call_oi.to_numpy(), calc_table.put_oi.to_numpy()
)
curve = result.curve
view = curve[curve.price.between(lo, hi)]
if view.empty:
    view = curve

# --------------------------------------------------------------------------
# Header metrics
# --------------------------------------------------------------------------

st.title(f"{symbol} - Max Pain")
st.caption(
    f"{len(chosen)} expiration(s): {', '.join(chosen)} - "
    f"{len(calc_table)} strikes - open interest sourced from Yahoo Finance "
    "(OI settles overnight, so it reflects the prior session's close)."
)

drift = (result.max_pain - spot) / spot * 100
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Spot price", f"${spot:,.2f}")
c2.metric("Max pain", f"${result.max_pain:,.2f}", f"{drift:+.2f}% from spot")
c3.metric("Call OI", f"{result.total_call_oi:,}")
c4.metric("Put OI", f"{result.total_put_oi:,}")
c5.metric("Put/Call OI", f"{result.put_call_oi_ratio:.2f}")

# --------------------------------------------------------------------------
# Gravity diagram
# --------------------------------------------------------------------------

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=view.price, y=view.gravity, name="Gravity", mode="lines",
        line=dict(color=GRAVITY_COLOR, width=3, shape="spline"),
        fill="tozeroy", fillcolor="rgba(142, 68, 173, 0.15)",
        customdata=view.total_pain,
        hovertemplate="Price $%{x:,.2f}<br>Gravity %{y:.4f}"
                      "<br>Writer payout $%{customdata:,.0f}<extra></extra>",
    )
)
fig.add_vline(
    x=result.max_pain, line=dict(color=GRAVITY_COLOR, width=2, dash="dash"),
    annotation_text=f"Max pain ${result.max_pain:,.2f}", annotation_position="top left",
)
fig.add_vline(
    x=spot, line=dict(color="#7f8c8d", width=2, dash="dot"),
    annotation_text=f"Spot ${spot:,.2f}", annotation_position="top right",
)
fig.update_layout(
    height=470, margin=dict(t=60, b=40, l=10, r=10), hovermode="x unified",
    title="Max pain gravity - how strongly each price pulls the underlying",
    xaxis_title="Underlying price at expiration ($)",
    yaxis_title="Gravity (1.0 = maximum pull)",
    yaxis=dict(range=[0, 1.05]), showlegend=False,
)
st.plotly_chart(fig, width="stretch")

with st.expander("What is gravity here?"):
    st.markdown(
        "**Pain** is the cash option writers owe holders if the stock settles at a "
        "given price:"
    )
    st.latex(
        r"\text{pain}(S) = 100\sum_K \text{CallOI}(K)\,\max(0,\, S-K)"
        r" \;+\; 100\sum_K \text{PutOI}(K)\,\max(0,\, K-S)"
    )
    st.markdown(
        "**Gravity** rescales that curve so it is easy to read: "
        "`gravity = (pain_max - pain(S)) / (pain_max - pain_min)`. "
        "It equals **1.0** at the max pain price (the cheapest settlement for writers, "
        "the price they are theoretically motivated to pin) and **0.0** at the most "
        "expensive settlement. A tall narrow peak means the pull concentrates on a "
        "single price; a broad plateau means the chain barely favours one level.\n\n"
        f"At max pain, writers pay **\\${result.min_pain_value:,.0f}**; at the worst "
        f"evaluated price they pay **\\${result.max_pain_value:,.0f}**."
    )

# --------------------------------------------------------------------------
# Supporting charts
# --------------------------------------------------------------------------

left, right = st.columns(2)

with left:
    st.subheader("Writer payout by settlement price")
    bars = go.Figure()
    bars.add_bar(x=view.price, y=view.call_pain, name="Calls", marker_color=CALL_COLOR)
    bars.add_bar(x=view.price, y=view.put_pain, name="Puts", marker_color=PUT_COLOR)
    bars.add_vline(x=result.max_pain, line=dict(color=GRAVITY_COLOR, width=2, dash="dash"))
    bars.update_layout(
        barmode="stack", height=380, margin=dict(t=30, b=70, l=10, r=10),
        xaxis_title="Settlement price ($)", yaxis_title="Payout owed ($)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.35, x=0),
    )
    st.plotly_chart(bars, width="stretch")

with right:
    st.subheader("Open interest by strike")
    oi_view = calc_table[calc_table.strike.between(lo, hi)]
    if oi_view.empty:
        oi_view = calc_table
    oi = go.Figure()
    oi.add_bar(x=oi_view.strike, y=oi_view.call_oi, name="Call OI", marker_color=CALL_COLOR)
    oi.add_bar(x=oi_view.strike, y=-oi_view.put_oi, name="Put OI", marker_color=PUT_COLOR)
    oi.add_vline(x=result.max_pain, line=dict(color=GRAVITY_COLOR, width=2, dash="dash"))
    oi.update_layout(
        barmode="relative", height=380, margin=dict(t=30, b=70, l=10, r=10),
        xaxis_title="Strike ($)", yaxis_title="Contracts (puts plotted downward)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.35, x=0),
    )
    st.plotly_chart(oi, width="stretch")

with st.expander("Underlying data"):
    merged = calc_table.merge(
        curve[["price", "total_pain", "gravity"]], left_on="strike", right_on="price", how="left"
    ).drop(columns="price")
    st.dataframe(
        merged.style.format(
            {
                "strike": "{:,.2f}", "call_oi": "{:,.0f}", "put_oi": "{:,.0f}",
                "call_volume": "{:,.0f}", "put_volume": "{:,.0f}",
                "total_pain": "${:,.0f}", "gravity": "{:.3f}",
            }
        ),
        width="stretch",
        height=340,
    )
    st.download_button(
        "Download CSV", merged.to_csv(index=False).encode(),
        file_name=f"{symbol}_maxpain.csv", mime="text/csv",
    )

st.caption(
    "Educational tool, not investment advice. Max pain is a descriptive statistic "
    "about open interest, not a forecast."
)
