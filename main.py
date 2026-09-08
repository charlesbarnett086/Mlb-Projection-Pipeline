#!/usr/bin/env python3
"""
MLB DFS Projection Pipeline (DraftKings & 5-Man Stack Optimized)
─────────────────────────────────────────────────────────────────
Features:
  - DraftKings Positions: P, C, 1B, 2B, 3B, SS, OF
  - 5-Man Stack Combinations: Generates optimal 5-batter team stacks (1-5, 1-4+9, 2-6)
  - Data Sources: MLB Stats API, Open-Meteo, The Odds API
  - Outputs: Hitters, Pitchers, Stacks (5-Man), Weather
"""

import os
import sys
import logging
from datetime import datetime
from itertools import combinations
import requests

# ── Logging ────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Endpoints & Secrets ────
MLB_API_BASE     = "https://statsapi.mlb.com/api/v1"
OPEN_METEO_URL   = "https://api.open-meteo.com/v1/forecast"
ODDS_API_URL     = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"

ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
GOOGLE_WEBAPP_URL = os.environ.get("GOOGLE_WEBAPP_URL", "")

# Standard stadium coordinates (Lat, Lon)
STADIUM_COORDS = {
    "Coors Field": (39.756, -104.994),
    "Yankee Stadium": (40.829, -73.926),
    "Fenway Park": (42.346, -71.097),
    "Wrigley Field": (41.948, -87.655),
    "Dodger Stadium": (34.073, -118.240),
    "Default": (39.828, -98.579)
}

# DraftKings Position Allocation Template for 1-9 Orders
DK_POSITION_MAP = {
    1: "SS", 2: "OF", 3: "1B", 4: "3B", 
    5: "OF", 6: "2B", 7: "C",  8: "OF", 9: "OF"
}

# Tier Color Scheme for Google Sheets
TIER_COLORS = {
    "Elite":   "#FFD966",  # Gold - Must-play core / Top 5-Man Stack
    "Strong":  "#93C47D",  # Green - Strong leverage / Value
    "Solid":   "#9FC5E8",  # Blue - Safe cash baseline
    "Average": "#FFFFFF",  # White - Neutral
    "Fade":    "#EA9999",  # Red - Bad weather / Low leverage
}
HEADER_COLOR = "#1F4E79"

# ═════════════════════════════════════════════════════════════════════════════
# 1. Weather Engine (Open-Meteo)
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
# 2. Vegas Lines & Totals
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
# 3. MLB Slate Data
# ═════════════════════════════════════════════════════════════════════════════

def fetch_mlb_slate_data():
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"{MLB_API_BASE}/schedule?sportId=1&date={today}&hydrate=probablePitcher,lineups,venue"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        schedule = resp.json()
        games = []
        dates = schedule.get("dates", [])
        if not dates:
            return []
            
        for game in dates[0].get("games", []):
            games.append({
                "home": game["teams"]["home"]["team"]["name"],
                "away": game["teams"]["away"]["team"]["name"],
                "home_sp": game["teams"]["home"].get("probablePitcher", {}).get("fullName", "TBD"),
                "away_sp": game["teams"]["away"].get("probablePitcher", {}).get("fullName", "TBD"),
                "venue": game.get("venue", {}).get("name", "Default"),
            })
        return games
    except Exception as e:
        log.error(f"Failed to load MLB schedule: {e}")
        return []

# ═════════════════════════════════════════════════════════════════════════════
# 4. 5-Man Stack Generator & Projection Engine
# ═════════════════════════════════════════════════════════════════════════════

def generate_5man_stacks(hitters_list: list, team: str, opp: str, implied_runs: float, hr_factor: float) -> list:
    """Calculates top 5-man DraftKings stacking combinations for a team."""
    # Top correlation patterns: 1-2-3-4-5, 1-2-3-4-9 (wrap-around), 2-3-4-5-6
    combos = [
        (1, 2, 3, 4, 5),
        (1, 2, 3, 4, 9),
        (2, 3, 4, 5, 6),
        (1, 3, 4, 5, 6)
    ]
    
    hitter_map = {h["order_num"]: h for h in hitters_list}
    stacks = []
    
    for combo in combos:
        if all(o in hitter_map for o in combo):
            selected = [hitter_map[o] for o in combo]
            total_salary = sum(h["salary"] for h in selected)
            total_proj = sum(h["proj"] for h in selected)
            total_ceiling = sum(h["ceiling"] for h in selected)
            
            # Stack Rating factoring DK scoring correlation
            rating = round((total_proj * 0.4) + (total_ceiling * 0.3) + (implied_runs * 1.5) + (hr_factor * 4), 2)
            combo_str = "-".join(str(o) for o in combo)
            names_str = ", ".join([h["name"].split()[-1] for h in selected])
            
            tier = "Elite" if rating > 38.0 else ("Strong" if rating > 32.0 else "Solid")
            
            stacks.append({
                "row": [
                    team, opp, f"Combo ({combo_str})", names_str, 
                    total_salary, round(total_proj, 2), round(total_ceiling, 2), rating, implied_runs
                ],
                "color": TIER_COLORS[tier],
                "rating": rating
            })
            
    return sorted(stacks, key=lambda x: x["rating"], reverse=True)

def build_mlb_projections():
    games = fetch_mlb_slate_data()
    vegas = fetch_vegas_totals()
    
    hitters, pitchers, all_stacks, weather_rows = [], [], [], []
    
    for g in games:
        wx = fetch_game_weather(g["venue"])
        
        weather_rows.append([
            f"{g['away']} @ {g['home']}", g['venue'], 
            wx['temp'], wx['wind'], f"{wx['hr_factor']}x HR Factor"
        ])
        
        for team, opp, opp_sp in [(g['home'], g['away'], g['away_sp']), (g['away'], g['home'], g['home_sp'])]:
            implied_runs = vegas.get(team, 4.5)
            team_hitters = []
            
            for order in range(1, 10):
                name = f"{team} Hitter {order}"
                pos = DK_POSITION_MAP[order]
                
                # DK scoring baseline projection
                base_proj = (9.5 - (order * 0.55)) * (implied_runs / 4.5) * wx['hr_factor']
                ceiling = round(base_proj * 1.9, 2)
                
                # DraftKings Salary logic ($2,000 to $6,300)
                salary = max(2000, min(6300, int(base_proj * 480)))
                leverage = round((ceiling / (salary / 1000)), 2)
                
                tier = "Elite" if leverage > 3.2 else ("Strong" if leverage > 2.5 else "Solid")
                
                hitter_data = {
                    "name": name, "order_num": order, "salary": salary, 
                    "proj": base_proj, "ceiling": ceiling
                }
                team_hitters.append(hitter_data)
                
                hitters.append({
                    "row": [name, pos, team, opp, f"Order {order}", opp_sp, salary, round(base_proj, 2), ceiling, leverage],
                    "color": TIER_COLORS[tier]
                })
            
            # Generate 5-Man Stacks
            team_stacks = generate_5man_stacks(team_hitters, team, opp, implied_runs, wx['hr_factor'])
            all_stacks.extend(team_stacks)
            
            # DraftKings Pitcher Projection (2 SP slots)
            sp_name = opp_sp
            if sp_name != "TBD":
                sp_proj = round(13.5 + (5.0 - implied_runs) * 2.2, 2)
                sp_ceiling = round(sp_proj * 1.6, 2)
                sp_salary = max(5500, min(10800, int(sp_proj * 510)))
                sp_tier = "Elite" if sp_proj > 19.0 else ("Strong" if sp_proj > 14.0 else "Solid")
                
                pitchers.append({
                    "row": [sp_name, "P", opp, team, sp_salary, sp_proj, sp_ceiling, f"{implied_runs} Implied Runs"],
                    "color": TIER_COLORS[sp_tier]
                })

    return hitters, pitchers, all_stacks, weather_rows

# ═════════════════════════════════════════════════════════════════════════════
# 5. Google Sheets Exporter
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
    log.info("Starting DraftKings MLB DFS Pipeline...")
    hitters, pitchers, stacks, weather = build_mlb_projections()
    
    # Export Hitters
    post_to_sheets("Hitters", ["Name", "DK Pos", "Team", "Opp", "Order", "Opp SP", "DK Salary", "DK Proj", "DK Ceiling", "Leverage"], hitters)
    
    # Export Pitchers
    post_to_sheets("Pitchers", ["Pitcher", "DK Pos", "Team", "Opp", "DK Salary", "DK Proj", "DK Ceiling", "Matchup Risk"], pitchers)
    
    # Export 5-Man Stacks
    post_to_sheets("Stacks", ["Team", "Opponent", "Stack Pattern", "Hitters Included", "Total DK Salary", "Combined Proj", "Combined Ceiling", "Stack Rating", "Implied Runs"], stacks)
    
    # Export Weather
    post_to_sheets("Weather", ["Matchup", "Venue", "Temp (°F)", "Wind (mph)", "HR Factor"], [{"row": r, "color": "#FFFFFF"} for r in weather])
    
    log.info("DraftKings MLB Pipeline Execution Complete.")
