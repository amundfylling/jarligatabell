"""
Jærligaen i Bordhockey – improved output generator
-----------------------------------------------
Key upgrades vs your original:
1) Richer HTML: search, click-to-sort, CSV export, sticky first column, better stats,
   tournament headers with date + link + participants, winner badges, and rank numbers.
2) Better player metrics: Rank, Spilt (played), Snitt (avg points when played),
   Tellende (counted in Topp 17), Seire (wins), Podier (<=3. plass).
3) More robust scraping: timeouts, session reuse, clearer exceptions.
4) Cleaner column order: [Rank, Spiller, Topp 17, Tellende, Spilt, Snitt, Seire, Podier, #1..#N].

Usage remains the same (see main()).
"""

import json
import math
import time
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
          ['Rank','Topp 17','Tellende','Spilt','Snitt','Seire','Podier', '#1', '#2', ...]
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
    df["Podier"]    = [count_podiums(name) for name in df.index]

    # 5) Tie-breakers: sort by Topp 17 desc, then Seire desc, then best single score desc, then Snitt desc
    best_single = df[league_cols].max(axis=1)
    df = df.assign(_best_single=best_single)
    df = df.sort_values(["Topp 17", "Seire", "_best_single", "Snitt"], ascending=[False, False, False, False])
    df.drop(columns=["_best_single"], inplace=True)

    # 6) Rank column (1-based)
    df.insert(0, "Rank", range(1, len(df) + 1))

    # 7) Reorder columns to desired view
    front_cols = ["Rank", "Topp 17", "Tellende", "Spilt", "Snitt", "Seire", "Podier"]
    ordered_cols = front_cols + league_cols
    df = df[ordered_cols]

    # 8) Build meta payload
    meta = {
        "tournaments": t_meta,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top_n": TOP_N,
    }
    return df, meta


# ----------------------------- HTML rendering ---------------------------------

def df_to_html_file(df: pd.DataFrame, meta: dict, filepath: str):
    """
    Render DataFrame + metadata to a modern, interactive HTML.

    - Sticky first column
    - Search field
    - Click-to-sort on any header
    - Toggle to show/hide all league columns
    - CSV export (current full table)
    - Tournament headers include date + link + participants, tooltip with winner

    Parameters
    ----------
    df : DataFrame from build_results_dataframe()
    meta : dict from build_results_dataframe()
    filepath : output .html path
    """
    # Column groups
    league_cols = [c for c in df.columns if c.startswith("#")]
    # Build a compact JSON payload for front-end utilities (CSV export, stats)
    payload = {
        "columns": ["Spiller"] + [c for c in df.columns],  # we will inject 'Spiller' separately in the HTML
        "tournaments": meta.get("tournaments", []),
        "generated_at": meta.get("generated_at"),
        "top_n": meta.get("top_n", TOP_N),
    }

    # HTML head & style
    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Jærligaen i Bordhockey – Sesongresultater</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #1e40af;
      --primary-600:#1e3a8a;
      --bg:#0f172a;
      --panel:#ffffff;
      --muted:#6b7280;
      --accent:#f59e0b;
      --border:#e5e7eb;
      --good:#10b981;
      --warn:#f59e0b;
      --bad:#ef4444;
      --radius:12px;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; font-family:Inter, system-ui, sans-serif; background:linear-gradient(135deg,#667eea,#764ba2);
      color:#0f172a; min-height:100vh;
    }}
    .wrap {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ color:#fff; text-align:center; margin:8px 0 2px; font-weight:800 }}
    .sub {{ color:#fff; text-align:center; opacity:.9; margin-bottom:16px }}
    .toolbar {{
      display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:center; margin:16px 0 20px;
    }}
    .toolbar input[type="search"] {{
      padding:10px 12px; border:1px solid var(--border); border-radius:10px; min-width:280px;
    }}
    .btn {{
      border:1px solid var(--border); background:#fff; padding:10px 14px; border-radius:10px; cursor:pointer;
      font-weight:600;
    }}
    .btn.primary {{ background:var(--primary); color:#fff; border-color:var(--primary-600); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin:14px 0 22px; }}
    .card {{
      background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:14px; text-align:center;
      box-shadow: 0 8px 20px rgba(0,0,0,.06);
    }}
    .card h3 {{ margin:0; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
    .val {{ font-size:26px; font-weight:800; margin-top:6px; color:var(--primary) }}

    .tablebox {{ background:#fff; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
    .scroll {{ overflow:auto; }}
    table {{ border-collapse:collapse; width:100%; font-size:14px; }}
    thead th {{
      position:sticky; top:0; background:#f8fafc; border-bottom:2px solid var(--border);
      padding:10px; text-align:center; white-space:nowrap; cursor:pointer; user-select:none;
    }}
    th:first-child, td:first-child {{
      position:sticky; left:0; z-index:2; background:#fff; border-right:1px solid var(--border);
      text-align:left;
    }}
    th:nth-child(2), td:nth-child(2) {{ text-align:left; }}
    tbody td {{ border-bottom:1px solid var(--border); padding:10px; text-align:center; white-space:nowrap; }}
    tbody tr:hover {{ background:#f3f4f6; }}
    .rank {{ font-weight:800; color:#111827; }}
    .name {{ font-weight:600; color:#111827; }}
    .topp17 {{ font-weight:800; color:var(--accent); background:#fff7ed; }}
    .chip {{ background:#f1f5f9; border:1px solid var(--border); padding:2px 8px; border-radius:999px; font-size:12px; }}
    .collapsed th.league, .collapsed td.league {{ display:none; }}
    /* score color cues */
    td[data-score="100"]{{ background:rgba(16,185,129,.10); font-weight:800; color:var(--good); }}
    td[data-score="85"] {{ background:rgba(16,185,129,.08); color:var(--good); font-weight:700; }}
    td[data-score="75"] {{ background:rgba(16,185,129,.06); color:var(--good); font-weight:700; }}
    td[data-score="65"],td[data-score="60"],td[data-score="55"],td[data-score="50"]{{ color:var(--warn); font-weight:700; }}
    td[data-score="0"]  {{ color:var(--bad); opacity:.9; }}
    .th-meta {{ display:block; font-size:11px; color:#475569; font-weight:600; }}
    .th-meta a{{ color:inherit; text-decoration:none; border-bottom:1px dashed #94a3b8; }}
    .th-meta small{{ font-weight:500; color:#64748b }}
    .foot {{ color:#fff; text-align:center; margin-top:18px; opacity:.9 }}
    @media (max-width: 860px) {{
      .btn, input[type="search"] {{ width:100%; max-width:520px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Jærligaen i Bordhockey</h1>
    <div class="sub">Ligatabell - topp {payload["top_n"]} teller</div>

    <div class="cards" id="stats">
      <div class="card"><h3>Antall spillere</h3><div class="val" id="stat_players">-</div></div>
      <div class="card"><h3>Antall ligaer</h3><div class="val" id="stat_leagues">-</div></div>
      <div class="card"><h3>Maks antall deltakere</h3><div class="val" id="stat_maxpart">-</div></div>
      <div class="card"><h3>Snitt antall pr. liga</h3><div class="val" id="stat_avgpart">-</div></div>
    </div>

    <div class="toolbar">
      <input id="search" type="search" placeholder="Søk spiller … (f.eks. 'Nygård')" />
      <button id="toggle" class="btn">Vis alle ligaer</button>
      <button id="exportCsv" class="btn primary">Last ned CSV</button>
    </div>

    <div class="tablebox">
      <div class="scroll">
        <table id="tbl" class="collapsed">
          <thead>
            <tr>
              <th data-key="Rank" aria-sort="descending">#</th>
              <th data-key="Spiller">Spiller</th>
              <th data-key="Topp 17">Topp 17</th>
              <th data-key="Tellende">Tellende</th>
              <th data-key="Spilt">Spilt</th>
              <th data-key="Snitt">Snitt</th>
              <th data-key="Seire">Seire</th>
              <th data-key="Podier">Podier</th>"""

    # Tournament headers with meta (date, participants, link)
    # We’ll emit e.g.: <th class="league" data-key="#1">#1 <span class="th-meta"><a ...>Name</a> · 20.08 · <small>24 spillere</small></span></th>
    date_fmt = "%d.%m"
    tournaments = meta.get("tournaments", [])
    for t in tournaments:
        dshort = datetime.fromisoformat(t["date"]).strftime(date_fmt)
        name = t["name"].replace("&", "&amp;")
        url = t["url"]
        part = t["participants"]
        winner = t.get("winner", "-")
        wpts = t.get("winner_points", 0)
        html += f'\n              <th class="league" data-key="{t["key"]}" title="Vinner: {winner} ({wpts})">{t["key"]}' \
                f'<span class="th-meta"><a href="{url}" target="_blank" rel="noopener">{name}</a> · {dshort} · <small>{part} spillere</small></span></th>'
    html += """
            </tr>
          </thead>
          <tbody>
"""

    # Table body rows
    # We need the player name (index), but df currently has no explicit 'Spiller' column; we will iterate index.
    for player, row in df.iterrows():
        html += "            <tr>\n"
        html += f'              <td class="rank">{int(row["Rank"])}</td>\n'
        html += f'              <td class="name" data-name="{player.lower()}">{player}</td>\n'
        html += f'              <td class="topp17">{int(row["Topp 17"])}</td>\n'
        html += f'              <td><span class="chip">{int(row["Tellende"])}</span></td>\n'
        html += f'              <td>{int(row["Spilt"])}</td>\n'
        # Snitt can be float
        snitt_val = f'{row["Snitt"]:.2f}'
        html += f'              <td>{snitt_val}</td>\n'
        html += f'              <td>{int(row["Seire"])}</td>\n'
        html += f'              <td>{int(row["Podier"])}</td>\n'
        for c in league_cols:
            score = int(row[c])
            html += f'              <td class="league" data-score="{score}">{score}</td>\n'
        html += "            </tr>\n"

    # Close table + payload + JS
    payload_json = json.dumps(payload, ensure_ascii=False)
    html += f"""          </tbody>
        </table>
      </div>
    </div>

    <div class="foot">
      Generert: {datetime.now().strftime("%d.%m.%Y %H:%M")} · Jærligaen i Bordhockey
    </div>
  </div>

  <!-- Data payload for utilities -->
  <script type="application/json" id="payload">{payload_json}</script>

  <script>
    // --------------------- Utilities ---------------------
    const $ = (sel, root=document) => root.querySelector(sel);
    const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

    const tbl = $("#tbl");
    const search = $("#search");
    const toggleBtn = $("#toggle");
    const exportBtn = $("#exportCsv");
    const payload = JSON.parse($("#payload").textContent);

    // Stats: players, leagues, max/avg participants
    (function initStats(){{
      const players = $$("#tbl tbody tr").length;
      const leagues = $$("#tbl thead th.league").length;
      const parts = payload.tournaments.map(t => t.participants || 0);
      const maxp = parts.length ? Math.max(...parts) : 0;
      const avgp = parts.length ? Math.round(parts.reduce((a,b)=>a+b,0)/parts.length) : 0;
      $("#stat_players").textContent = players;
      $("#stat_leagues").textContent = leagues;
      $("#stat_maxpart").textContent = maxp;
      $("#stat_avgpart").textContent = avgp;
    }})();

    // Search by player name (case-insensitive)
    search.addEventListener("input", e => {{
      const q = e.target.value.trim().toLowerCase();
      $$("#tbl tbody tr").forEach(tr => {{
        const name = tr.querySelector("td.name")?.dataset.name || "";
        tr.style.display = (!q || name.includes(q)) ? "" : "none";
      }});
    }});

    // Toggle all league columns
    toggleBtn.addEventListener("click", () => {{
      tbl.classList.toggle("collapsed");
      toggleBtn.textContent = tbl.classList.contains("collapsed") ? "Vis alle ligaer" : "Skjul ligaer";
    }});

    // Simple click-to-sort for any header
    let sortState = {{ key: "Topp 17", dir: "desc" }};
    function getCellValue(tr, idx) {{
      const td = tr.children[idx];
      if (!td) return "";
      const raw = td.textContent.trim();
      const num = Number(raw.replace(",", "."));
      return isNaN(num) ? raw : num;
    }}
    function sortByTH(th) {{
      const ths = $$("#tbl thead th");
      const idx = ths.indexOf ? ths.indexOf(th) : ths.findIndex(x => x===th);
      const key = th.dataset.key || "";
      const dir = (sortState.key===key && sortState.dir==="asc") ? "desc" : "asc";
      sortState = {{ key, dir }};

      const rows = $$("#tbl tbody tr");
      rows.sort((a,b) => {{
        const va = getCellValue(a, idx);
        const vb = getCellValue(b, idx);
        if (typeof va === "number" && typeof vb === "number") {{
          return dir==="asc" ? va - vb : vb - va;
        }} else {{
          return dir==="asc" ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
        }}
      }});
      const tbody = $("#tbl tbody");
      rows.forEach(r => tbody.appendChild(r));
      // aria-sort
      ths.forEach(h => h.setAttribute("aria-sort","none"));
      th.setAttribute("aria-sort", dir==="asc" ? "ascending" : "descending");
    }}
    $$("#tbl thead th").forEach(th => {{
      th.addEventListener("click", () => sortByTH(th));
    }});

    // CSV export (entire current table)
    function tableToCSV() {{
      const rows = $$("#tbl tr");
      return rows.map(tr => {{
        const cells = Array.from(tr.children).filter(td => td.style.display!=="none");
        return cells.map(td => {{
          let t = td.textContent.replaceAll("\\n"," ").trim();
          if (t.includes('"') || t.includes(","))
            t = '"' + t.replaceAll('"','""') + '"';
          return t;
        }}).join(",");
      }}).join("\\n");
    }}
    exportBtn.addEventListener("click", () => {{
      const csv = tableToCSV();
      const blob = new Blob([csv], {{type:"text/csv;charset=utf-8;"}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "jarliga_resultater.csv";
      a.click();
      URL.revokeObjectURL(url);
    }});
  </script>
</body>
</html>"""

    # Write file
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------- Main -------------------------------------

def main():
    """
    Run generator end-to-end.
    - Adjust series_page / dates as needed.
    """
    series_page = "https://stiga.trefik.cz/ithf/ranking/serial.aspx?ID=220004"
    start = "15.07.2025"
    end   = "15.06.2026"

    print("🔍 Henter turneringer …")
    df, meta = build_results_dataframe(series_page, start, end)

    # Final HTML
    out_file = "index.html"
    print(f"🎨 Lager HTML: {out_file}")
    df_to_html_file(df, meta, out_file)
    print(f"✅ Ferdig: {out_file}")


if __name__ == "__main__":
    main()
