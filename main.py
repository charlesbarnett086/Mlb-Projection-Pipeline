#!/usr/bin/env python3
"""
MLB DFS Projection Pipeline (DraftKings 5-Man Stacks & Slate-Separated)
────────────────────────────────────────────────────────────────────────
Features:
  - Real Player Names: Official game lineups & active team rosters
  - Timezone & Slate Classification: Python zoneinfo (America/New_York)
  - Dynamic Relative Percentile Colors & Color Key tab export
  - DraftKings Positions: P, C, 1B, 2B, 3B, SS, OF
  - Free API Stack: MLB Stats API, Open-Meteo Weather, The Odds API
"""

import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# ── Logging Setup ────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── API Endpoints & Config ────
MLB_API_BASE     = "https://statsapi.mlb.com/api/v1"
OPEN_METEO_URL   = "https://api.open-meteo.com/v1/forecast"
ODDS_API_URL     = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"

ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
GOOGLE_WEBAPP_URL = os.environ.get("GOOGLE_WEBAPP_URL", "")

STADIUM_COORDS = {
    "Coors Field": (39.756, -104.994),
    "Yankee Stadium": (40.829, -73.926),
    "Fenway Park": (42.346, -71.097),
    "Wrigley Field": (41.948, -87.655),
    "Dodger Stadium": (34.073, -118.240),
    "Default": (39.828, -98.579)
}

DK_POSITION_MAP = {
    1: "SS", 2: "OF", 3: "1B", 4: "3B", 
    5: "OF", 6: "2B", 7: "C",  8: "OF", 9: "OF"
}

TIER_COLORS = {
    "Elite":   "#FFD966",  # Gold (Top 15%)
    "Strong":  "#93C47D",  # Green (Next 20%)
    "Solid":   "#9FC5E8",  # Blue (Next 30%)
    "Average": "#FFFFFF",  # White (Next 20%)
    "Fade":    "#EA9999",  # Red (Bottom 15%)
}
HEADER_COLOR = "#1F4E79"

ROSTER_CACHE = {}

# ═════════════════════════════════════════════════════════════════════════════
# 1. Timezone & Slate Classification
# ═════════════════════════════════════════════════════════════════════════════

def get_slate_info(game_date_utc: str) -> str:
    """Converts UTC game time to accurate Eastern Time and Slate category."""
    if not game_date_utc:
        return "Main Slate"
    try:
        dt_utc = datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        time_str = dt_et.strftime("%I:%M %p ET").lstrip("0")
        et_hour = dt_et.hour
        
        if et_hour < 16:
            return f"Early ({time_str})"
        elif et_hour < 18:
            return f"Afternoon ({time_str})"
        else:
            return f"Main ({time_str})"
    except Exception as e:
        log.warning(f"Error parsing game date '{game_date_utc}': {e}")
        return "Main Slate"

# ═════════════════════════════════════════════════════════════════════════════
# 2. Weather Engine
# ═════════════════════════════════════════════════════════════════════════════

def fetch_game_weather(venue_name: str) -> dict:
    lat, lon = STADIUM_COORDS.get(venue_name, STADIUM_COORDS["Default"])
    params = {
        "latitude": lat, "longitude": lon,
        "current_weather": True, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"
    }
    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("current_weather", {})
        temp = data.get("temperature", 70.0)
        wind = data.get("windspeed", 5.0)
        
        hr_boost = 1.0
        if temp > 78: hr_boost += 0.05
        if temp < 55: hr_boost -= 0.05
        if wind > 12: hr_boost += 0.08
        
        return {"temp": temp, "wind": wind, "hr_factor": round(hr_boost, 2)}
    except Exception as e:
        log.warning(f"Weather fetch failed for {venue_name}: {e}")
        return {"temp": 70.0, "wind": 5.0, "hr_factor": 1.0}

# ═════════════════════════════════════════════════════════════════════════════
# 3. Vegas Totals
# ═════════════════════════════════════════════════════════════════════════════

def fetch_vegas_totals() -> dict:
    if not ODDS_API_KEY:
        log.warning("ODDS_API_KEY missing. Using 4.5 baseline implied runs.")
        return {}
        
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "totals", "oddsFormat": "american"}
    try:
        resp = requests.get(ODDS_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        totals = {}
        for game in resp.json():
            home, away = game.get("home_team"), game.get("away_team")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") == "totals":
                        line = float(mkt["outcomes"][0].get("point", 8.5))
                        totals[home] = line / 2.0
                        totals[away] = line / 2.0
        return totals
    except Exception as e:
        log.warning(f"Odds API error: {e}")
        return {}

# ═════════════════════════════════════════════════════════════════════════════
# 4. MLB Schedule & Active Roster / Lineup Parser
# ═════════════════════════════════════════════════════════════════════════════

def fetch_team_active_hitters(team_id: int) -> list:
    """Fetches real hitter names from active team roster if lineups unposted."""
    if team_id in ROSTER_CACHE:
        return ROSTER_CACHE[team_id]
        
    url = f"{MLB_API_BASE}/teams/{team_id}/roster?rosterType=active"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        hitters = []
        for m in resp.json().get("roster", []):
            pos = m.get("position", {}).get("abbreviation", "OF")
            if pos != "P":
                hitters.append({
                    "name": m.get("person", {}).get("fullName", "Unknown Player"),
                    "pos": pos if pos in ["C", "1B", "2B", "3B", "SS", "OF"] else "OF"
                })
        ROSTER_CACHE[team_id] = hitters
        return hitters
    except Exception as e:
        log.warning(f"Failed roster fetch for team ID {team_id}: {e}")
        return []

def fetch_mlb_slate_data():
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"{MLB_API_BASE}/schedule?sportId=1&date={today}&hydrate=probablePitcher,lineups,venue,team"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        schedule = resp.json()
        games = []
        dates = schedule.get("dates", [])
        if not dates:
            return []
            
        for game in dates[0].get("games", []):
            game_utc = game.get("gameDate", "")
            slate_tag = get_slate_info(game_utc)
            
            home_team = game["teams"]["home"]["team"]
            away_team = game["teams"]["away"]["team"]
            
            lineups = game.get("lineups", {})
            home_lineup = lineups.get("homePlayers", [])
            away_lineup = lineups.get("awayPlayers", [])
            
            if not home_lineup:
                home_lineup = fetch_team_active_hitters(home_team["id"])
            if not away_lineup:
                away_lineup = fetch_team_active_hitters(away_team["id"])

            games.append({
                "slate": slate_tag,
                "home": home_team.get("name"),
                "away": away_team.get("name"),
                "home_id": home_team.get("id"),
                "away_id": away_team.get("id"),
                "home_sp": game["teams"]["home"].get("probablePitcher", {}).get("fullName", "TBD Pitcher"),
                "away_sp": game["teams"]["away"].get("probablePitcher", {}).get("fullName", "TBD Pitcher"),
                "home_lineup": home_lineup,
                "away_lineup": away_lineup,
                "venue": game.get("venue", {}).get("name", "Default"),
            })
        return games
    except Exception as e:
        log.error(f"Failed to load MLB schedule: {e}")
        return []

# ═════════════════════════════════════════════════════════════════════════════
# 5. Percentile-Based Color Assignment Engine
# ═════════════════════════════════════════════════════════════════════════════

def apply_dynamic_percentile_colors(items: list, score_key: str):
    """Assigns cell colors based on relative rank across the entire slate."""
    if not items:
        return
    items.sort(key=lambda x: x[score_key], reverse=True)
    total = len(items)
    
    for idx, item in enumerate(items):
        pct = idx / total
        if pct <= 0.15:
            tier = "Elite"
        elif pct <= 0.35:
            tier = "Strong"
        elif pct <= 0.65:
            tier = "Solid"
        elif pct <= 0.85:
            tier = "Average"
        else:
            tier = "Fade"
        item["color"] = TIER_COLORS[tier]

# ═════════════════════════════════════════════════════════════════════════════
# 6. Projection Engine & 5-Man Stacks
# ═════════════════════════════════════════════════════════════════════════════

def generate_5man_stacks(hitters_list: list, team: str, opp: str, implied_runs: float, hr_factor: float, slate_tag: str) -> list:
    combos = [(1, 2, 3, 4, 5), (1, 2, 3, 4, 9), (2, 3, 4, 5, 6), (1, 3, 4, 5, 6)]
    hitter_map = {h["order_num"]: h for h in hitters_list}
    stacks = []
    
    for combo in combos:
        if all(o in hitter_map for o in combo):
            selected = [hitter_map[o] for o in combo]
            total_salary = sum(h["salary"] for h in selected)
            total_proj = sum(h["proj"] for h in selected)
            total_ceiling = sum(h["ceiling"] for h in selected)
            
            rating = round((total_proj * 0.4) + (total_ceiling * 0.3) + (implied_runs * 1.5) + (hr_factor * 4), 2)
            combo_str = "-".join(str(o) for o in combo)
            names_str = ", ".join([h["name"].split()[-1] for h in selected])
            
            stacks.append({
                "row": [
                    slate_tag, team, opp, f"Combo ({combo_str})", names_str, 
                    total_salary, round(total_proj, 2), round(total_ceiling, 2), rating, implied_runs
                ],
                "rating": rating
            })
            
    return stacks

def build_mlb_projections():
    games = fetch_mlb_slate_data()
    vegas = fetch_vegas_totals()
    
    hitters, pitchers, all_stacks, weather_rows = [], [], [], []
    
    for g in games:
        wx = fetch_game_weather(g["venue"])
        slate_tag = g["slate"]
        
        weather_rows.append([
            slate_tag, f"{g['away']} @ {g['home']}", g['venue'], 
            wx['temp'], wx['wind'], f"{wx['hr_factor']}x HR Factor"
        ])
        
        sides = [
            (g['home'], g['away'], g['away_sp'], g['home_lineup']),
            (g['away'], g['home'], g['home_sp'], g['away_lineup'])
        ]
        
        for team, opp, opp_sp, lineup in sides:
            implied_runs = vegas.get(team, 4.5)
            team_hitters = []
            
            for order in range(1, 10):
                if order - 1 < len(lineup):
                    p_info = lineup[order - 1]
                    name = p_info.get("fullName") or p_info.get("name") or f"{team} Hitter {order}"
                    pos = p_info.get("primaryPosition", {}).get("abbreviation") or p_info.get("pos") or DK_POSITION_MAP[order]
                else:
                    name = f"{team} Hitter {order}"
                    pos = DK_POSITION_MAP[order]
                
                slot_mult = 1.0 - ((order - 1) * 0.06)
                base_proj = round((8.8 * slot_mult) * (implied_runs / 4.5) * wx['hr_factor'], 2)
                ceiling = round(base_proj * 1.85, 2)
                salary = int(2200 + (base_proj * 380))
                leverage = round(ceiling / (salary / 1000), 2)
                
                hitter_item = {
                    "name": name, "order_num": order, "salary": salary, 
                    "proj": base_proj, "ceiling": ceiling, "leverage": leverage,
                    "row": [slate_tag, name, pos, team, opp, f"Order {order}", opp_sp, salary, base_proj, ceiling, leverage]
                }
                
                team_hitters.append(hitter_item)
                hitters.append(hitter_item)
            
            all_stacks.extend(generate_5man_stacks(team_hitters, team, opp, implied_runs, wx['hr_factor'], slate_tag))
            
            if opp_sp and opp_sp != "TBD Pitcher":
                sp_proj = round(12.5 + (5.0 - implied_runs) * 2.3, 2)
                sp_ceiling = round(sp_proj * 1.55, 2)
                sp_salary = int(5200 + (sp_proj * 320))
                
                pitchers.append({
                    "score": sp_proj,
                    "row": [slate_tag, opp_sp, "P", opp, team, sp_salary, sp_proj, sp_ceiling, f"{implied_runs} Implied Runs"]
                })

    apply_dynamic_percentile_colors(hitters, "leverage")
    apply_dynamic_percentile_colors(pitchers, "score")
    apply_dynamic_percentile_colors(all_stacks, "rating")

    return hitters, pitchers, all_stacks, weather_rows

# ═════════════════════════════════════════════════════════════════════════════
# 7. Google Sheets Exporter
# ═════════════════════════════════════════════════════════════════════════════

def post_to_sheets(tab: str, headers: list, items: list) -> bool:
    if not GOOGLE_WEBAPP_URL:
        log.error("GOOGLE_WEBAPP_URL not configured.")
        return False
        
    rows = [headers] + [i["row"] if isinstance(i, dict) else i for i in items]
    row_colors = [HEADER_COLOR] + [i["color"] if isinstance(i, dict) else "#FFFFFF" for i in items]
    
    payload = {"tab": tab, "clear": True, "rows": rows, "rowColors": row_colors}
    try:
        resp = requests.post(GOOGLE_WEBAPP_URL, json=payload, timeout=20)
        log.info(f"Exported tab '{tab}': {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Export error for {tab}: {e}")
        return False

# ═════════════════════════════════════════════════════════════════════════════
# Main Execution
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("Starting MLB DFS Projection Pipeline...")
    hitters, pitchers, stacks, weather = build_mlb_projections()
    
    # Export Color Key Legend Tab
    legend_headers = ["Tier", "Percentile Range", "Color Code", "Description / GPP Strategy"]
    legend_rows = [
        {"row": ["Elite", "Top 15%", "Gold (#FFD966)", "High-leverage core plays & top 5-man stack combinations"], "color": TIER_COLORS["Elite"]},
        {"row": ["Strong", "Next 20% (15%-35%)", "Green (#93C47D)", "Strong leverage and high-upside value targets"], "color": TIER_COLORS["Strong"]},
        {"row": ["Solid", "Next 30% (35%-65%)", "Blue (#9FC5E8)", "Safe baseline cash game plays and secondary correlation fillers"], "color": TIER_COLORS["Solid"]},
        {"row": ["Average", "Next 20% (65%-85%)", "White (#FFFFFF)", "Neutral slate plays and multi-positional salary balancing options"], "color": TIER_COLORS["Average"]},
        {"row": ["Fade", "Bottom 15% (85%-100%)", "Red (#EA9999)", "Low leverage, severe matchup risk, or harsh weather environments"], "color": TIER_COLORS["Fade"]},
    ]
    post_to_sheets("Color Key", legend_headers, legend_rows)
    
    # Export Hitters
    post_to_sheets("Hitters", ["Slate", "Name", "DK Pos", "Team", "Opp", "Order", "Opp SP", "DK Salary", "DK Proj", "DK Ceiling", "Leverage"], hitters)
    
    # Export Pitchers
    post_to_sheets("Pitchers", ["Slate", "Pitcher", "DK Pos", "Team", "Opp", "DK Salary", "DK Proj", "DK Ceiling", "Matchup Risk"], pitchers)
    
    # Export 5-Man Stacks
    post_to_sheets("Stacks", ["Slate", "Team", "Opponent", "Stack Pattern", "Hitters Included", "Total DK Salary", "Combined Proj", "Combined Ceiling", "Stack Rating", "Implied Runs"], stacks)
    
    # Export Weather
    post_to_sheets("Weather", ["Slate", "Matchup", "Venue", "Temp (°F)", "Wind (mph)", "HR Factor"], [{"row": r, "color": "#FFFFFF"} for r in weather])
    
    log.info("MLB DFS Pipeline Execution Complete.")
