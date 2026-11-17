#!/usr/bin/env python3
"""
Kalshi Sports Value Scanner
---------------------------
Compares Kalshi game-winner prices to sharp sportsbook odds (Pinnacle first,
then an average of other books) and sends a phone push notification (via the
free ntfy app) when a Kalshi contract is cheaper than its fair value, AFTER fees.

ALERT-ONLY: this script never places trades and never touches your Kalshi account.

Setup (one time):
  1. Get a free Odds API key:  https://the-odds-api.com  (free plan = 500 credits/month)
  2. Install the "ntfy" app on your phone and subscribe to a topic name
     (make one up that's hard to guess, e.g. "kalshi-alerts-7f3k9q").
  3. Put both in the CONFIG section below (or set env vars ODDS_API_KEY / NTFY_TOPIC).

Run:
  python3 kalshi_value_scanner.py --test-notify   # sends a test push to your phone
  python3 kalshi_value_scanner.py --once --debug  # one scan, prints everything it sees
  python3 kalshi_value_scanner.py                 # runs forever, scanning every few minutes

Needs only Python 3.8+ (no extra packages).
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ============================== CONFIG ======================================
CONFIG = {
    # --- Required ---
    "ODDS_API_KEY": os.environ.get("ODDS_API_KEY", "PASTE_YOUR_ODDS_API_KEY_HERE"),
    "NTFY_TOPIC": os.environ.get("NTFY_TOPIC", "PASTE_YOUR_NTFY_TOPIC_HERE"),
    # Discord alerts (optional, more reliable pop-ups): paste a channel webhook URL here
    "DISCORD_WEBHOOK": os.environ.get("DISCORD_WEBHOOK", "PASTE_DISCORD_WEBHOOK_URL_HERE"),

    # --- Which sports to scan: Odds API sport key -> Kalshi series ticker ---
    # Fewer sports = more frequent odds refreshes on the free plan.
    "SPORTS": {
        "basketball_nba": "KXNBAGAME",
        "icehockey_nhl": "KXNHLGAME",
        "americanfootball_nfl": "KXNFLGAME",
    },

    # --- Alert rules ---
    "MIN_EDGE": 0.03,            # alert when fair value beats (price + fee) by 3+ cents
    "MAX_EDGE": 0.10,            # edges bigger than 10c are almost always errors -> logged, not alerted
    "CONTRACTS_FOR_FEE": 10,     # fee is calculated as if you buy this many contracts
    "MIN_PRICE": 0.05,           # ignore contracts priced under 5c...
    "MAX_PRICE": 0.95,           # ...or over 95c
    "MIN_MINUTES_TO_START": 10,  # skip games starting very soon / already live
    "MAX_HOURS_TO_START": 36,    # only look at games in the next 36 hours
    "REALERT_EDGE_JUMP": 0.02,   # re-alert same contract only if edge grows 2+ cents...
    "REALERT_HOURS": 6,          # ...or after this many hours

    # --- Timing ---
    "KALSHI_POLL_MINUTES": 5,    # Kalshi prices are free to check, so check often
    "MIN_ODDS_REFRESH_MINUTES": 30,  # never refresh sportsbook odds faster than this

    # --- Books used for "fair value" (1 credit per sport per refresh for up to 10 books) ---
    "SHARP_BOOK": "pinnacle",
    "OTHER_BOOKS": ["draftkings", "fanduel", "betmgm", "williamhill_us", "betonlineag"],

    # Extra name aliases if a Kalshi team label doesn't match: {"kalshi label": "Full Team Name"}
    "ALIASES": {},
}
# ===========================================================================

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_state.json")
DEBUG = False


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def dbg(*a):
    if DEBUG:
        log("[debug]", *a)


# ----------------------------- HTTP helpers --------------------------------
def http_get_json(url, params=None, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-value-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), dict(r.headers)


def notify_discord(title, message, click_url=None):
    url = CONFIG.get("DISCORD_WEBHOOK", "")
    if not url or url.startswith("PASTE_"):
        return False
    text = f"**{title}**\n{message}" + (f"\n{click_url}" if click_url else "")
    req = urllib.request.Request(url, data=json.dumps({"content": text[:1990]}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "kalshi-value-scanner/1.0"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log("Discord notification failed:", e)
        return False


def notify(title, message, click_url=None, priority="high"):
    notify_discord(title, message, click_url)
    topic = CONFIG["NTFY_TOPIC"]
    if not topic or topic.startswith("PASTE_"):
        log("(no NTFY_TOPIC set, would have sent):", title, "|", message)
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": "moneybag"}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic), data=message.encode("utf-8"),
        headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log("Notification failed:", e)


# ----------------------------- Math ----------------------------------------
def kalshi_fee_per_contract(price, contracts):
    """Kalshi taker fee: round_up(0.07 * C * P * (1-P)) to the cent, spread over C contracts."""
    total = math.ceil(round(0.07 * contracts * price * (1 - price) * 100, 6)) / 100
    return total / contracts


def devig_two_way(odds_a, odds_b):
    """Decimal odds -> fair probabilities with the bookmaker's margin removed."""
    ia, ib = 1 / odds_a, 1 / odds_b
    s = ia + ib
    return ia / s, ib / s


def fair_probs_for_game(game):
    """Returns ({team_name: fair_prob}, source_label) or (None, None)."""
    per_book = {}
    for bm in game.get("bookmakers", []):
        for mk in bm.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            outs = mk.get("outcomes", [])
            if len(outs) != 2:  # skip anything with a draw option
                continue
            pa, pb = devig_two_way(outs[0]["price"], outs[1]["price"])
            per_book[bm["key"]] = {outs[0]["name"]: pa, outs[1]["name"]: pb}
    if not per_book:
        return None, None
    sharp = CONFIG["SHARP_BOOK"]
    if sharp in per_book:
        return per_book[sharp], sharp
    if len(per_book) < 3:  # without the sharp book, only trust a consensus of 3+ books
        return None, None
    teams = list(next(iter(per_book.values())).keys())
    avg = {t: sum(b.get(t, 0) for b in per_book.values()) / len(per_book) for t in teams}
    return avg, "avg of %d books" % len(per_book)


# ----------------------------- Name matching --------------------------------
EXPAND = {"la": ["los", "angeles"], "ny": ["new", "york"], "nyc": ["new", "york"]}


def norm(s):
    out = []
    for tok in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split():
        out += EXPAND.get(tok, [tok])
    return out


def label_matches_team(label, team):
    """Score how well a Kalshi label (e.g. 'Boston', 'Los Angeles L', 'Celtics') fits a full team name."""
    alias = CONFIG["ALIASES"].get(label) or CONFIG["ALIASES"].get(label.lower())
    if alias:
        return 1.0 if alias.lower() == team.lower() else 0.0
    lt, tt = norm(label), norm(team)
    if not lt:
        return 0.0
    if lt == tt:
        return 1.0
    matched = 0
    for i, tok in enumerate(lt):
        if len(tok) <= 2 and i == len(lt) - 1 and len(lt) > 1 and tok not in tt:
            if tt[-1].startswith(tok):
                matched += 1  # 'Los Angeles L' -> Lakers (initial must match the nickname)
            continue
        if tok in tt:
            matched += 1
        elif len(tok) >= 3 and any(t.startswith(tok) for t in tt):
            matched += 0.8
    if matched < len(lt) * 0.99:
        return 0.0  # every word in the label must fit the team
    return matched / len(lt)


def ticker_date(event_ticker):
    """KXNBAGAME-25MAY12BOSLAL -> date(2025,5,12)"""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2) + m.group(3), "%y%b%d").date()
    except ValueError:
        return None


def eastern_date(iso):
    """Game start time -> calendar date in US Eastern time (the date Kalshi puts in its tickers)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        return (dt - timedelta(hours=5)).date()


def market_label(m):
    return m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or ""


def match_event_to_game(event, games):
    """Returns (game, {market_ticker: team_name}) or (None, None)."""
    markets = [m for m in event.get("markets", []) if m.get("status") in (None, "active", "open")]
    if len(markets) != 2:
        return None, None
    edate = ticker_date(event.get("event_ticker", ""))
    if not edate:
        return None, None
    best = None
    for g in games:
        if eastern_date(g["commence_time"]) != edate:
            continue
        home, away = g["home_team"], g["away_team"]
        l0, l1 = market_label(markets[0]), market_label(markets[1])
        s_a = label_matches_team(l0, home) + label_matches_team(l1, away)
        s_b = label_matches_team(l0, away) + label_matches_team(l1, home)
        ok_a = label_matches_team(l0, home) > 0 and label_matches_team(l1, away) > 0
        ok_b = label_matches_team(l0, away) > 0 and label_matches_team(l1, home) > 0
        for ok, score, mapping in ((ok_a, s_a, (home, away)), (ok_b, s_b, (away, home))):
            if ok and (best is None or score > best[0]):
                best = (score, g, {markets[0]["ticker"]: mapping[0], markets[1]["ticker"]: mapping[1]})
    if best:
        return best[1], best[2]
    return None, None


# ----------------------------- Data fetching --------------------------------
def fetch_kalshi_events(series):
    events, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data, _ = http_get_json(KALSHI_BASE + "/events", params)
        events += data.get("events", [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


def price(m, field):
    """Read a Kalshi price as dollars, handling both new (*_dollars) and old (cents) fields."""
    v = m.get(field + "_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(field)
    if v in (None, ""):
        return None
    v = float(v)
    return v / 100 if v > 1 else v


def ask_size(m):
    v = m.get("yes_ask_size_fp")
    if v in (None, ""):
        return None
    return float(v)


class OddsCache:
    def __init__(self, saved=None):
        saved = saved or {}
        self.data = {k: tuple(v) for k, v in saved.get("odds", {}).items()}  # sport -> (fetched_at, games)
        self.remaining = saved.get("remaining")    # credits left this month

    def to_dict(self):
        return {"odds": {k: list(v) for k, v in self.data.items()}, "remaining": self.remaining}

    def refresh_interval_minutes(self, n_sports):
        """Spread the remaining monthly credits evenly over the rest of the month."""
        min_m = CONFIG["MIN_ODDS_REFRESH_MINUTES"]
        if self.remaining is None:
            return min_m
        now = datetime.now(timezone.utc)
        nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        minutes_left = (nxt - now).total_seconds() / 60
        refreshes_left = max(self.remaining // max(n_sports, 1), 1)
        return max(min_m, minutes_left / refreshes_left)

    def get(self, sport, n_active_sports):
        now = time.time()
        cached = self.data.get(sport)
        interval = self.refresh_interval_minutes(n_active_sports) * 60
        if cached and now - cached[0] < interval:
            return cached[1], (now - cached[0]) / 60
        if self.remaining is not None and self.remaining < 1:
            log("Out of Odds API credits for this month; using old odds if any.")
            return (cached[1], (now - cached[0]) / 60) if cached else ([], None)
        books = [CONFIG["SHARP_BOOK"]] + CONFIG["OTHER_BOOKS"]
        params = {"apiKey": CONFIG["ODDS_API_KEY"], "markets": "h2h", "oddsFormat": "decimal",
                  "bookmakers": ",".join(books[:10])}
        try:
            games, headers = http_get_json(f"{ODDS_BASE}/sports/{sport}/odds", params)
        except urllib.error.HTTPError as e:
            log(f"Odds API error for {sport}: {e.code} {e.read()[:200]!r}")
            return (cached[1], (now - cached[0]) / 60) if cached else ([], None)
        rem = headers.get("x-requests-remaining") or headers.get("X-Requests-Remaining")
        if rem is not None:
            self.remaining = int(float(rem))
        self.data[sport] = (now, games)
        log(f"Refreshed odds for {sport}: {len(games)} games, credits left: {self.remaining}")
        return games, 0.0


# ----------------------------- State (dedupe) --------------------------------
def load_state():
    """State file holds sent alerts (so you aren't pinged twice) and the saved sportsbook odds."""
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    if "alerts" not in st:  # older format: the whole file was the alerts dict
        st = {"alerts": {k: v for k, v in st.items() if isinstance(v, dict) and "t" in v}}
    return st


def save_state(state, odds_cache=None):
    cutoff = time.time() - 3 * 86400
    state["alerts"] = {k: v for k, v in state.get("alerts", {}).items() if v.get("t", 0) > cutoff}
    if odds_cache is not None:
        state["cache"] = odds_cache.to_dict()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def should_alert(state, ticker, edge):
    prev = state["alerts"].get(ticker)
    if not prev:
        return True
    if edge >= prev["edge"] + CONFIG["REALERT_EDGE_JUMP"]:
        return True
    return time.time() - prev["t"] > CONFIG["REALERT_HOURS"] * 3600


# ----------------------------- Main scan --------------------------------------
def scan(odds_cache, state):
    now = datetime.now(timezone.utc)
    found = []
    # 1) Which sports have open Kalshi games right now? (free to check)
    kalshi_by_sport = {}
    for sport, series in CONFIG["SPORTS"].items():
        try:
            evs = fetch_kalshi_events(series)
        except Exception as e:
            log(f"Kalshi fetch failed for {series}: {e}")
            continue
        today = eastern_date(datetime.now(timezone.utc).isoformat())
        soon = [e for e in evs if ticker_date(e.get("event_ticker", "")) and
                0 <= (ticker_date(e["event_ticker"]) - today).days <= 1]
        if soon:
            kalshi_by_sport[sport] = soon
        dbg(f"{series}: {len(evs)} open events")

    for sport, events in kalshi_by_sport.items():
        games, odds_age = odds_cache.get(sport, len(kalshi_by_sport))
        games = [g for g in games
                 if timedelta(minutes=CONFIG["MIN_MINUTES_TO_START"])
                 <= datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")) - now
                 <= timedelta(hours=CONFIG["MAX_HOURS_TO_START"])]
        if not games:
            continue
        for ev in events:
            game, mapping = match_event_to_game(ev, games)
            if not game:
                dbg("No sportsbook match for", ev.get("event_ticker"), "|", ev.get("title"),
                    "| labels:", [market_label(m) for m in ev.get("markets", [])])
                continue
            fair, src = fair_probs_for_game(game)
            if not fair:
                continue
            for m in ev["markets"]:
                team = mapping.get(m["ticker"])
                ask = price(m, "yes_ask")
                if team is None or ask is None or team not in fair:
                    continue
                size = ask_size(m)
                if not (CONFIG["MIN_PRICE"] <= ask <= CONFIG["MAX_PRICE"]) or (size is not None and size < 1):
                    continue
                fee = kalshi_fee_per_contract(ask, CONFIG["CONTRACTS_FOR_FEE"])
                edge = fair[team] - (ask + fee)
                dbg(f"{m['ticker']}: {team} ask {ask:.2f} + fee {fee:.3f} vs fair {fair[team]:.3f} ({src}) -> edge {edge:+.3f}")
                if edge > CONFIG["MAX_EDGE"]:
                    log(f"SKIPPED suspicious edge {edge*100:+.1f}c on {m['ticker']} ({team}) - probably bad data")
                    continue
                if edge >= CONFIG["MIN_EDGE"]:
                    found.append((edge, m, team, ask, fee, fair[team], src, game, odds_age, size))

    found.sort(key=lambda x: -x[0])
    for edge, m, team, ask, fee, fp, src, game, odds_age, size in found:
        if not should_alert(state, m["ticker"], edge):
            continue
        start = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00")).astimezone()
        roi = edge / (ask + fee) * 100
        title = f"Kalshi value: {team} YES @ {ask*100:.0f}c (+{edge*100:.1f}c edge)"
        msg = (f"{game['away_team']} @ {game['home_team']}, {start.strftime('%a %I:%M %p')}\n"
               f"Kalshi ask {ask*100:.0f}c + fee {fee*100:.1f}c = {(ask+fee)*100:.1f}c\n"
               f"Fair value {fp*100:.1f}% ({src}, odds {odds_age:.0f} min old)\n"
               f"Expected return ~{roi:.1f}% | {('%.0f' % size) if size else '?'} contracts available\n"
               f"Ticker: {m['ticker']}\n"
               f"Double-check the price before buying - it may have moved.")
        log("ALERT:", title)
        print(msg, flush=True)
        notify(title, msg, click_url="https://kalshi.com/markets/" + m["event_ticker"].split("-")[0].lower())
        state["alerts"][m["ticker"]] = {"edge": edge, "t": time.time()}
    save_state(state, odds_cache)
    log(f"Scan done: {len(found)} value spots ({sum(len(v) for v in kalshi_by_sport.values())} Kalshi games checked)")
    return found


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="Kalshi sports value scanner (alert-only)")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--debug", action="store_true", help="print every comparison")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()
    DEBUG = args.debug

    if args.test_notify:
        notify("Kalshi scanner test", "If you see this, phone alerts are working!")
        log("Test notification sent to topic:", CONFIG["NTFY_TOPIC"])
        return
    if not CONFIG["ODDS_API_KEY"] or CONFIG["ODDS_API_KEY"].startswith("PASTE_"):
        sys.exit("Set your ODDS_API_KEY in the CONFIG section first (free at https://the-odds-api.com).")

    state = load_state()
    odds_cache = OddsCache(state.get("cache"))
    log("Scanner started (alert-only, no trades). Ctrl+C to stop.")
    while True:
        try:
            scan(odds_cache, state)
        except Exception as e:
            log("Scan error:", repr(e))
        if args.once:
            break
        time.sleep(CONFIG["KALSHI_POLL_MINUTES"] * 60)


if __name__ == "__main__":
    main()
