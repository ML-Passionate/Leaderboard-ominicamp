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
import threading
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
# How often the app re-opens the leaderboard page to keep the session alive.
# The site renews its session cookie on each visit (like a browser does);
# without these visits the copied cookie expires after about a day.
KEEPALIVE_SECONDS = int(cfg("KEEPALIVE_SECONDS", "1800"))

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
        jar = parse_cookie_header(cookie)
        # Drop analytics/tracking cookies (_ga, __utm*, cwr_*, ...): they are
        # useless here and make the header large. Everything else is kept, so
        # the session cookie survives whatever its name is.
        noise = ("_ga", "_gid", "_gat", "__utm", "cwr_", "_fbp", "_gcl", "_hj",
                 "ajs_", "mp_", "intercom")
        host = requests.utils.urlparse(BASE_URL).hostname
        for k, v in jar.items():
            if not k.startswith(noise):
                s.cookies.set(k, v, domain=host, path="/")
    return s


@st.cache_resource
def token_box() -> dict:
    return {"token": None, "exp": 0.0, "page_at": 0.0, "lock": threading.Lock(),
            "keepalive_error": None}


def adopt_renewed_cookies(session: requests.Session, response: requests.Response) -> None:
    """Make cookies renewed by the server (Set-Cookie) replace the old values.

    Without this, a renewed cookie can be stored next to the original one
    (different cookie domain) and both would be sent.
    """
    for c in response.cookies:
        for old in [o for o in session.cookies if o.name == c.name]:
            session.cookies.clear(old.domain, old.path, old.name)
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path or "/")


def token_from_page(session: requests.Session) -> str:
    """Open the leaderboard page with the cookie and extract accessToken from __NEXT_DATA__."""
    r = session.get(PAGE_URL, timeout=30, allow_redirects=False)
    adopt_renewed_cookies(session, r)
    if r.is_redirect:
        loc = r.headers.get("Location", "")
        if "login" in loc or "auth0.com" in loc:
            raise AuthError("The session cookie has expired or is invalid (redirected to login).")
        r = session.get(requests.compat.urljoin(PAGE_URL, loc), timeout=30)
        adopt_renewed_cookies(session, r)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise AuthError("Could not find __NEXT_DATA__ on the page (did the site layout change?).")
    props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    token = props.get("accessToken")
    if not token:
        raise AuthError("The page returned no accessToken — the session cookie has expired "
                        "or is incomplete. Copy a fresh cookie from a logged-in browser.")
    return token


def refresh_from_page(session: requests.Session, box: dict) -> str:
    """Visit the page (renewing the session cookie) and store the fresh token."""
    with box["lock"]:
        token = token_from_page(session)  # requests stores the renewed Set-Cookie
        box["token"], box["exp"] = token, jwt_exp(token) or time.time() + 3600
        box["page_at"] = time.time()
        box["keepalive_error"] = None
        return token


def get_token(force_refresh: bool = False) -> str:
    box = token_box()
    use_cookie = bool(cfg("OMNI_COOKIE"))
    fresh_page = time.time() - box["page_at"] < KEEPALIVE_SECONDS
    if (not force_refresh and box["token"] and box["exp"] - time.time() > 120
            and (fresh_page or not use_cookie)):
        return box["token"]

    if use_cookie:
        return refresh_from_page(http_session(), box)
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


@st.cache_resource
def start_keepalive() -> threading.Thread:
    """Background thread that re-visits the page every KEEPALIVE_SECONDS.

    It keeps the Omnicampus session rolling even when nobody opens the app.
    It runs only while the Streamlit server is up; if the app goes to sleep
    or restarts, it starts again from the cookie stored in the secrets.
    """
    session, box = http_session(), token_box()

    def loop() -> None:
        while True:
            time.sleep(KEEPALIVE_SECONDS)
            try:
                refresh_from_page(session, box)
            except Exception as e:  # keep trying; the UI shows the error
                box["keepalive_error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=loop, name="omni-keepalive", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# Scraping → DataFrame
# --------------------------------------------------------------------------
def fetch_raw() -> list[dict]:
    for attempt in range(2):
        token = get_token(force_refresh=attempt > 0)
        # The API only needs the Bearer token: send NO cookies here
        # (cookies + token together exceed the server's header limit → 431).
        r = requests.get(
            API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Auth-Token": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
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




# --------------------------------------------------------------------------
# Visual design
# --------------------------------------------------------------------------
INK = "#0b0b0b"          # primary text
INK_2 = "#52514e"        # secondary text
INK_3 = "#8a8983"        # muted text / axes
SURFACE = "#fcfcfb"      # chart surface
GRID = "#e9e8e4"
ACCENT = "#2a78d6"       # top band (blue)
OTHER = "#b4b3ad"        # everyone else (neutral)
HIGHLIGHT = "#eb6834"    # selected participant (orange)
FOOTER_AUTHOR = cfg("FOOTER_AUTHOR", "ricardomonteiro")

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stApp {{ font-family: 'Inter', system-ui, sans-serif; }}
.stApp {{ background: #f6f6f4; }}
#MainMenu, header[data-testid="stHeader"], footer, .stDeployButton {{ visibility: hidden; height: 0; }}
.block-container {{ padding-top: 2.2rem; padding-bottom: 5rem; max-width: 1180px; }}

.lb-eyebrow {{ color: {ACCENT}; font-weight: 600; font-size: .78rem; letter-spacing: .08em;
              text-transform: uppercase; margin-bottom: .25rem; }}
.lb-title {{ color: {INK}; font-size: 2.1rem; font-weight: 700; line-height: 1.15; margin: 0; }}
.lb-sub {{ color: {INK_2}; font-size: .95rem; margin-top: .35rem; }}

.lb-cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px;
            margin: 1.4rem 0 1rem; }}
@media (max-width: 800px) {{ .lb-cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
.lb-card {{ background: #fff; border: 1px solid #e7e6e1; border-radius: 14px; padding: 16px 18px; }}
.lb-card.hero {{ background: {ACCENT}; border-color: {ACCENT}; }}
.lb-card .k {{ color: {INK_2}; font-size: .8rem; font-weight: 500; }}
.lb-card .v {{ color: {INK}; font-size: 1.75rem; font-weight: 700; margin-top: 4px;
              font-variant-numeric: tabular-nums; }}
.lb-card .n {{ color: {INK_3}; font-size: .75rem; margin-top: 2px; }}
.lb-card.hero .k, .lb-card.hero .n {{ color: rgba(255,255,255,.85); }}
.lb-card.hero .v {{ color: #fff; }}

.lb-note {{ color: {INK_2}; font-size: .82rem; margin: .2rem 0 1rem; }}

div[data-testid="stVerticalBlockBorderWrapper"] {{ background: #fff; border-radius: 14px; }}
.stButton > button[kind="primary"] {{ background: {INK}; border: 1px solid {INK}; border-radius: 10px;
                                     font-weight: 600; padding: .55rem 1rem; }}
.stButton > button[kind="primary"]:hover {{ background: #2b2b2a; border-color: #2b2b2a; }}

.lb-footer {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 100; text-align: center;
             padding: 10px 16px; background: rgba(246,246,244,.92); backdrop-filter: blur(6px);
             border-top: 1px solid #e7e6e1; color: {INK_2}; font-size: .82rem; }}
.lb-footer b {{ color: {INK}; font-weight: 600; }}
</style>
"""


def card(label: str, value: str, note: str = "", hero: bool = False) -> str:
    return (f'<div class="lb-card{" hero" if hero else ""}"><div class="k">{label}</div>'
            f'<div class="v">{value}</div><div class="n">{note}</div></div>')


def make_figure(df: pd.DataFrame, cutoff: float, fraction: float, highlight: str | None,
                zoom: bool):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    top = df["score"] >= cutoff

    ymax = df["score"].max()
    ymin = float(df["score"].quantile(0.05)) if zoom else float(df["score"].min())
    pad = (ymax - ymin) * 0.06 or 0.01
    ylo, yhi = ymin - pad, ymax + pad

    # hatched top band
    ax.axhspan(cutoff, yhi, facecolor=ACCENT, alpha=0.07, zorder=0)
    ax.axhspan(cutoff, yhi, facecolor="none", edgecolor=ACCENT, alpha=0.35,
               hatch="////", linewidth=0, zorder=0)
    ax.axhline(cutoff, color=ACCENT, lw=1.5, ls=(0, (5, 3)), zorder=1)
    ax.annotate(f"Top {fraction:.0%} cutoff  {cutoff:.5f}", xy=(df["rank"].max(), cutoff),
                xytext=(0, 6), textcoords="offset points", ha="right", va="bottom",
                color=INK, fontsize=10, fontweight="bold")

    ax.scatter(df.loc[~top, "rank"], df.loc[~top, "score"], s=22, color=OTHER,
               edgecolor=SURFACE, linewidth=0.6, label="Other participants", zorder=2)
    ax.scatter(df.loc[top, "rank"], df.loc[top, "score"], s=26, color=ACCENT,
               edgecolor=SURFACE, linewidth=0.6, label=f"Top {fraction:.0%}", zorder=3)

    if highlight:
        hit = df[df["participant"].str.lower() == highlight.lower()]
        if not hit.empty:
            r = hit.iloc[0]
            ax.scatter([r["rank"]], [r["score"]], s=150, color=HIGHLIGHT,
                       edgecolor=SURFACE, linewidth=2, zorder=5, label=r["participant"])
            ax.annotate(f"{r['participant']}  #{int(r['rank'])} · {r['score']:.5f}",
                        xy=(r["rank"], r["score"]), xytext=(14, -22),
                        textcoords="offset points", color=INK, fontsize=9.5,
                        fontweight="bold",
                        arrowprops=dict(arrowstyle="-", color=INK_3, lw=1))

    ax.set_ylim(ylo, yhi)
    ax.set_xlim(-2, df["rank"].max() + 4)
    ax.set_xlabel("Leaderboard rank", color=INK_2, labelpad=8)
    ax.set_ylabel("Score", color=INK_2, labelpad=8)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_3, length=0, pad=6)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    leg = ax.legend(loc="upper right", frameon=False, labelcolor=INK_2,
                    bbox_to_anchor=(1, 1.09), ncol=3, handletextpad=0.3, columnspacing=1.4)
    for h in leg.legend_handles:
        h.set_sizes([40])
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Omnicampus Leaderboard", page_icon="📊", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        f'<div class="lb-footer">Courtesy of <b>{FOOTER_AUTHOR}</b></div>',
        unsafe_allow_html=True,
    )

    head_l, head_r = st.columns([5, 1], vertical_alignment="bottom")
    with head_l:
        st.markdown(
            f'<div class="lb-eyebrow">Course {COURSE_ID} · Assignment {ASSIGNMENT_ID}</div>'
            f'<h1 class="lb-title">Omnicampus Leaderboard</h1>'
            f'<div class="lb-sub">Live scores, with the cutoff for the top '
            f'{TOP_FRACTION:.0%} of participants.</div>',
            unsafe_allow_html=True,
        )
    with head_r:
        if st.button("↻  Reload", type="primary", width="stretch"):
            load_leaderboard.clear()

    if cfg("OMNI_COOKIE"):
        start_keepalive()

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
    n_invalid = len(df) - len(dv)

    st.markdown(
        '<div class="lb-cards">'
        + card(f"Top {TOP_FRACTION:.0%} cutoff", f"{cutoff:.5f}", f"score of rank #{k}", hero=True)
        + card("Participants", f"{len(dv)}",
               f"{n_invalid} invalid ignored" if n_invalid else "all scores valid")
        + card("Best score", f"{dv['score'].max():.5f}", dv.iloc[0]["participant"] if SHOW_NAMES else "")
        + card("Median score", f"{dv['score'].median():.5f}", f"{n_in_band} inside the band")
        + "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="lb-note">Cutoff = score of rank #{k} '
        f'(⌈{TOP_FRACTION:.0%} × {len(dv)}⌉); ties with the cutoff are included. '
        f'{round((1 - TOP_FRACTION) * 100)}th percentile (interpolated): '
        f'{dv["score"].quantile(1 - TOP_FRACTION):.5f}. '
        f'Updated {fetched_at.strftime("%Y-%m-%d %H:%M")} UTC · auto-refresh every '
        f'{CACHE_TTL // 60} min.</div>',
        unsafe_allow_html=True,
    )

    tab_chart, tab_table = st.tabs(["Chart", "Table"])

    with tab_chart:
        o1, o2 = st.columns([3, 1], vertical_alignment="bottom")
        with o1:
            names = sorted(dv["participant"].tolist(), key=str.lower) if SHOW_NAMES else []
            highlight = (st.selectbox("Find a participant", [""] + names,
                                      format_func=lambda x: x or "Type or pick a name…")
                         if SHOW_NAMES else None)
        with o2:
            zoom = st.toggle("Zoom (hide the bottom 5%)", value=True)

        if highlight:
            r = dv[dv["participant"] == highlight].iloc[0]
            gap = cutoff - r["score"]
            pct = 100 * (r["rank"] - 1) / max(1, len(dv) - 1)
            if gap <= 0:
                st.success(f"**{highlight}** is ranked **#{int(r['rank'])}** of {len(dv)} "
                           f"(top {max(pct, 0.1):.1f}%) — inside the top {TOP_FRACTION:.0%}.")
            else:
                st.warning(f"**{highlight}** is ranked **#{int(r['rank'])}** of {len(dv)} — "
                           f"**{gap:.5f}** points below the cutoff.")

        plot_df = dv if SHOW_NAMES else dv.assign(participant="")
        with st.container(border=True):
            st.pyplot(make_figure(plot_df, cutoff, TOP_FRACTION, highlight or None, zoom),
                      width="stretch")

    with tab_table:
        show = df.copy()
        show[f"top_{round(TOP_FRACTION * 100)}"] = show["score"] >= cutoff
        if not SHOW_NAMES:
            show = show.drop(columns=["participant"])
        st.dataframe(
            show, hide_index=True, width="stretch", height=520,
            column_config={
                "rank": st.column_config.NumberColumn("Rank", width="small"),
                "participant": st.column_config.TextColumn("Participant"),
                "score": st.column_config.NumberColumn("Score", format="%.5f"),
                "status": st.column_config.TextColumn("Status", width="small"),
                "submissions": st.column_config.NumberColumn("Submissions", width="small"),
                "first_submission": st.column_config.DatetimeColumn("First submission"),
                "last_update": st.column_config.DatetimeColumn("Last update"),
                f"top_{round(TOP_FRACTION * 100)}": st.column_config.CheckboxColumn(
                    f"Top {TOP_FRACTION:.0%}", width="small"),
            },
        )
        st.download_button("Download CSV", show.to_csv(index=False).encode("utf-8"),
                           file_name="leaderboard.csv", mime="text/csv")


if __name__ == "__main__":
    main()
