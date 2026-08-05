# AGENTS.md

## Repository Overview
`jarligatabell` is an automated static website generator for **Jærligaen** (a Norwegian table hockey league). It scrapes tournament results from the official ITHF database (`stiga.trefik.cz`), calculates standings based on league scoring rules, caches historical tournament data in `tournaments.json`, and generates responsive static HTML standings pages (`index.html`, `stats.html`, and `YYYY-YYYY.html` season archives).

## Architecture & Project Structure
- [tabell_generator.py](file:///Users/amundfylling/Downloads/jarligatabell/tabell_generator.py): Primary Python script handling data fetching, parsing, ranking calculations, all-time stats aggregation, and HTML generation.
- [tournaments.json](file:///Users/amundfylling/Downloads/jarligatabell/tournaments.json): Persistent JSON cache storing scraped tournament details, dates, participants, and player placement results.
- [index.html](file:///Users/amundfylling/Downloads/jarligatabell/index.html): Landing page displaying standings for the current season (generated as a copy of the latest season HTML file).
- [stats.html](file:///Users/amundfylling/Downloads/jarligatabell/stats.html): Cross-season leaderboard displaying top 10 titles, podiums, tournament wins, and overall participation records.
- `YYYY-YYYY.html` (e.g., [2025-2026.html](file:///Users/amundfylling/Downloads/jarligatabell/2025-2026.html)): Historical and active season standings archives.
- [.github/workflows/update.yml](file:///Users/amundfylling/Downloads/jarligatabell/.github/workflows/update.yml): Scheduled GitHub Action running weekly (Tuesdays 23:00 UTC) or manually to rebuild the site and auto-commit updates.
- [CNAME](file:///Users/amundfylling/Downloads/jarligatabell/CNAME): Configures the custom domain `jarligatabell.puck.no`.

## Tech Stack & Dependencies
- **Language**: Python 3.12+
- **Key Dependencies**:
  - `pandas`: Standing dataframes, sorting, and ranking tiebreaker logic.
  - `beautifulsoup4` & `lxml`: Parsing HTML tables from ITHF (`stiga.trefik.cz`).
  - `requests`: Session handling for HTTP requests with custom User-Agent headers.
  - `openpyxl`: Excel handling support if required.

## Key Business Logic & League Rules
1. **Season Window**: A season runs from **July 1** of year $Y$ to **June 30** of year $Y+1$ (e.g., July 1, 2025 – June 30, 2026).
2. **Point Scale (`PLACEMENT_TO_VALUE`)**:
   - 1st: 100, 2nd: 85, 3rd: 75, 4th: 65, 5th: 60, 6th: 55, 7th: 50, ..., 50th: 2 pts.
3. **Top 17 Counting Rule (`TOP_N = 17`)**: Only a player's top 17 best tournament scores count toward their season total ("Topp 17").
4. **Tiebreakers**:
   1. Highest total "Topp 17" points (descending)
   2. Most individual tournament wins (`Seire`) (descending)
   3. Highest single tournament score (descending)
5. **Scraping Safety & Polite Rate-Limiting**:
   - Request timeout: 20 seconds (`HTTP_TIMEOUT`).
   - Sleep delay: 0.4 seconds between requests (`SLEEP_BETWEEN`).
   - Cache-first strategy: Tournaments already present in `tournaments.json` are skipped.

## How to Run & Verify
1. **Install Dependencies**:
   ```bash
   pip install pandas beautifulsoup4 requests lxml openpyxl
   ```
2. **Execute Site Rebuild**:
   ```bash
   python tabell_generator.py
   ```
3. **Output Verification**:
   - Check `index.html` and `stats.html` for clean HTML generation.
   - Verify `tournaments.json` is formatted cleanly and updated.

## AI Agent Guidelines
- **Preserve Data Cache**: Treat `tournaments.json` as the source of truth for cached raw data. Do not wipe or restructure it without backwards compatibility.
- **UTF-8 File Operations**: Always open files with explicit `encoding="utf-8"`.
- **Minimal Git Diffs**: Keep generated HTML structure clean and deterministic to prevent unnecessary git diff churn during automated runs.
