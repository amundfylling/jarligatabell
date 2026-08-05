import json
import math
import time
import os
from datetime import datetime
from urllib.parse import urljoin
import html

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ----------------------------- Config & constants -----------------------------

# mapping from finishing place → points value
PLACEMENT_TO_VALUE = {
    1: 100, 2: 85, 3: 75, 4: 65, 5: 60, 6: 55, 7: 50, 8: 48, 9: 46, 10: 44,
    11: 42, 12: 40, 13: 39, 14: 38, 15: 37, 16: 36, 17: 35, 18: 34, 19: 33, 20: 32,
    21: 31, 22: 30, 23: 29, 24: 28, 25: 27, 26: 26, 27: 25, 28: 24, 29: 23, 30: 22,
    31: 21, 32: 20, 33: 19, 34: 18, 35: 17, 36: 16, 37: 15, 38: 14, 39: 13, 40: 12,
    41: 11, 42: 10, 43: 9, 44: 8, 45: 7, 46: 6, 47: 5, 48: 4, 49: 3, 50: 2
}
TOP_N = 17  # how many results count in "Topp 17"

HTTP_TIMEOUT = 20  # seconds
SLEEP_BETWEEN = 0.4  # polite delay between requests (seconds)

# persistence for scraped tournaments
DATA_FILE = "tournaments.json"

# serial page for Jærligaen
SERIES_URL = "https://stiga.trefik.cz/ithf/ranking/serial.aspx?ID=220004"


def season_label(start_year: int) -> str:
    """Return display label like '2002/2003' for a season starting *start_year*."""
    return f"{start_year}/{start_year + 1}"


def season_filename(start_year: int) -> str:
    """Return filename like '2002-2003.html' for a season."""
    return f"{start_year}-{start_year + 1}.html"


def season_date_range(start_year: int) -> tuple[str, str]:
    """Start and end date strings (dd.mm.yyyy) for the season."""
    start = f"01.07.{start_year}"
    end = f"30.06.{start_year + 1}"
    return start, end


def current_season_start_year() -> int:
    """Return starting year of the current season (July-June)."""
    today = datetime.today()
    return today.year if today.month >= 7 else today.year - 1


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tournaments": {}}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ----------------------------- Scraping helpers -------------------------------

def _new_session() -> requests.Session:
    """
    Create a requests session with a desktop UA and sensible defaults.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36",
        "Accept-Language": "no,en;q=0.9",
    })
    return s


def extract_series_tournaments(session: requests.Session, series_url: str,
                                start_date_str: str, end_date_str: str) -> list[dict]:
    """
    Fetch the series page and return a list of tournaments within the date range.
    Each item: {'date': date, 'name': str, 'url': str}
    """
    start_date = datetime.strptime(start_date_str, "%d.%m.%Y").date()
    end_date   = datetime.strptime(end_date_str,   "%d.%m.%Y").date()
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    r = session.get(series_url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Past tournaments area
    past_span = soup.find("span", id="LabPast")
    if not past_span:
        raise RuntimeError("Kunne ikke finne 'LabPast' (tidligere turneringer).")
    table = past_span.find("table")
    if not table:
        raise RuntimeError("Kunne ikke finne tabell i #LabPast.")

    tournaments: list[dict] = []
    rows = table.find_all("tr")
    for row in rows[1:] if len(rows) > 1 else rows:
        cols = row.find_all("td")
        if len(cols) < 2:
            continue

        date_text = cols[0].get_text(strip=True)
        try:
            tour_date = datetime.strptime(date_text, "%d.%m.%Y").date()
        except ValueError:
            continue

        if not (start_date <= tour_date <= end_date):
            continue

        link = cols[1].find("a", href=True)
        if not link:
            continue

        name = link.get_text(strip=True)
        url = urljoin(series_url, link["href"])
        tournaments.append({"date": tour_date, "name": name, "url": url})

    tournaments.sort(key=lambda t: t["date"])
    return tournaments


def extract_tournament_results(session: requests.Session, tournament_url: str) -> dict[str, int]:
    """
    Fetch tournament page and return {player_name -> placement}.
    """
    r = session.get(tournament_url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    target = None
    title_span = soup.find("span", id="LabTitle", string=lambda t: t and "Final table" in t)
    if title_span:
        outer = title_span.find_next("table")
        target = outer.find("table") if outer else None

    if not target:
        for tbl in soup.find_all("table"):
            if tbl.find("a", id="LBName") or tbl.find(string=lambda t: isinstance(t, str) and t.strip().lower() == "player"):
                target = tbl
                break

    if not target:
        raise RuntimeError("Finner ikke resultat-tabell på siden: " + tournament_url)

    results: dict[str, int] = {}
    rows = target.find_all("tr")

    data_rows = []
    for tr in rows:
        tds = tr.find_all("td")
        if not tds:
            continue
        if tr.find("a", id="LBPos") or tr.find("a", id="LBName") or any("head" in (td.get("class") or []) for td in tds):
            continue
        data_rows.append(tr)

    for tr in data_rows:
        cols = tr.find_all("td")
        if len(cols) < 2:
            continue

        place_text = cols[0].get_text(strip=True).rstrip(".")
        try:
            place = int(place_text)
        except ValueError:
            place = None
            for td in cols[:2]:
                txt = td.get_text(strip=True).rstrip(".")
                if txt.isdigit():
                    place = int(txt)
                    break
            if place is None:
                continue

        player_a = None
        for td in cols:
            a = td.find("a", href=True)
            if a and "player.aspx" in a["href"].lower():
                player_a = a
                break
        if not player_a:
            continue

        player_name = player_a.get_text(strip=True)
        if not player_name:
            continue

        player_name = " ".join(player_name.split())
        results[player_name] = place

    return results

# ----------------------------- Data assembly ----------------------------------

def build_df_from_tournament_data(tournaments: list[dict]):
    """Build DataFrame & metadata from scraped tournament data."""
    all_points: dict[str, dict] = {}
    all_places: dict[str, dict] = {}
    t_meta = []

    tournaments_sorted = sorted(tournaments, key=lambda t: t["date"])
    for i, t in enumerate(tournaments_sorted, start=1):
        key = f"#{i}"
        results = t["results"]
        for player, place in results.items():
            place = int(place)
            pts = PLACEMENT_TO_VALUE.get(place, 0)
            all_points.setdefault(player, {})[key] = pts
            all_places.setdefault(player, {})[key] = place

        t_meta.append({
            "key": key,
            "name": t["name"],
            "date": t["date"],
            "url": t["url"],
            "participants": t.get("participants", len(results)),
            "winner": t.get("winner", "-"),
            "winner_points": t.get("winner_points", 0),
        })

    pts_df = pd.DataFrame.from_dict(all_points, orient="index").fillna(0).astype(int)
    plc_df = pd.DataFrame.from_dict(all_places, orient="index")
    league_cols = [f"#{i}" for i in range(1, len(tournaments_sorted) + 1)]
    for df in (pts_df, plc_df):
        for c in league_cols:
            if c not in df.columns:
                df[c] = 0 if df is pts_df else math.nan
        df[:] = df[league_cols]

    def count_played(row) -> int:
        return int((row[league_cols] > 0).sum())

    def avg_points_when_played(row) -> float:
        vals = row[league_cols]
        played_vals = vals[vals > 0]
        return float(round(played_vals.mean(), 2)) if len(played_vals) else 0.0

    def count_wins(name: str) -> int:
        row = plc_df.loc[name, league_cols]
        return int((row == 1).sum())

    def count_podiums(name: str) -> int:
        row = plc_df.loc[name, league_cols]
        return int((row <= 3).sum())

    def top_n_sum(row, n=TOP_N) -> int:
        vals = sorted([int(v) for v in row[league_cols] if v > 0], reverse=True)
        return int(sum(vals[:n]))

    df = pts_df.copy()
    df["Spilt"] = df.apply(count_played, axis=1)
    df["Tellende"] = df.apply(lambda r: min(r["Spilt"], TOP_N), axis=1)
    df["Snitt"] = df.apply(avg_points_when_played, axis=1)
    df["Seire"] = [count_wins(p) for p in df.index]
    df["Pallplasseringer"] = [count_podiums(p) for p in df.index]
    df["Topp 17"] = df.apply(top_n_sum, axis=1)

    cols_order = ["Topp 17", "Tellende", "Spilt", "Snitt", "Seire", "Pallplasseringer"] + league_cols
    df = df[cols_order]
    df = df.sort_values(by=["Topp 17", "Seire"] + league_cols,
                        ascending=[False, False] + [False] * len(league_cols))
    df.insert(0, "Rank", range(1, len(df) + 1))

    meta = {
        "tournaments": t_meta,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top_n": TOP_N,
    }
    return df, meta

# ----------------------------- HTML Component Rendering -----------------------

def render_page_head(title: str, description: str, canonical_path: str = "") -> str:
    url = f"https://jarligatabell.puck.no/{canonical_path}".rstrip('/')
    return f"""<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}" />
  <link rel="canonical" href="{url}" />

  <!-- Open Graph -->
  <meta property="og:type" content="website" />
  <meta property="og:title" content="{html.escape(title)}" />
  <meta property="og:description" content="{html.escape(description)}" />
  <meta property="og:url" content="{url}" />

  <!-- Stylesheet -->
  <link rel="stylesheet" href="./assets/styles.css" />
</head>"""


def render_site_header(active_nav: str = "tabell") -> str:
    tabell_curr = ' aria-current="page"' if active_nav == "tabell" else ""
    stats_curr = ' aria-current="page"' if active_nav == "stats" else ""

    return f"""<header class="site-header">
  <div class="site-wrap site-header__inner">
    <a href="index.html" class="site-brand" aria-label="Jærligaen – puck.no">
      <img src="./assets/images/logo.png" alt="NBHF Logo" class="site-brand__logo" width="36" height="36" />
      <div class="site-brand__text">
        <span class="site-brand__title">JÆRLIGAEN</span>
        <span class="site-brand__subtitle">Jærligaen i Bordhockey</span>
      </div>
    </a>
    <nav class="site-nav" aria-label="Hovedmeny">
      <ul class="site-nav__list">
        <li><a href="index.html" class="site-nav__link"{tabell_curr}>Tabell</a></li>
        <li><a href="stats.html" class="site-nav__link"{stats_curr}>Sesongstatistikk</a></li>
        <li><a href="https://puck.no" class="site-nav__link site-nav__link--external" target="_blank" rel="noopener">puck.no ↗</a></li>
      </ul>
    </nav>
  </div>
</header>"""


def render_page_intro(season_label_str: str, season_links: list[tuple[str, str]], is_active_season: bool = False) -> str:
    options_html = []
    for lbl, fname in season_links:
        sel = " selected" if lbl == season_label_str else ""
        options_html.append(f'<option value="{fname}"{sel}>{lbl}</option>')
    select_options = "".join(options_html)

    status_badge = '<span class="season-status-badge">Pågående sesong</span>' if is_active_season else '<span class="season-status-badge season-status-badge--ended">Avsluttet sesong</span>'

    return f"""<section class="page-intro">
  <div class="site-wrap page-intro__grid">
    <div class="page-intro__content">
      <div class="page-eyebrow">Jærligaen i Bordhockey</div>
      <h1 class="page-title">Tabell {season_label_str}</h1>
      <p class="page-description">De 17 beste turneringsresultatene teller i sesongsammendraget.</p>
    </div>
    <div class="season-controls">
      <label for="season-select" class="season-select-label">Sesong:</label>
      <div class="season-select-wrapper">
        <select id="season-select" class="season-select">
          {select_options}
        </select>
      </div>
      {status_badge}
    </div>
  </div>
</section>"""


def render_metrics_cards(num_players: int, num_leagues: int, max_part: int, avg_part: int) -> str:
    return f"""<section class="metrics-section">
  <div class="site-wrap">
    <div class="metric-grid">
      <div class="metric-card">
        <div class="metric-label">Antall spillere</div>
        <div class="metric-value">{num_players}</div>
        <div class="metric-sub">Deltakere i sesongen</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Antall ligaer</div>
        <div class="metric-value">{num_leagues}</div>
        <div class="metric-sub">Spilte turneringer</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Maks deltakere</div>
        <div class="metric-value">{max_part}</div>
        <div class="metric-sub">Høyeste oppmøte</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Snitt deltakere</div>
        <div class="metric-value">{avg_part}</div>
        <div class="metric-sub">Spillere per turnering</div>
      </div>
    </div>
  </div>
</section>"""


def render_standings_toolbar(stats_href: str) -> str:
    return f"""<div class="standings-toolbar">
  <div class="toolbar-info">
    <h2 class="section-title">Sesongtabell</h2>
    <p class="section-desc">Rangeringen avgjøres av Topp 17-poeng, deretter antall seire og beste enkeltresultat.</p>
  </div>
  <div class="toolbar-controls">
    <div class="view-switcher" role="tablist" aria-label="Visningsmodus">
      <button type="button" id="btn-view-summary" role="tab" aria-selected="true" aria-controls="view-summary">Sammendrag</button>
      <button type="button" id="btn-view-full" role="tab" aria-selected="false" aria-controls="view-full">Alle ligaer</button>
    </div>
    <a href="{stats_href}" class="button-outline" style="min-height: 38px; padding: 0.44rem 0.875rem; font-size: 0.875rem;">Sesongstatistikk 📊</a>
  </div>
</div>"""


def render_summary_table(df: pd.DataFrame) -> str:
    rows_html = []
    for idx, (player, row) in enumerate(df.iterrows(), start=1):
        rank = int(row["Rank"])
        if rank == 1:
            rank_badge = '<span class="rank-badge rank-1">1</span>'
        elif rank == 2:
            rank_badge = '<span class="rank-badge rank-2">2</span>'
        elif rank == 3:
            rank_badge = '<span class="rank-badge rank-3">3</span>'
        else:
            rank_badge = f'<span class="rank-other">{rank}</span>'

        player_id = f"p-{idx}"
        details_id = f"details-{player_id}"

        player_name_esc = html.escape(player)
        topp17 = int(row["Topp 17"])
        tellende = int(row["Tellende"])
        spilt = int(row["Spilt"])
        snitt = f"{row['Snitt']:.2f}"
        seire = int(row["Seire"])
        pall = int(row["Pallplasseringer"])

        # Main summary row
        tr_main = f"""<tr>
  <td class="col-num col-center">{rank_badge}</td>
  <td class="col-player">{player_name_esc}</td>
  <td class="col-num col-topp17">{topp17}</td>
  <td class="col-num">{tellende}</td>
  <td class="col-num">{spilt}</td>
  <td class="col-num">{snitt}</td>
  <td class="col-num">{seire}</td>
  <td class="col-num">{pall}</td>
  <td class="row-trigger-cell">
    <button type="button" class="btn-row-expand" aria-expanded="false" aria-controls="{details_id}" aria-label="Vis detaljer for {player_name_esc}">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
    </button>
  </td>
</tr>"""

        # Expandable mobile details row
        tr_details = f"""<tr id="{details_id}" class="row-details-row" hidden>
  <td colspan="9" class="row-details-cell">
    <div class="details-grid">
      <div class="detail-item"><span class="detail-label">Topp 17 poeng</span><span class="detail-value">{topp17}</span></div>
      <div class="detail-item"><span class="detail-label">Tellende / Spilt</span><span class="detail-value">{tellende} / {spilt}</span></div>
      <div class="detail-item"><span class="detail-label">Snitt poeng</span><span class="detail-value">{snitt}</span></div>
      <div class="detail-item"><span class="detail-label">Seire / Pall</span><span class="detail-value">{seire} seire, {pall} pall</span></div>
    </div>
  </td>
</tr>"""

        rows_html.append(tr_main)
        rows_html.append(tr_details)

    tbody_content = "\n".join(rows_html)

    return f"""<div id="view-summary" class="tab-panel" role="tabpanel">
  <div class="table-card">
    <table class="standings-table">
      <thead>
        <tr>
          <th scope="col" style="width: 60px; text-align: center;">#</th>
          <th scope="col">Spiller</th>
          <th scope="col" class="col-num">Topp 17</th>
          <th scope="col" class="col-num">Tellende</th>
          <th scope="col" class="col-num">Spilt</th>
          <th scope="col" class="col-num">Snitt</th>
          <th scope="col" class="col-num">Seire</th>
          <th scope="col" class="col-num">Pallplasser</th>
          <th scope="col" style="width: 48px;"><span class="sr-only">Detaljer</span></th>
        </tr>
      </thead>
      <tbody>
        {tbody_content}
      </tbody>
    </table>
  </div>
</div>"""


def render_full_matrix_table(df: pd.DataFrame, meta: dict) -> str:
    tournaments = meta.get("tournaments", [])
    league_cols = [c for c in df.columns if c.startswith("#")]

    # Build Header
    th_cols = [
      '<th scope="col" class="sticky-col-rank" style="width: 50px; text-align: center;">#</th>',
      '<th scope="col" class="sticky-col-player">Spiller</th>',
      '<th scope="col" class="col-num" style="background: var(--color-navy-dark);">Topp 17</th>'
    ]

    for t in tournaments:
        key = t["key"]
        dshort = datetime.fromisoformat(t["date"]).strftime("%d.%m")
        name = html.escape(t["name"])
        url = t["url"]
        part = t.get("participants", 0)
        winner = html.escape(t.get("winner", "-"))
        wpts = t.get("winner_points", 0)

        header_cell = f"""<th scope="col" class="col-center" title="Vinner: {winner} ({wpts}p)">
  <a href="{url}" class="tour-header-link" target="_blank" rel="noopener">
    <span>{key}</span>
    <span class="tour-sub">{dshort}</span>
    <span class="tour-sub">{part} spillere</span>
  </a>
</th>"""
        th_cols.append(header_cell)

    thead_html = "<tr>\n" + "\n".join(th_cols) + "\n</tr>"

    # Build Body
    rows_html = []
    for player, row in df.iterrows():
        rank = int(row["Rank"])
        if rank == 1:
            rank_badge = '<span class="rank-badge rank-1">1</span>'
        elif rank == 2:
            rank_badge = '<span class="rank-badge rank-2">2</span>'
        elif rank == 3:
            rank_badge = '<span class="rank-badge rank-3">3</span>'
        else:
            rank_badge = f'<span class="rank-other">{rank}</span>'

        player_name_esc = html.escape(player)
        topp17 = int(row["Topp 17"])

        # Determine which league scores are in the player's Top 17 counting set
        played_scores = [(c, int(row[c])) for c in league_cols if int(row[c]) > 0]
        played_scores_sorted = sorted(played_scores, key=lambda item: item[1], reverse=True)
        counting_cols = {c for c, score in played_scores_sorted[:TOP_N]}

        td_cols = [
            f'<td class="col-num col-center sticky-col-rank">{rank_badge}</td>',
            f'<td class="col-player sticky-col-player">{player_name_esc}</td>',
            f'<td class="col-num col-topp17" style="background: var(--color-ice);">{topp17}</td>'
        ]

        for c in league_cols:
            score = int(row[c])
            if score == 0:
                td_cols.append('<td class="cell-absent">—</td>')
            else:
                is_counting = c in counting_cols
                cls = "cell-result cell-counting" if is_counting else "cell-result"
                td_cols.append(f'<td class="{cls}">{score}</td>')

        rows_html.append("<tr>\n" + "\n".join(td_cols) + "\n</tr>")

    tbody_html = "\n".join(rows_html)

    return f"""<div id="view-full" class="tab-panel" role="tabpanel" hidden>
  <div class="table-card">
    <div id="scroll-hint" class="scroll-hint">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 8l4 4m0 0l-4 4m4-4H3"/></svg>
      <span>Dra sidelengs for å se alle ligaer ({len(tournaments)} turneringer)</span>
    </div>
    <div class="table-scroll">
      <table class="standings-table">
        <thead>
          {thead_html}
        </thead>
        <tbody>
          {tbody_html}
        </tbody>
      </table>
    </div>
  </div>
</div>"""


def render_site_footer(updated_str: str = "") -> str:
    year = datetime.now().year
    updated_display = f"Sist oppdatert: {updated_str}" if updated_str else f"Sesongdata for Jærligaen"
    return f"""<footer class="site-footer">
  <div class="site-wrap site-footer__inner">
    <div class="site-footer__brand">
      <span>Jærligaen i Bordhockey</span>
      <span>•</span>
      <span>{year}</span>
    </div>
    <div>Offisiell turneringsdata fra ITHF · {updated_display}</div>
    <div class="site-footer__links">
      <a href="https://puck.no" class="site-footer__link" target="_blank" rel="noopener">puck.no</a>
      <a href="stats.html" class="site-footer__link">Statistikk</a>
    </div>
  </div>
</footer>"""

# ----------------------------- Page Exporters ---------------------------------

def df_to_html_file(df: pd.DataFrame, meta: dict, filepath: str,
                    season_label_str: str, season_links: list[tuple[str, str]]):
    """Render a season's standings page to static HTML."""
    curr_year = current_season_start_year()
    is_active = season_label_str == season_label(curr_year)

    tournaments = meta.get("tournaments", [])
    parts = [t.get("participants", 0) for t in tournaments]
    num_players = len(df)
    num_leagues = len(tournaments)
    max_part = max(parts) if parts else 0
    avg_part = round(sum(parts) / len(parts)) if parts else 0

    def start_from_label(lbl: str) -> int:
        return int(lbl.split("/")[0])

    s_start = start_from_label(season_label_str)
    stats_href = f"stats.html?season={s_start}-{s_start+1}"

    title = f"Jærligaen {season_label_str} – Tabell"
    desc = f"Se tabellen for Jærligaen {season_label_str}, med Topp 17-poeng, deltakelser, seire, pallplasser og resultater."

    last_tour_date = ""
    if tournaments:
        try:
            last_tour_date = datetime.fromisoformat(tournaments[-1]["date"]).strftime("%d.%m.%Y")
        except Exception:
            pass

    head_html = render_page_head(title, desc, filepath)
    header_html = render_site_header(active_nav="tabell")
    intro_html = render_page_intro(season_label_str, season_links, is_active_season=is_active)
    metrics_html = render_metrics_cards(num_players, num_leagues, max_part, avg_part)
    toolbar_html = render_standings_toolbar(stats_href)
    summary_table_html = render_summary_table(df)
    full_table_html = render_full_matrix_table(df, meta)
    footer_html = render_site_footer(last_tour_date)

    html_content = f"""<!DOCTYPE html>
<html lang="no">
{head_html}
<body class="page-reveal">
  <a href="#main-content" class="skip-link">Hopp til hovedinnhold</a>
  {header_html}

  <main id="main-content">
    {intro_html}
    {metrics_html}

    <section class="standings-section">
      <div class="site-wrap">
        {toolbar_html}
        {summary_table_html}
        {full_table_html}

        <div class="info-note">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
          <div><strong>Forklaring:</strong> <em>Topp 17</em> er summen av de 17 beste poengsummene. <em>Tellende</em> er antall poenggivende plasseringer som inngår i Topp 17. <em>Snitt</em> er gjennomsnittlig poengsum per spilt liga. I matrisen markerer røde prikker teller-resultater.</div>
        </div>
      </div>
    </section>
  </main>

  {footer_html}
  <script src="./assets/site.js"></script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)


def build_global_stats(label_to_dfmeta: dict[str, tuple[pd.DataFrame, dict]], top_k: int = 10) -> dict:
    """Build global cross-season stats payload."""
    from collections import Counter

    season_labels = sorted(label_to_dfmeta.keys(), key=lambda s: int(s.split("/")[0]))

    champions = Counter()
    podiums = Counter()
    leagues_attended = Counter()
    tour_wins = Counter()

    seasons_series = []
    players_series = []
    leagues_series = []
    max_part_series = []
    avg_part_series = []

    season_rows_summary = []

    for label in season_labels:
        df, meta = label_to_dfmeta[label]
        tournaments = meta.get("tournaments", [])

        seasons_series.append(label)
        players_cnt = len(df)
        leagues_cnt = len(tournaments)
        players_series.append(players_cnt)
        leagues_series.append(leagues_cnt)

        parts = [int(t.get("participants", 0)) for t in tournaments]
        max_p = max(parts) if parts else 0
        avg_p = round(sum(parts) / len(parts)) if parts else 0
        max_part_series.append(max_p)
        avg_part_series.append(avg_p)

        season_rows_summary.append({
            "season": label,
            "filename": season_filename(int(label.split("/")[0])),
            "players": players_cnt,
            "leagues": leagues_cnt,
            "maxPart": max_p,
            "avgPart": avg_p,
        })

        if not df.empty:
            season_winner = df.index[0]
            champions[season_winner] += 1

            for p in df.head(3).index.tolist():
                podiums[p] += 1

            for player, spilt in df["Spilt"].items():
                leagues_attended[player] += int(spilt)

        for t in tournaments:
            w = t.get("winner") or "-"
            if w != "-":
                tour_wins[w] += 1

    def top_list(counter: Counter, k=top_k):
        items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:k]
        return [{"name": n, "val": int(v)} for n, v in items]

    payload = {
        "seasons": seasons_series,
        "seasonSummary": season_rows_summary,
        "series": {
            "players": players_series,
            "leagues": leagues_series,
            "maxParticipants": max_part_series,
            "avgParticipants": avg_part_series,
        },
        "leaderboards": {
            "seasonTitles": top_list(champions),
            "seasonPodiums": top_list(podiums),
            "mostLeaguesAttended": top_list(leagues_attended),
            "tournamentWins": top_list(tour_wins),
        }
    }
    return payload


def stats_overview_to_html(global_payload: dict, filepath: str):
    """Build cross-season statistics page (stats.html)."""
    payload_json = json.dumps(global_payload, ensure_ascii=False)

    title = "Jærligaen – Sesongstatistikk"
    desc = "Se historiske rekorder, adelskalender, deltakelse og utvikling på tvers av Jærligaens sesonger."

    head_html = render_page_head(title, desc, filepath)
    header_html = render_site_header(active_nav="stats")

    # Leaderboard Cards
    def make_leaderboard_card(card_title: str, items: list[dict], unit_label: str = "") -> str:
        rows = []
        for idx, item in enumerate(items, start=1):
            if idx == 1:
                badge = '<span class="rank-badge rank-1" style="width: 1.5rem; height: 1.5rem; font-size: 0.75rem;">1</span>'
            elif idx == 2:
                badge = '<span class="rank-badge rank-2" style="width: 1.5rem; height: 1.5rem; font-size: 0.75rem;">2</span>'
            elif idx == 3:
                badge = '<span class="rank-badge rank-3" style="width: 1.5rem; height: 1.5rem; font-size: 0.75rem;">3</span>'
            else:
                badge = f'<span class="rank-other" style="width: 1.5rem; font-size: 0.75rem;">{idx}</span>'

            p_name = html.escape(item["name"])
            val = item["val"]
            unit_suffix = f" {unit_label}" if unit_label else ""

            row_html = f"""<li class="leaderboard-card__item">
  <div class="leaderboard-card__player">
    {badge}
    <span>{p_name}</span>
  </div>
  <div class="leaderboard-card__value">{val}{unit_suffix}</div>
</li>"""
            rows.append(row_html)

        list_content = "\n".join(rows)

        return f"""<div class="leaderboard-card">
  <div class="leaderboard-card__header">
    <h3 class="leaderboard-card__title">{card_title}</h3>
  </div>
  <ol class="leaderboard-card__list">
    {list_content}
  </ol>
</div>"""

    lb_titles = make_leaderboard_card("Flest sesongtitler", global_payload["leaderboards"]["seasonTitles"], "titler")
    lb_podiums = make_leaderboard_card("Flest pallplasser", global_payload["leaderboards"]["seasonPodiums"], "ganger")
    lb_attended = make_leaderboard_card("Flest ligadeltakelser", global_payload["leaderboards"]["mostLeaguesAttended"], "ligaer")
    lb_wins = make_leaderboard_card("Flest turneringsseire", global_payload["leaderboards"]["tournamentWins"], "seire")

    # Cross-season table rows
    table_rows = []
    for row in global_payload["seasonSummary"]:
        s_lbl = row["season"]
        fname = row["filename"]
        table_rows.append(f"""<tr>
  <td><a href="{fname}" class="content-link" style="font-weight: 700;">{s_lbl}</a></td>
  <td class="col-num">{row['players']}</td>
  <td class="col-num">{row['leagues']}</td>
  <td class="col-num">{row['maxPart']}</td>
  <td class="col-num">{row['avgPart']}</td>
</tr>""")

    season_table_tbody = "\n".join(table_rows)
    latest_s = global_payload["seasons"][-1] if global_payload.get("seasons") else ""
    footer_html = render_site_footer(f"2002–{latest_s}")

    html_content = f"""<!DOCTYPE html>
<html lang="no">
{head_html}
<body class="page-reveal">
  <a href="#main-content" class="skip-link">Hopp til hovedinnhold</a>
  {header_html}

  <main id="main-content">
    <section class="page-intro">
      <div class="site-wrap page-intro__grid">
        <div class="page-intro__content">
          <div class="page-eyebrow">Jærligaen i Bordhockey</div>
          <h1 class="page-title">Sesongstatistikk</h1>
          <p class="page-description">Rekorder, adelskalender og historisk utvikling på tvers av alle sesonger.</p>
        </div>
      </div>
    </section>

    <section class="standings-section">
      <div class="site-wrap">
        <h2 class="section-title" style="margin-bottom: 1.25rem;">Historiske topplister</h2>
        <div class="leaderboard-grid">
          {lb_titles}
          {lb_podiums}
          {lb_attended}
          {lb_wins}
        </div>

        <h2 class="section-title" style="margin-top: 2.5rem; margin-bottom: 1.25rem;">Utvikling per sesong</h2>
        <div class="chart-grid">
          <div class="chart-card">
            <h3 class="chart-card__title">Maks deltakere pr. sesong</h3>
            <div class="chart-container"><canvas id="chMax"></canvas></div>
          </div>
          <div class="chart-card">
            <h3 class="chart-card__title">Snitt deltakere pr. sesong</h3>
            <div class="chart-container"><canvas id="chAvg"></canvas></div>
          </div>
          <div class="chart-card">
            <h3 class="chart-card__title">Antall unike spillere pr. sesong</h3>
            <div class="chart-container"><canvas id="chPlayers"></canvas></div>
          </div>
          <div class="chart-card">
            <h3 class="chart-card__title">Antall ligaer pr. sesong</h3>
            <div class="chart-container"><canvas id="chLeagues"></canvas></div>
          </div>
        </div>

        <h2 class="section-title" style="margin-top: 2.5rem; margin-bottom: 1.25rem;">Nøkkeltall per sesong</h2>
        <div class="table-card">
          <table class="stats-table">
            <thead>
              <tr>
                <th scope="col">Sesong</th>
                <th scope="col" class="col-num">Unike spillere</th>
                <th scope="col" class="col-num">Antall ligaer</th>
                <th scope="col" class="col-num">Maks deltakere</th>
                <th scope="col" class="col-num">Snitt deltakere</th>
              </tr>
            </thead>
            <tbody>
              {season_table_tbody}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  {footer_html}

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('payload').textContent);

    function createTrendChart(id, labels, seriesData, labelName) {{
      const ctx = document.getElementById(id).getContext('2d');
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [{{
            label: labelName,
            data: seriesData,
            borderColor: '#c8102e',
            backgroundColor: 'rgba(200, 16, 46, 0.08)',
            borderWidth: 2.5,
            pointBackgroundColor: '#0e2a57',
            pointRadius: 3,
            pointHoverRadius: 6,
            tension: 0.2,
            fill: true
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          scales: {{
            x: {{
              grid: {{ color: '#f1f5f9' }},
              ticks: {{ font: {{ family: 'Geist Variable, sans-serif', size: 11 }}, color: '#64748b' }}
            }},
            y: {{
              beginAtZero: true,
              grid: {{ color: '#e2e8f0' }},
              ticks: {{ precision: 0, font: {{ family: 'Geist Variable, sans-serif', size: 11 }}, color: '#64748b' }}
            }}
          }},
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              backgroundColor: '#0e2a57',
              titleFont: {{ family: 'Bricolage Grotesque Variable, sans-serif' }},
              bodyFont: {{ family: 'Geist Variable, sans-serif' }},
              padding: 10,
              cornerRadius: 6
            }}
          }}
        }}
      }});
    }}

    document.addEventListener('DOMContentLoaded', () => {{
      const labels = data.seasons;
      createTrendChart('chMax', labels, data.series.maxParticipants, 'Maks deltakere');
      createTrendChart('chAvg', labels, data.series.avgParticipants, 'Snitt deltakere');
      createTrendChart('chPlayers', labels, data.series.players, 'Unike spillere');
      createTrendChart('chLeagues', labels, data.series.leagues, 'Antall ligaer');
    }});
  </script>
  <script src="./assets/site.js"></script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

# ----------------------------------- Main -------------------------------------

def main():
    """Rebuild pages + global stats.html."""
    data = load_data()
    session = _new_session()

    start_year = 2002
    current_start = current_season_start_year()

    # Exclude empty season 2006/2007
    season_years = [y for y in range(start_year, current_start + 1) if y != 2006]

    # -------- scrape/cache --------
    for year in season_years:
        label = season_label(year)
        start, end = season_date_range(year)
        print(f"🔍 Henter turneringer for {label} …")
        try:
            tournaments = extract_series_tournaments(session, SERIES_URL, start, end)
        except Exception as e:
            print(f"⚠️  Klarte ikke hente turneringer for {label}: {e}")
            continue

        for t in tournaments:
            if t["url"] in data["tournaments"]:
                continue
            print(f"  ➕ Skraper {t['name']} ({t['date']:%d.%m.%Y})")
            try:
                results = extract_tournament_results(session, t["url"])
            except Exception as e:
                print(f"    Feil ved skraping av {t['name']}: {e}")
                continue

            participants = len(results)
            winner_name, winner_place = min(results.items(), key=lambda kv: kv[1])
            winner_points = PLACEMENT_TO_VALUE.get(winner_place, 0)
            data["tournaments"][t["url"]] = {
                "season": season_label(year),
                "name": t["name"],
                "date": t["date"].isoformat(),
                "url": t["url"],
                "participants": participants,
                "winner": winner_name,
                "winner_points": int(winner_points),
                "results": results,
            }
            time.sleep(SLEEP_BETWEEN)

    save_data(data)

    # -------- build season pages --------
    season_links = [(season_label(y), season_filename(y)) for y in season_years]
    label_to_dfmeta: dict[str, tuple[pd.DataFrame, dict]] = {}

    for year in season_years:
        label = season_label(year)
        file = season_filename(year)
        t_list = [t for t in data["tournaments"].values() if t["season"] == label]
        if not t_list:
            continue
        df, meta = build_df_from_tournament_data(t_list)
        label_to_dfmeta[label] = (df, meta)

        print(f"🎨 Lager HTML: {file}")
        df_to_html_file(df, meta, file, label, season_links)

    # -------- build ONE cross-season stats.html --------
    if label_to_dfmeta:
        global_payload = build_global_stats(label_to_dfmeta, top_k=10)
        print("📊 Lager samlet oversikt: stats.html")
        stats_overview_to_html(global_payload, "stats.html")

    # Latest season -> index.html (use latest generated season file)
    generated_years = [y for y in season_years if season_label(y) in label_to_dfmeta]
    if generated_years:
        latest_file = season_filename(max(generated_years))
        if os.path.exists(latest_file):
            with open(latest_file, "r", encoding="utf-8") as src, open("index.html", "w", encoding="utf-8") as dst:
                dst.write(src.read())

    print("✅ Ferdig")


if __name__ == "__main__":
    main()
