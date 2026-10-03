"""
Omnicampus Leaderboard — public viewer
======================================

Reads the leaderboard of an Omnicampus assignment, loads it into a pandas
DataFrame and draws a scatter plot (rank × score) with the top-20% band hatched.

How Omnicampus works under the hood:
  1. The page /en/courses/<course>/assignments/<assignment>/leader-board/ is a
     Next.js app. When the browser sends the session cookie, the server embeds
     an access token (JWT, ~24 h) inside <script id="__NEXT_DATA__">.
  2. The browser then calls the JSON API
     /api/v3/courses/<course>/assignments/<assignment>/leader-board
     with the header "Authorization: Bearer <token>".

Credentials (NEVER hard-code them — use st.secrets or environment variables):
  OMNI_COOKIE        full Cookie header string of a logged-in session
                     (must contain the appSession cookie). Preferred: the app
                     refreshes the token by itself while the session is valid.
  OMNI_ACCESS_TOKEN  alternative: the JWT itself (expires in ~24 h).

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import time
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
import streamlit as st  # noqa: E402

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = os.environ.get("OMNI_BASE_URL", "https://edu.omnicamp.us")


def cfg(name: str, default: str | None = None) -> str | None:
    """Read from st.secrets (Streamlit Cloud), falling back to environment variables."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets.toml
        pass
    return os.environ.get(name, default)


COURSE_ID = cfg("OMNI_COURSE_ID", "170")
ASSIGNMENT_ID = cfg("OMNI_ASSIGNMENT_ID", "1038")
TOP_FRACTION = float(cfg("TOP_FRACTION", "0.20"))
SHOW_NAMES = cfg("SHOW_NAMES", "true").lower() in ("1", "true", "yes")
CACHE_TTL = int(cfg("CACHE_TTL_SECONDS", "300"))  # automatic refresh every 5 min

PAGE_URL = f"{BASE_URL}/en/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}/leader-board/"
API_URL = f"{BASE_URL}/api/v3/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}/leader-board"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def jwt_exp(token: str) -> float:
    """Expiry time (epoch) of a JWT, without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return 0.0


def parse_cookie_header(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:]
    jar = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


@st.cache_resource
def http_session() -> requests.Session:
    """Persistent HTTP session: keeps the session cookie (and its renewals)."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    cookie = cfg("OMNI_COOKIE")
    if cookie:
        s.cookies.update(parse_cookie_header(cookie))
    return s


@st.cache_resource
def token_box() -> dict:
    return {"token": None, "exp": 0.0}


def token_from_page(session: requests.Session) -> str:
    """Open the leaderboard page with the cookie and extract accessToken from __NEXT_DATA__."""
    r = session.get(PAGE_URL, timeout=30, allow_redirects=False)
    if r.is_redirect:
        loc = r.headers.get("Location", "")
        if "login" in loc or "auth0.com" in loc:
            raise AuthError("The session cookie has expired or is invalid (redirected to login).")
        r = session.get(requests.compat.urljoin(PAGE_URL, loc), timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise AuthError("Could not find __NEXT_DATA__ on the page (did the site layout change?).")
    props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    token = props.get("accessToken")
    if not token:
        raise AuthError("The page returned no accessToken — the cookie is not authenticated.")
    return token


def get_token(force_refresh: bool = False) -> str:
    box = token_box()
    if not force_refresh and box["token"] and box["exp"] - time.time() > 120:
        return box["token"]

    if cfg("OMNI_COOKIE"):
        token = token_from_page(http_session())
    elif cfg("OMNI_ACCESS_TOKEN"):
        token = cfg("OMNI_ACCESS_TOKEN").strip()
        if token.lower().startswith("bearer "):
            token = token[7:]
        if jwt_exp(token) and jwt_exp(token) < time.time():
            raise AuthError("OMNI_ACCESS_TOKEN has expired — get a new one or use OMNI_COOKIE.")
    else:
        raise AuthError("No credentials configured (OMNI_COOKIE or OMNI_ACCESS_TOKEN).")

    box["token"], box["exp"] = token, jwt_exp(token) or time.time() + 3600
    return token


# --------------------------------------------------------------------------
# Scraping → DataFrame
# --------------------------------------------------------------------------
def fetch_raw() -> list[dict]:
    for attempt in range(2):
        token = get_token(force_refresh=attempt > 0)
        r = http_session().get(
            API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Auth-Token": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        if r.status_code == 401 and attempt == 0:
            continue  # stale token: try to refresh once
        if r.status_code == 401:
            raise AuthError("The API rejected the token (401).")
        r.raise_for_status()
        return r.json()
    return []


def to_dataframe(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for item in raw:
        user = item.get("user") or {}
        rows.append(
            {
                "participant": user.get("accountName") or user.get("githubId") or "?",
                "score": item.get("score"),
                "status": item.get("status"),
                "submissions": item.get("submitCount"),
                "first_submission": item.get("createdAt"),
                "last_update": item.get("updatedAt"),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    for c in ("first_submission", "last_update"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    # official rank = API order (already sorted by score)
    df.insert(0, "rank", np.arange(1, len(df) + 1))
    return df


@st.cache_data(ttl=CACHE_TTL, show_spinner="Reading the leaderboard…")
def load_leaderboard() -> tuple[pd.DataFrame, datetime]:
    return to_dataframe(fetch_raw()), datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def valid_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Drop invalid submissions (negative score, e.g. -999 = failed, or missing)."""
    return df[df["score"].notna() & (df["score"] >= 0)].copy()


def cutoff_top(df_valid: pd.DataFrame, fraction: float) -> tuple[float, int]:
    """
    Cutoff score to be among the top `fraction` of participants.
    k = ceil(fraction · N); the cutoff is the score of the k-th ranked participant.
    Anyone tied with that score is also inside the band.
    """
    scores = np.sort(df_valid["score"].to_numpy())[::-1]
    k = max(1, math.ceil(fraction * len(scores)))
    return float(scores[k - 1]), k


def make_figure(df: pd.DataFrame, cutoff: float, fraction: float, highlight: str | None,
                zoom: bool):
    fig, ax = plt.subplots(figsize=(11, 6), dpi=110)
    top = df["score"] >= cutoff

    ymax = df["score"].max()
    ymin = float(df["score"].quantile(0.05)) if zoom else float(df["score"].min())
    pad = (ymax - ymin) * 0.06 or 0.01
    ylo, yhi = ymin - pad, ymax + pad

    # hatched top band
    ax.axhspan(cutoff, yhi, facecolor="#2a9d8f", alpha=0.10, edgecolor="#2a9d8f",
               hatch="///", linewidth=0, zorder=0,
               label=f"Top {fraction:.0%} (score ≥ {cutoff:.5f})")
    ax.axhline(cutoff, color="#2a9d8f", lw=1.6, ls="--", zorder=1)
    ax.annotate(f"cutoff: {cutoff:.5f}", xy=(df["rank"].max(), cutoff),
                xytext=(-4, 5), textcoords="offset points", ha="right",
                color="#1d6f65", fontsize=10, fontweight="bold")

    ax.scatter(df.loc[~top, "rank"], df.loc[~top, "score"], s=18, color="#8d99ae",
               alpha=0.85, label="Other participants", zorder=2)
    ax.scatter(df.loc[top, "rank"], df.loc[top, "score"], s=22, color="#264653",
               label=f"Within the top {fraction:.0%}", zorder=3)

    if highlight:
        hit = df[df["participant"].str.lower() == highlight.lower()]
        if not hit.empty:
            r = hit.iloc[0]
            ax.scatter([r["rank"]], [r["score"]], s=140, facecolor="none",
                       edgecolor="#e76f51", linewidth=2.2, zorder=4)
            ax.annotate(f"{r['participant']}\n#{int(r['rank'])} · {r['score']:.5f}",
                        xy=(r["rank"], r["score"]), xytext=(12, -28),
                        textcoords="offset points", color="#c4472b", fontsize=9,
                        arrowprops=dict(arrowstyle="-", color="#e76f51"))

    ax.set_ylim(ylo, yhi)
    ax.set_xlim(0, df["rank"].max() + 2)
    ax.set_xlabel("Leaderboard rank")
    ax.set_ylabel("Score")
    ax.set_title(f"Leaderboard — course {COURSE_ID}, assignment {ASSIGNMENT_ID} "
                 f"({len(df)} participants with a valid score)", fontsize=12)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", framealpha=0.9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Omnicampus Leaderboard", page_icon="📊", layout="wide")
    st.title("Omnicampus Leaderboard")

    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("🔄 Reload", type="primary", width="stretch"):
            load_leaderboard.clear()
    with c2:
        st.caption(f"Data is also refreshed automatically every {CACHE_TTL // 60} min.")

    try:
        df, fetched_at = load_leaderboard()
    except AuthError as e:
        st.error(f"Authentication failed: {e}")
        st.info("Update the OMNI_COOKIE (or OMNI_ACCESS_TOKEN) secret — see the README.")
        st.stop()
    except requests.RequestException as e:
        st.error(f"Could not reach Omnicampus: {e}")
        st.stop()

    if df.empty:
        st.warning("The leaderboard is empty.")
        st.stop()

    dv = valid_scores(df)
    cutoff, k = cutoff_top(dv, TOP_FRACTION)
    n_in_band = int((dv["score"] >= cutoff).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(f"Cutoff score (top {TOP_FRACTION:.0%})", f"{cutoff:.5f}")
    m2.metric("Participants with a valid score", len(dv),
              help=f"{len(df) - len(dv)} invalid submissions (negative score) were ignored.")
    m3.metric("Best score", f"{dv['score'].max():.5f}")
    m4.metric("Median", f"{dv['score'].median():.5f}")

    st.caption(
        f"Cutoff = score of rank #{k} (⌈{TOP_FRACTION:.0%} × {len(dv)}⌉). "
        f"Including ties, {n_in_band} participants are inside the band. "
        f"{round((1 - TOP_FRACTION) * 100)}th percentile (interpolated): "
        f"{dv['score'].quantile(1 - TOP_FRACTION):.5f}. "
        f"Fetched at {fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC."
    )

    o1, o2 = st.columns([3, 1])
    with o1:
        names = sorted(dv["participant"].tolist(), key=str.lower) if SHOW_NAMES else []
        highlight = (st.selectbox("Highlight a participant", [""] + names,
                                  format_func=lambda x: x or "— none —")
                     if SHOW_NAMES else None)
    with o2:
        zoom = st.toggle("Zoom (hide the bottom 5%)", value=True)

    if highlight:
        r = dv[dv["participant"] == highlight].iloc[0]
        gap = cutoff - r["score"]
        if gap <= 0:
            st.success(f"**{highlight}** is ranked #{int(r['rank'])} — within the top "
                       f"{TOP_FRACTION:.0%}.")
        else:
            st.warning(f"**{highlight}** is ranked #{int(r['rank'])} — "
                       f"{gap:.5f} points below the cutoff.")

    plot_df = dv if SHOW_NAMES else dv.assign(participant="")
    st.pyplot(make_figure(plot_df, cutoff, TOP_FRACTION, highlight or None, zoom))

    with st.expander("Full table", expanded=False):
        show = df.copy()
        show[f"top_{round(TOP_FRACTION * 100)}"] = show["score"] >= cutoff
        if not SHOW_NAMES:
            show = show.drop(columns=["participant"])
        st.dataframe(show, hide_index=True, width="stretch")
        st.download_button("Download CSV", show.to_csv(index=False).encode("utf-8"),
                           file_name="leaderboard.csv", mime="text/csv")


if __name__ == "__main__":
    main()
