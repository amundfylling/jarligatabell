
import json
import math
import time
import os
from datetime import datetime
from urllib.parse import urljoin

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

    - Robust to minor markup variations and trims whitespace.
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
    # Defensive: skip header if present
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
    Hent en turneringsside og returner {spiller_navn -> plassering}.
    Robust mot kolonnerekkefølge og tar alltid navnet (anker-tekst) fra lenke til player.aspx.
    """
    r = session.get(tournament_url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # 1) Prøv standard "Final table" struktur
    target = None
    title_span = soup.find("span", id="LabTitle", string=lambda t: t and "Final table" in t)
    if title_span:
        outer = title_span.find_next("table")
        target = outer.find("table") if outer else None

    # 2) Fallback: finn tabellen som har "Player"-header (LBName) eller tekst "Player"
    if not target:
        for tbl in soup.find_all("table"):
            if tbl.find("a", id="LBName") or tbl.find(string=lambda t: isinstance(t, str) and t.strip().lower() == "player"):
                target = tbl
                break

    if not target:
        raise RuntimeError("Finner ikke resultat-tabell på siden: " + tournament_url)

    results: dict[str, int] = {}
    rows = target.find_all("tr")

    # Hopp over header-rader (med 'head' klasser eller LBPos/LBName)
    data_rows = []
    for tr in rows:
        tds = tr.find_all("td")
        if not tds:
            continue
        # Heuristikk: header har ofte <a id="LBPos"> / klassenavn 'head'
        if tr.find("a", id="LBPos") or tr.find("a", id="LBName") or any("head" in (td.get("class") or []) for td in tds):
            continue
        data_rows.append(tr)

    for tr in data_rows:
        cols = tr.find_all("td")
        if len(cols) < 2:
            continue

        # Plassering: første tall i første kolonne (ofte '1.')
        place_text = cols[0].get_text(strip=True).rstrip(".")
        try:
            place = int(place_text)
        except ValueError:
            # fallback: let etter et heltall i første 1–2 kolonner
            place = None
            for td in cols[:2]:
                txt = td.get_text(strip=True).rstrip(".")
                if txt.isdigit():
                    place = int(txt)
                    break
            if place is None:
                continue  # kan ikke tolke rad

        # Spiller: finn <a href="player.aspx?id=...">Navn</a> i radens kolonner
        player_a = None
        for td in cols:
            a = td.find("a", href=True)
            if a and "player.aspx" in a["href"].lower():
                player_a = a
                break
        if not player_a:
            continue  # ingen spillerlenke -> hopp

        player_name = player_a.get_text(strip=True)
        if not player_name:
            continue

        # Normaliser mellomrom
        player_name = " ".join(player_name.split())
        results[player_name] = place

    return results


# ----------------------------- Data assembly ----------------------------------

def build_results_dataframe(series_url: str, start_date_str: str, end_date_str: str):
    """
    Build points & metrics DataFrame + metadata for the HTML renderer.

    Returns
    -------
    df : pandas.DataFrame
        Index = player name.
        Columns:
          ['Rank','Topp 17','Tellende','Spilt','Snitt','Seire','Pallplasseringer', '#1', '#2', ...]
        Sorted by 'Topp 17' desc, then Seire desc, then best single score desc.
    meta : dict
        {
          'tournaments': [
            {'key':'#1','name':..., 'date':'YYYY-MM-DD','url':..., 'participants':int,
             'winner': 'Name', 'winner_points': int}
             ...],
          'generated_at': ISO datetime string,
          'top_n': int
        }
    """
    session = _new_session()

    # 1) Tournaments
    tournaments = extract_series_tournaments(session, series_url, start_date_str, end_date_str)
    print(f"Fant {len(tournaments)} turneringer i perioden.")

    # 2) Gather results for each tournament
    # Use two dicts: points and places (so we can compute wins/podiums robustly)
    all_points: dict[str, dict] = {}
    all_places: dict[str, dict] = {}

    # Per-tournament quick stats
    t_meta = []  # will be aligned with #1..#N columns
    for i, t in enumerate(tournaments, start=1):
        key = f"#{i}"
        print(f"Skraper {key}: {t['name']} ({t['date']:%d.%m.%Y})")
        try:
            results = extract_tournament_results(session, t["url"])
        except Exception as e:
            raise RuntimeError(f"Feil ved skraping av '{t['name']}': {e}") from e

        # Convert to points and accumulate
        for player, place in results.items():
            # points
            pts = PLACEMENT_TO_VALUE.get(place, 0)
            all_points.setdefault(player, {})[key] = pts
            # places
            all_places.setdefault(player, {})[key] = place

        # Tournament-level stats
        participants = len(results)
        # Winner (lowest place number); if tie, break by max points (should not happen)
        winner_name, winner_place = None, math.inf
        for p, place in results.items():
            if place < winner_place:
                winner_name, winner_place = p, place
        winner_points = PLACEMENT_TO_VALUE.get(winner_place, 0)

        t_meta.append({
            "key": key,
            "name": t["name"],
            "date": t["date"].isoformat(),
            "url": t["url"],
            "participants": participants,
            "winner": winner_name or "-",
            "winner_points": int(winner_points),
        })

        time.sleep(SLEEP_BETWEEN)  # be polite

    # 3) Build DataFrames
    pts_df = pd.DataFrame.from_dict(all_points, orient="index").fillna(0).astype(int)
    plc_df = pd.DataFrame.from_dict(all_places, orient="index")  # keep as ints/floats (NaN)

    # Ensure tournament columns in correct order (#1..#N)
    league_cols = [f"#{i}" for i in range(1, len(tournaments) + 1)]
    for df in (pts_df, plc_df):
        for c in league_cols:
            if c not in df.columns:
                df[c] = 0 if df is pts_df else math.nan
        df[:] = df[league_cols]  # reorder

    # 4) Player-level metrics
    def count_played(row) -> int:
        # number of >0 scores → participated
        return int((row[league_cols] > 0).sum())

    def avg_points_when_played(row) -> float:
        vals = row[league_cols]
        played_vals = vals[vals > 0]
        return float(round(played_vals.mean(), 2)) if len(played_vals) else 0.0

    def count_wins(name: str) -> int:
        # place == 1 across tournaments
        row = plc_df.loc[name, league_cols]
        return int((row == 1).sum())

    def count_podiums(name: str) -> int:
        row = plc_df.loc[name, league_cols]
        return int((row <= 3).sum())

    def top_n_sum(row, n=TOP_N) -> int:
        vals = sorted([int(v) for v in row[league_cols] if v > 0], reverse=True)
        return int(sum(vals[:n]))

    # Assemble master DF starting from points DF
    df = pts_df.copy()

    # Compute metrics
    df["Spilt"]     = df.apply(count_played, axis=1)
    df["Snitt"]     = df.apply(avg_points_when_played, axis=1)
    df["Topp 17"]   = df.apply(lambda r: top_n_sum(r, TOP_N), axis=1)
    df["Tellende"]  = df["Spilt"].clip(upper=TOP_N)
    df["Seire"]     = [count_wins(name) for name in df.index]
    df["Pallplasseringer"]    = [count_podiums(name) for name in df.index]

    # 5) Tie-breakers: sort by Topp 17 desc, then Seire desc, then best single score desc, then Snitt desc
    best_single = df[league_cols].max(axis=1)
    df = df.assign(_best_single=best_single)
    df = df.sort_values(["Topp 17", "Seire", "_best_single", "Snitt"], ascending=[False, False, False, False])
    df.drop(columns=["_best_single"], inplace=True)

    # 6) Rank column (1-based)
    df.insert(0, "Rank", range(1, len(df) + 1))

    # 7) Reorder columns to desired view
    front_cols = ["Rank", "Topp 17", "Tellende", "Spilt", "Snitt", "Seire", "Pallplasseringer"]
    ordered_cols = front_cols + league_cols
    df = df[ordered_cols]

    # 8) Build meta payload
    meta = {
        "tournaments": t_meta,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top_n": TOP_N,
    }
    return df, meta


def build_df_from_tournament_data(tournaments: list[dict]):
    """Build DataFrame & metadata from already scraped tournament data."""
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


# ----------------------------- HTML rendering ---------------------------------
def df_to_html_file(df: pd.DataFrame, meta: dict, filepath: str,
                    season_label: str, season_links: list[tuple[str, str]]):
    """
    Renders the per-season standings page (table). Changes vs before:
    - No CSV export.
    - Season navigation is a <select>.
    - 'Sesongstatistikk' now points to ONE shared stats.html with a season query param.
    """
    # --- helpers ---
    def start_from_label(lbl: str) -> int:
        # "YYYY/YYYY+1" -> YYYY
        return int(lbl.split("/")[0])

    # League columns
    league_cols = [c for c in df.columns if c.startswith("#")]

    # Minimal payload for small front-end computations
    payload = {
        "tournaments": meta.get("tournaments", []),
        "generated_at": meta.get("generated_at"),
        "top_n": meta.get("top_n", TOP_N),
    }

    # Build season dropdown
    options_html = []
    for lbl, fname in season_links:
        sel = " selected" if lbl == season_label else ""
        options_html.append(f"<option value='{fname}'{sel}>{lbl}</option>")
    select_html = f"<select id='seasonSelect' class='season-select'>{''.join(options_html)}</select>"

    # Link to the single stats page with current season preselected
    s = start_from_label(season_label)
    stats_href = f"stats.html?season={s}-{s+1}"

    # --- HTML/CSS/JS (unchanged layout; CSV removed; season dropdown kept) ---
    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Jærligaen i Bordhockey – {season_label}</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {{ --primary:#1e40af; --primary-600:#1e3a8a; --bg:#0f172a; --panel:#ffffff; --muted:#6b7280; --accent:#f59e0b;
            --border:#e5e7eb; --good:#10b981; --warn:#f59e0b; --bad:#ef4444; --radius:12px; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, system-ui, sans-serif; background:linear-gradient(135deg,#667eea,#764ba2); color:#0f172a; }}
    .wrap {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ color:#fff; text-align:center; margin:8px 0 2px; font-weight:800 }}
    .sub {{ color:#fff; text-align:center; opacity:.9; margin-bottom:8px }}
    .season-nav {{ display:flex; gap:10px; justify-content:center; align-items:center; margin-bottom:16px; color:#fff; }}
    .season-select {{ padding:10px 12px; border:1px solid var(--border); border-radius:10px; min-width:220px; }}
    .toolbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:center; margin:16px 0 20px; }}
    .btn {{ border:1px solid var(--border); background:#fff; padding:10px 14px; border-radius:10px; cursor:pointer; font-weight:600; text-decoration:none; display:inline-block; }}
    .btn.primary {{ background:var(--primary); color:#fff; border-color:var(--primary-600); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin:14px 0 22px; }}
    .card {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; text-align:center; box-shadow: 0 8px 20px rgba(0,0,0,.06); }}
    .card h3 {{ margin:0; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
    .val {{ font-size:26px; font-weight:800; margin-top:6px; color:var(--primary) }}
    .tablebox {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
    .scroll {{ overflow:auto; }}
    table {{ border-collapse:collapse; width:100%; font-size:14px; }}
    thead th {{ position:sticky; top:0; background:#f8fafc; border-bottom:2px solid var(--border); padding:10px; text-align:center; white-space:nowrap; cursor:pointer; user-select:none; }}
    th:first-child, td:first-child {{ position:sticky; left:0; z-index:2; background:#fff; border-right:1px solid var(--border); text-align:left; }}
    th:nth-child(2), td:nth-child(2) {{ text-align:left; }}
    tbody td {{ border-bottom:1px solid var(--border); padding:10px; text-align:center; white-space:nowrap; }}
    tbody tr:hover {{ background:#f3f4f6; }}
    .rank {{ font-weight:800; color:#111827; }}
    .name {{ font-weight:600; color:#111827; }}
    .topp17 {{ font-weight:800; color:var(--accent); background:#fff7ed; }}
    .chip {{ background:#f1f5f9; border:1px solid var(--border); padding:2px 8px; border-radius:999px; font-size:12px; }}
    .collapsed th.league, .collapsed td.league {{ display:none; }}
    td[data-score="100"]{{ background:rgba(16,185,129,.10); font-weight:800; color:var(--good); }}
    td[data-score="85"] {{ background:rgba(16,185,129,.08); color:var(--good); font-weight:700; }}
    td[data-score="75"] {{ background:rgba(16,185,129,.06); color:var(--good); font-weight:700; }}
    td[data-score="65"],td[data-score="60"],td[data-score="55"],td[data-score="50"]{{ color:var(--warn); font-weight:700; }}
    td[data-score="0"]  {{ color:var(--bad); opacity:.9; }}
    .th-meta {{ display:block; font-size:11px; color:#475569; font-weight:600; }}
    .th-meta a{{ color:inherit; text-decoration:none; border-bottom:1px dashed #94a3b8; }}
    .th-meta small{{ font-weight:500; color:#64748b }}
    .foot {{ color:#fff; text-align:center; margin-top:18px; opacity:.9 }}
    @media (max-width: 860px) {{ .btn, .season-select {{ width:100%; max-width:520px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Jærligaen i Bordhockey</h1>
    <div class="sub">Sesong {season_label} – topp {payload["top_n"]} teller</div>
    <div class="season-nav"><span>Sesong:</span>{select_html}</div>

    <div class="cards" id="stats">
      <div class="card"><h3>Antall spillere</h3><div class="val" id="stat_players">-</div></div>
      <div class="card"><h3>Antall ligaer</h3><div class="val" id="stat_leagues">-</div></div>
      <div class="card"><h3>Maks deltakere</h3><div class="val" id="stat_maxpart">-</div></div>
      <div class="card"><h3>Snitt deltakere</h3><div class="val" id="stat_avgpart">-</div></div>
    </div>

    <div class="toolbar">
    <button id="toggle" class="btn">Vis alle ligaer</button>
    <a href="{stats_href}" class="btn primary">Sesongstatistikk</a>
    </div>


    <div class="tablebox"><div class="scroll"><table id="tbl" class="collapsed">
      <thead><tr>
        <th data-key="Rank" aria-sort="descending">#</th>
        <th data-key="Spiller">Spiller</th>
        <th data-key="Topp 17">Topp 17</th>
        <th data-key="Tellende">Tellende</th>
        <th data-key="Spilt">Spilt</th>
        <th data-key="Snitt">Snitt</th>
        <th data-key="Seire">Seire</th>
        <th data-key="Pallplasseringer">Pallplasseringer</th>"""

    # Tournament headers (same as before)
    date_fmt = "%d.%m"
    tournaments = meta.get("tournaments", [])
    for t in tournaments:
        dshort = datetime.fromisoformat(t["date"]).strftime(date_fmt)
        name = t["name"].replace("&", "&amp;")
        url = t["url"]; part = t["participants"]
        winner = t.get("winner", "-"); wpts = t.get("winner_points", 0)
        html += (f'\n        <th class="league" data-key="{t["key"]}" title="Vinner: {winner} ({wpts})">{t["key"]}'
                 f'<span class="th-meta"><a href="{url}" target="_blank" rel="noopener">{name}</a> · {dshort} · '
                 f'<small>{part} spillere</small></span></th>')
    html += """
      </tr></thead><tbody>
"""

    # Body rows
    for player, row in df.iterrows():
        html += "      <tr>\n"
        html += f'        <td class="rank">{int(row["Rank"])}</td>\n'
        html += f'        <td class="name" data-name="{player.lower()}">{player}</td>\n'
        html += f'        <td class="topp17">{int(row["Topp 17"])}</td>\n'
        html += f'        <td><span class="chip">{int(row["Tellende"])}</span></td>\n'
        html += f'        <td>{int(row["Spilt"])}</td>\n'
        html += f'        <td>{row["Snitt"]:.2f}</td>\n'
        html += f'        <td>{int(row["Seire"])}</td>\n'
        html += f'        <td>{int(row["Pallplasseringer"])}</td>\n'
        for c in league_cols:
            score = int(row[c])
            html += f'        <td class="league" data-score="{score}">{score}</td>\n'
        html += "      </tr>\n"

    payload_json = json.dumps(payload, ensure_ascii=False)
    html += f"""      </tbody></table></div></div>

    <div class="foot">Generert: {datetime.now().strftime("%d.%m.%Y %H:%M")} · Jærligaen i Bordhockey</div>
  </div>

    <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const $ = (s,r=document)=>r.querySelector(s); const $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
    const tbl = $("#tbl"), toggleBtn = $("#toggle");

    const payload = JSON.parse($("#payload").textContent);

    // season dropdown (go to table file)
    $("#seasonSelect").addEventListener("change", e => {{ if(e.target.value) location.href = e.target.value; }});

    // stats cards
    (function(){{ 
      const players = $$("#tbl tbody tr").length;
      const leagues = $$("#tbl thead th.league").length;
      const parts = payload.tournaments.map(t=>t.participants||0);
      const maxp = parts.length?Math.max(...parts):0;
      const avgp = parts.length?Math.round(parts.reduce((a,b)=>a+b,0)/parts.length):0;
      $("#stat_players").textContent=players; $("#stat_leagues").textContent=leagues;
      $("#stat_maxpart").textContent=maxp; $("#stat_avgpart").textContent=avgp;
    }})();

    // toggle league columns
    toggleBtn.addEventListener("click", () => {{ 
      tbl.classList.toggle("collapsed");
      toggleBtn.textContent = tbl.classList.contains("collapsed") ? "Vis alle ligaer" : "Skjul ligaer";
    }});

    // click-to-sort
    let sortState={{key:"Topp 17",dir:"desc"}};
    function cellVal(tr, idx){{ const t=tr.children[idx]?.textContent.trim()||""; const n=Number(t.replace(",", ".")); return isNaN(n)?t:n; }}
    function sortBy(th){{ 
      const ths=$$("#tbl thead th"); const idx=ths.findIndex(x=>x===th);
      const key=th.dataset.key||""; const dir=(sortState.key===key&&sortState.dir==="asc")?"desc":"asc";
      sortState={{key,dir}};
      const rows=$$("#tbl tbody tr");
      rows.sort((a,b)=>{{ const va=cellVal(a,idx), vb=cellVal(b,idx);
        return (typeof va==="number"&&typeof vb==="number") ? (dir==="asc"?va-vb:vb-va)
               : (dir==="asc"?String(va).localeCompare(String(vb)):String(vb).localeCompare(String(va))); }});
      const tb=$("#tbl tbody"); rows.forEach(r=>tb.appendChild(r));
      ths.forEach(h=>h.setAttribute("aria-sort","none"));
      th.setAttribute("aria-sort", dir==="asc" ? "ascending" : "descending");
    }}
    $$("#tbl thead th").forEach(th => th.addEventListener("click", () => sortBy(th)));
  </script>
</body>
</html>"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)


def stats_to_html_file(df: pd.DataFrame, meta: dict, filepath: str,
                       season_label: str, season_links: list[tuple[str, str]]):
    """
    Render a compact stats page for a season:
    - Cards: players, leagues, max participants, avg participants
    - Leaderboards (Top 10): Seire, Pallplasseringer, Topp 17, Snitt (>=1 spilt)
    - Tournament list with date, participants, winner
    - Season dropdown + button back to the table page
    """
    # Build season dropdown (same as table page)
    options_html = []
    table_file_for_current = None
    for lbl, fname in season_links:
        if lbl == season_label:
            table_file_for_current = fname
        sel = " selected" if lbl == season_label else ""
        options_html.append(f"<option value='{fname}'{sel}>{lbl}</option>")
    select_html = f"<select id='seasonSelect' class='season-select'>{''.join(options_html)}</select>"

    # Aggregate numbers
    tournaments = meta.get("tournaments", [])
    parts = [t.get("participants", 0) for t in tournaments]
    maxp = max(parts) if parts else 0
    avgp = round(sum(parts)/len(parts)) if parts else 0

    # Leaderboards (Top 10)
    def top_list(series: pd.Series, n=10):
        # Returns list of (player, value)
        s = series.copy().sort_values(ascending=False).head(n)
        return list(zip(s.index.tolist(), s.astype(int if series.dtype != float else float).tolist()))

    top_wins   = top_list(df["Seire"])
    top_podium = top_list(df["Pallplasseringer"])
    top_topp17 = top_list(df["Topp 17"])
    df_snitt = df[df["Spilt"] > 0].copy()
    top_snitt  = list(zip(df_snitt.sort_values("Snitt", ascending=False).head(10).index.tolist(),
                          df_snitt.sort_values("Snitt", ascending=False).head(10)["Snitt"].round(2).tolist()))

    # Winner count by player (from tournament meta)
    winner_counts = {}
    for t in tournaments:
        w = t.get("winner")
        if w and w != "-":
            winner_counts[w] = winner_counts.get(w, 0) + 1
    top_winners = sorted(winner_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    # Tournament rows
    t_rows = []
    for t in tournaments:
        d = datetime.fromisoformat(t["date"]).strftime("%d.%m.%Y")
        t_rows.append(
            f"<tr><td>{d}</td>"
            f"<td><a href='{t['url']}' target='_blank' rel='noopener'>{t['name']}</a></td>"
            f"<td style='text-align:center'>{int(t.get('participants',0))}</td>"
            f"<td>{t.get('winner','-')} ({int(t.get('winner_points',0))})</td></tr>"
        )
    t_rows_html = "\n".join(t_rows)

    # Small helper to build ordered lists
    def ol(items, fmt=lambda k,v: f"{k} – {v}"):
        return "<ol>" + "".join(f"<li>{fmt(k, v)}</li>" for k, v in items) + "</ol>"

    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sesongstatistikk – {season_label}</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {{ --primary:#1e40af; --primary-600:#1e3a8a; --border:#e5e7eb; --radius:12px; --muted:#6b7280; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, system-ui, sans-serif; background:#f5f7fb; color:#0f172a; }}
    .wrap {{ max-width:1200px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 4px; font-weight:800; }}
    .sub {{ margin:0 0 16px; color:#334155 }}
    .season-nav {{ display:flex; gap:10px; align-items:center; margin-bottom:16px; }}
    .season-select {{ padding:10px 12px; border:1px solid var(--border); border-radius:10px; min-width:220px; }}
    .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; margin: 8px 0 16px; }}
    .btn {{ border:1px solid var(--border); background:#fff; padding:10px 14px; border-radius:10px; cursor:pointer; font-weight:600; text-decoration:none; display:inline-block; }}
    .btn.primary {{ background:var(--primary); color:#fff; border-color:var(--primary-600); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }}
    .card {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; }}
    .card h3 {{ margin:0 0 8px; color:#0f172a }}
    .val {{ font-size:26px; font-weight:800; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
    th, td {{ padding:10px; border-bottom:1px solid var(--border); }}
    th {{ text-align:left; background:#f8fafc; }}
    tr:last-child td {{ border-bottom:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Sesongstatistikk</h1>
    <div class="sub">{season_label}</div>

    <div class="season-nav">
      <span>Sesong:</span>
      {select_html}
      <a class="btn" href="{table_file_for_current}">Til tabellen</a>
    </div>

    <div class="grid" style="margin:8px 0 16px">
      <div class="card"><h3>Antall spillere</h3><div class="val">{len(df)}</div></div>
      <div class="card"><h3>Antall ligaer</h3><div class="val">{len(tournaments)}</div></div>
      <div class="card"><h3>Maks deltakere</h3><div class="val">{maxp}</div></div>
      <div class="card"><h3>Snitt deltakere</h3><div class="val">{avgp}</div></div>
    </div>

    <div class="grid">
      <div class="card"><h3>Flest seire (Top 10)</h3>{ol(top_wins)}</div>
      <div class="card"><h3>Flest Pallplasseringer (Top 10)</h3>{ol(top_podium)}</div>
      <div class="card"><h3>Høyest Topp 17 (Top 10)</h3>{ol(top_topp17)}</div>
      <div class="card"><h3>Høyest snitt (Top 10)</h3>{ol(top_snitt, fmt=lambda k,v: f"{k} – {v:.2f}")}</div>
      <div class="card"><h3>Flest turneringsseire (Top 10)</h3>{ol(top_winners)}</div>
    </div>

    <h2 style="margin-top:18px;">Turneringer</h2>
    <table>
      <thead><tr><th>Dato</th><th>Navn</th><th style="text-align:center">Deltakere</th><th>Vinner (poeng)</th></tr></thead>
      <tbody>
        {t_rows_html}
      </tbody>
    </table>
  </div>

  <script>
    // Season dropdown navigation on stats page
    const sel = document.getElementById('seasonSelect');
    sel.addEventListener('change', (e) => {{
      const file = e.target.value;
      if (file) window.location.href = file.replace('.html','-stats.html'); // keep user on the stats view
    }});
  </script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
        
def _season_stats_payload(df: pd.DataFrame, meta: dict) -> dict:
    """Precompute all stats used by the stats UI for one season."""
    tournaments = meta.get("tournaments", [])
    parts = [int(t.get("participants", 0)) for t in tournaments]
    maxp = max(parts) if parts else 0
    avgp = round(sum(parts)/len(parts)) if parts else 0

    # Leaderboards
    def top_list(series: pd.Series, n=10):
        s = series.sort_values(ascending=False).head(n)
        # int where possible, else keep float (for Snitt)
        vals = [float(v) if isinstance(v, float) or "float" in str(s.dtype) else int(v) for v in s.tolist()]
        return [{"name": p, "val": v} for p, v in zip(s.index.tolist(), vals)]

    top_wins   = top_list(df["Seire"])
    top_podium = top_list(df["Pallplasseringer"])
    top_topp17 = top_list(df["Topp 17"])
    df_snitt = df[df["Spilt"] > 0].copy()
    top_snitt = top_list(df_snitt["Snitt"])

    # Winner counts from meta
    winner_counts = {}
    for t in tournaments:
        w = t.get("winner")
        if w and w != "-":
            winner_counts[w] = winner_counts.get(w, 0) + 1
    top_winners = [{"name": k, "val": v} for k, v in sorted(winner_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]

    # Tournaments table
    t_rows = [{
        "date": datetime.fromisoformat(t["date"]).strftime("%d.%m.%Y"),
        "name": t["name"],
        "url": t["url"],
        "participants": int(t.get("participants", 0)),
        "winner": t.get("winner", "-"),
        "winner_points": int(t.get("winner_points", 0)),
    } for t in tournaments]

    return {
        "players": int(len(df)),
        "leagues": int(len(tournaments)),
        "maxp": int(maxp),
        "avgp": int(avgp),
        "topWins": top_wins,
        "topPodiums": top_podium,
        "topTopp17": top_topp17,
        "topSnitt": top_snitt,
        "topWinners": top_winners,
        "tournaments": t_rows,
    }


def stats_all_seasons_to_html(season_labels_order: list[str],
                              label_to_stats: dict[str, dict],
                              filepath: str):
    """
    Build ONE 'stats.html' page that can display stats for any season via dropdown.
    - Reads ?season=YYYY-YYYY+1 (optional) to preselect season.
    - Has a 'Til tabellen' button that jumps to the selected season's table file.
    """
    # Dropdown
    options_html = "".join(
        f"<option value='{lbl}'>{lbl}</option>" for lbl in season_labels_order
    )

    # Preload all stats payload into the page
    payload = {
        "order": season_labels_order,
        "seasons": label_to_stats,  # { "YYYY/YYYY+1": {stats...}, ... }
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sesongstatistikk</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {{ --primary:#1e40af; --primary-600:#1e3a8a; --border:#e5e7eb; --radius:12px; --muted:#6b7280; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, system-ui, sans-serif; background:#f5f7fb; color:#0f172a; }}
    .wrap {{ max-width:1200px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-weight:800; }}
    .sub {{ margin:0 0 16px; color:#334155 }}
    .season-nav {{ display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }}
    .season-select {{ padding:10px 12px; border:1px solid var(--border); border-radius:10px; min-width:220px; }}
    .btn {{ border:1px solid var(--border); background:#fff; padding:10px 14px; border-radius:10px; cursor:pointer; font-weight:600; text-decoration:none; display:inline-block; }}
    .btn.primary {{ background:var(--primary); color:#fff; border-color:var(--primary-600); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }}
    .card {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; }}
    .card h3 {{ margin:0 0 8px; color:#0f172a }}
    .val {{ font-size:26px; font-weight:800; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
    th, td {{ padding:10px; border-bottom:1px solid var(--border); }}
    th {{ text-align:left; background:#f8fafc; }}
    tr:last-child td {{ border-bottom:none; }}
    ol {{ margin: 0 0 0 18px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Sesongstatistikk</h1>
    <div class="sub" id="seasonTitle">-</div>

    <div class="season-nav">
      <span>Sesong:</span>
      <select id="seasonSelect" class="season-select">{options_html}</select>
      <a id="toTable" class="btn">Til tabellen</a>
    </div>

    <div class="grid" style="margin:8px 0 16px">
      <div class="card"><h3>Antall spillere</h3><div class="val" id="stat_players">-</div></div>
      <div class="card"><h3>Antall ligaer</h3><div class="val" id="stat_leagues">-</div></div>
      <div class="card"><h3>Maks deltakere</h3><div class="val" id="stat_maxp">-</div></div>
      <div class="card"><h3>Snitt deltakere</h3><div class="val" id="stat_avgp">-</div></div>
    </div>

    <div class="grid">
      <div class="card"><h3>Flest seire (Top 10)</h3><ol id="ol_wins"></ol></div>
      <div class="card"><h3>Flest Pallplasseringer (Top 10)</h3><ol id="ol_podiums"></ol></div>
      <div class="card"><h3>Høyest Topp 17 (Top 10)</h3><ol id="ol_topp17"></ol></div>
      <div class="card"><h3>Høyest snitt (Top 10)</h3><ol id="ol_snitt"></ol></div>
      <div class="card"><h3>Flest turneringsseire (Top 10)</h3><ol id="ol_winners"></ol></div>
    </div>

    <h2 style="margin-top:18px;">Turneringer</h2>
    <table>
      <thead><tr><th>Dato</th><th>Navn</th><th style="text-align:center">Deltakere</th><th>Vinner (poeng)</th></tr></thead>
      <tbody id="tbody_tours"></tbody>
    </table>
  </div>

    <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('payload').textContent);
    const $ = (s,r=document)=>r.querySelector(s);

    // map "YYYY/YYYY+1" -> "YYYY-YYYY+1.html"
    function tableFileFromLabel(lbl){{ const y=parseInt(lbl.split('/')[0]); return `${{y}}-${{y+1}}.html`; }}

    function render(label){{ 
      const s = data.seasons[label]; if(!s) return;

      // header
      $('#seasonTitle').textContent = label;
      $('#seasonSelect').value = label;
      $('#toTable').setAttribute('href', tableFileFromLabel(label));

      // cards
      $('#stat_players').textContent = s.players;
      $('#stat_leagues').textContent = s.leagues;
      $('#stat_maxp').textContent = s.maxp;
      $('#stat_avgp').textContent = s.avgp;

      // helper to fill <ol>
      function fillList(olId, arr, fmt=(x)=>`${{x.name}} – ${{x.val}}`){{ 
        const ol = $(olId); ol.innerHTML = '';
        arr.forEach(x=>{{ 
          const li = document.createElement('li');
          li.textContent = fmt(x);
          ol.appendChild(li);
        }});
      }}
      fillList('#ol_wins', s.topWins);
      fillList('#ol_podiums', s.topPodiums);
      fillList('#ol_topp17', s.topTopp17);
      fillList('#ol_snitt', s.topSnitt, x => `${{x.name}} – ${{Number(x.val).toFixed(2)}}`);
      fillList('#ol_winners', s.topWinners);

      // tournaments table
      const tb = $('#tbody_tours'); tb.innerHTML = '';
      s.tournaments.forEach(t=>{{ 
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${{t.date}}</td>
                        <td><a href="${{t.url}}" target="_blank" rel="noopener">${{t.name}}</a></td>
                        <td style="text-align:center">${{t.participants}}</td>
                        <td>${{t.winner}} (${{t.winner_points}})</td>`;
        tb.appendChild(tr);
      }});
    }}

    // init: use ?season=YYYY-YYYY+1 if present; else latest in payload.order
    const q = new URLSearchParams(location.search).get('season');
    const byDash = (s)=> s && s.includes('-') ? `${{s.split('-')[0]}}/${{parseInt(s.split('-')[0])+1}}` : null;
    const startLabel = q ? byDash(q) : data.order[data.order.length-1];

    document.getElementById('seasonSelect').addEventListener('change', (e)=>render(e.target.value));
    render(startLabel);
  </script>
</body>
</html>"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

def build_global_stats(label_to_dfmeta: dict[str, tuple[pd.DataFrame, dict]], top_k: int = 10) -> dict:
    """
    Build cross-season stats:
      - season champions (who won most seasons)
      - season podiums (who reached top 3 most)
      - most league participations overall (sum of 'Spilt' across seasons)
      - tournament wins overall (count of tournament winners across all seasons)
      - per-season series for charts/tables: players, leagues, max/avg participants
    Returns a JSON-serializable dict payload for the HTML renderer.
    """
    from collections import Counter, defaultdict

    # Keep seasons in chronological order
    season_labels = sorted(label_to_dfmeta.keys(), key=lambda s: int(s.split("/")[0]))

    champions = Counter()
    podiums   = Counter()
    leagues_attended = Counter()   # "Spilt" summed over seasons
    tour_wins = Counter()

    seasons_series = []        # labels
    players_series = []        # unique players per season
    leagues_series = []        # # tournaments per season
    max_part_series = []       # max participants in any tournament that season
    avg_part_series = []       # average participants across tournaments that season

    for label in season_labels:
        df, meta = label_to_dfmeta[label]
        tournaments = meta.get("tournaments", [])

        # Series for charts
        seasons_series.append(label)
        players_series.append(int(len(df)))
        leagues_series.append(int(len(tournaments)))
        parts = [int(t.get("participants", 0)) for t in tournaments]
        max_part_series.append(int(max(parts)) if parts else 0)
        avg_part_series.append(int(round(sum(parts) / len(parts))) if parts else 0)

        if not df.empty:
            # Season champion = rank 1 (first row after your sort)
            season_winner = df.index[0]
            champions[season_winner] += 1

            # Season podiums (top 3)
            for p in df.head(3).index.tolist():
                podiums[p] += 1

            # League participations across all seasons (sum 'Spilt')
            # NB: names must match exactly across seasons (your scraper already normalizes whitespace)
            for player, spilt in df["Spilt"].items():
                leagues_attended[player] += int(spilt)

        # Tournament wins overall (per tournament winner)
        for t in tournaments:
            w = t.get("winner") or "-"
            if w != "-":
                tour_wins[w] += 1

    def top_list(counter: Counter, k=top_k, sort_key=lambda kv: (-kv[1], kv[0].lower())):
        items = sorted(counter.items(), key=sort_key)[:k]
        return [{"name": n, "val": int(v)} for n, v in items]

    payload = {
        "seasons": seasons_series,
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
    """
    Build ONE 'stats.html' that shows cross-season leaderboards and season-by-season trends.
    Uses Chart.js via CDN. No per-season dropdown here (goal: one-glance overview).
    """
    import json
    payload_json = json.dumps(global_payload, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Jærligaen – Sesongoversikt</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {{ --primary:#1e40af; --primary-600:#1e3a8a; --border:#e5e7eb; --radius:12px; --muted:#6b7280; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, system-ui, sans-serif; background:#f5f7fb; color:#0f172a; }}
    .wrap {{ max-width:1200px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-weight:800; }}
    .sub {{ margin:0 0 16px; color:#334155 }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; }}
    .card {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; }}
    .card h3 {{ margin:0 0 8px; font-weight:700; }}
    .card p {{ margin:0; color:#475569; }}
    .val {{ font-size:26px; font-weight:800; }}
    ol {{ margin:0 0 0 18px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
    th, td {{ padding:10px; border-bottom:1px solid var(--border); font-size:14px; }}
    th {{ text-align:left; background:#f8fafc; }}
    tr:last-child td {{ border-bottom:none; }}
    .charts {{ display:grid; grid-template-columns:1fr; gap:14px; }}
    @media (min-width: 900px) {{ .charts {{ grid-template-columns:1fr 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Sesongoversikt – Jærligaen</h1>
    <div class="sub">Topp-lister på tvers av alle sesonger + utvikling per sesong</div>

    <div class="grid" style="margin-bottom:16px;">
      <div class="card">
        <h3>Flest sesongtitler (Top 10)</h3>
        <ol id="ol_titles"></ol>
      </div>
      <div class="card">
        <h3>Flest sesong-Pallplasseringer (Top 10)</h3>
        <ol id="ol_podiums"></ol>
      </div>
      <div class="card">
        <h3>Flest ligadeltakelser totalt (Top 10)</h3>
        <ol id="ol_attended"></ol>
      </div>
      <div class="card">
        <h3>Flest turneringsseire totalt (Top 10)</h3>
        <ol id="ol_twins"></ol>
      </div>
    </div>

    <div class="charts">
      <div class="card">
        <h3>Maks deltakere pr. sesong</h3>
        <canvas id="chMax"></canvas>
      </div>
      <div class="card">
        <h3>Snitt deltakere pr. sesong</h3>
        <canvas id="chAvg"></canvas>
      </div>
      <div class="card">
        <h3>Antall unike spillere pr. sesong</h3>
        <canvas id="chPlayers"></canvas>
      </div>
      <div class="card">
        <h3>Antall ligaer pr. sesong</h3>
        <canvas id="chLeagues"></canvas>
      </div>
    </div>

    <h2 style="margin:18px 0 8px;">Tabell – nøkkeltall pr. sesong</h2>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Sesong</th>
            <th>Unike spillere</th>
            <th>Antall ligaer</th>
            <th>Maks deltakere</th>
            <th>Snitt deltakere</th>
          </tr>
        </thead>
        <tbody id="tbodySeason"></tbody>
      </table>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    // -------- helpers --------
    const data = JSON.parse(document.getElementById('payload').textContent);
    const $ = (s,r=document)=>r.querySelector(s);

    function fillOrderedList(olEl, arr) {{
      olEl.innerHTML = "";
      arr.forEach(item => {{
        const li = document.createElement("li");
        li.textContent = item.name + " – " + item.val;
        olEl.appendChild(li);
      }});
    }}

    function renderLeaderboards() {{
      fillOrderedList(document.getElementById("ol_titles"),   data.leaderboards.seasonTitles);
      fillOrderedList(document.getElementById("ol_podiums"),  data.leaderboards.seasonPodiums);
      fillOrderedList(document.getElementById("ol_attended"), data.leaderboards.mostLeaguesAttended);
      fillOrderedList(document.getElementById("ol_twins"),    data.leaderboards.tournamentWins);
    }}

    function renderSeasonTable() {{
      const tb = document.getElementById("tbodySeason");
      tb.innerHTML = "";
      const seasons = data.seasons;
      const players = data.series.players;
      const leagues = data.series.leagues;
      const maxP = data.series.maxParticipants;
      const avgP = data.series.avgParticipants;
      for (let i=0; i<seasons.length; i++) {{
        const tr = document.createElement("tr");
        const td0 = document.createElement("td"); td0.textContent = seasons[i];
        const td1 = document.createElement("td"); td1.textContent = players[i];
        const td2 = document.createElement("td"); td2.textContent = leagues[i];
        const td3 = document.createElement("td"); td3.textContent = maxP[i];
        const td4 = document.createElement("td"); td4.textContent = avgP[i];
        tr.appendChild(td0); tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); tr.appendChild(td4);
        tb.appendChild(tr);
      }}
    }}

    function lineChart(id, labels, series, label) {{
      const ctx = document.getElementById(id).getContext('2d');
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [{{ label: label, data: series, tension: 0.25, fill: false }}]
        }},
        options: {{
          responsive: true,
          scales: {{
            x: {{ ticks: {{ autoSkip: true, maxRotation: 0 }} }},
            y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }}
          }},
          plugins: {{
            legend: {{ display: false }}
          }}
        }}
      }});
    }}

    function renderCharts() {{
      const labels = data.seasons;
      lineChart("chMax",     labels, data.series.maxParticipants, "Maks deltakere");
      lineChart("chAvg",     labels, data.series.avgParticipants, "Snitt deltakere");
      lineChart("chPlayers", labels, data.series.players,         "Unike spillere");
      lineChart("chLeagues", labels, data.series.leagues,         "Antall ligaer");
    }}

    // init
    renderLeaderboards();
    renderSeasonTable();
    renderCharts();
  </script>
</body>
</html>"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)



# ----------------------------------- Main -------------------------------------

def main():
    """Rebuild pages + one global stats.html (excludes 2006/2007)."""
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

    # Latest season -> index.html
    latest_label = season_label(current_start)
    latest_file = season_filename(current_start)
    if os.path.exists(latest_file):
        with open(latest_file, "r", encoding="utf-8") as src, open("index.html", "w", encoding="utf-8") as dst:
            dst.write(src.read())

    print("✅ Ferdig")


if __name__ == "__main__":
    main()
