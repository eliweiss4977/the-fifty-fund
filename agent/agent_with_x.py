"""
agent_with_x.py — Unified runner for The Fifty Fund
====================================================
Wires together:
  - algomind_agent  →  market data, Claude decisions, Alpaca execution
  - x_poster        →  X/Twitter auto-posting
  - substack_engine →  AI-authored Substack content

Entry point: python agent/agent_with_x.py

Scheduler (all times ET):
  - Every 30 minutes during NYSE hours → run_cycle()
  - 9:30am ET weekdays                 → morning market outlook
  - 4:05pm ET weekdays                 → EOD summary + daily Telegram summary
  - Every Friday 4:05pm                → weekly recap tweet + Substack review
  - 1st of each month                  → Substack monthly deep dive
"""

import logging
import time
from datetime import datetime, date

import pytz
from dotenv import load_dotenv

# ── Internal modules ──────────────────────────────────────────────────────────
import algomind_agent as agent
import x_poster       as xp
import substack_engine as sub

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ET_ZONE = pytz.timezone("America/New_York")

# ── State tracking (in-memory, reset on restart) ──────────────────────────────

_state = {
    "last_cycle_dt":            None,    # datetime of last trade cycle
    "morning_outlook_posted":   set(),   # set of dates (date objects)
    "eod_summary_posted":       set(),   # set of dates
    "weekly_recap_posted":      set(),   # set of dates (Friday dates)
    "monthly_deep_dive_posted": set(),   # set of (year, month) tuples
    "daily_summary_sent":       set(),   # set of dates
    "trades_this_week":         [],      # list of decision dicts since Monday
    "first_trade_done":         False,   # True after first ever execution
}

CYCLE_INTERVAL_SECONDS = 30 * 60   # 30 minutes


# ── Unified Run Cycle ─────────────────────────────────────────────────────────

def run_cycle() -> None:
    """
    Execute one complete trading + social cycle:
      1. Fetch market data & portfolio
      2. Ask Claude for a decision
      3. Execute the trade via Alpaca
      4. Post the decision to X
      5. Check for newly unlocked milestones
      6. Accumulate trade for weekly Substack review
    """
    logger.info("=== run_cycle() start ===")

    try:
        # ── Market data + portfolio ──────────────────────────────────────────
        market_data = agent.fetch_market_data(agent.TICKERS)
        if not market_data:
            logger.warning("No market data — skipping cycle.")
            return

        portfolio = agent.get_portfolio()

        # ── Claude decision + Alpaca execution ───────────────────────────────
        decision = agent.ask_claude(market_data, portfolio)
        result   = agent.execute_trade(decision)
        decision["result"]    = result
        decision["timestamp"] = datetime.now(ET_ZONE).isoformat()

        # ── Telegram notification ─────────────────────────────────────────────
        pv        = portfolio["portfolio_value"]
        pnl_pct   = (pv - agent.STARTING_CASH) / agent.STARTING_CASH * 100
        tg_msg    = (
            f"🤖 *AlgoMind* | {datetime.now(ET_ZONE).strftime('%H:%M ET')}\n"
            f"Action    : *{decision.get('action', '?')}* "
            f"{decision.get('ticker') or ''}\n"
            f"Result    : {result}\n"
            f"Reasoning : {decision.get('reasoning', '')}\n"
            f"Confidence: {decision.get('confidence', '?')}/10\n"
            f"Portfolio : ${pv:.2f} ({pnl_pct:+.1f}% vs start)"
        )
        agent.send_telegram(tg_msg)
        agent.send_email(
            f"[TheFiftyFund] {decision.get('action', '?')} {decision.get('ticker') or ''}",
            tg_msg,
        )

        # ── X post ───────────────────────────────────────────────────────────
        xp.post_trade_decision(decision)

        # ── First trade flag ─────────────────────────────────────────────────
        is_first_trade = False
        action = (decision.get("action") or "HOLD").upper()
        if action in ("BUY", "SELL") and not _state["first_trade_done"]:
            _state["first_trade_done"] = True
            is_first_trade = True

        # ── Milestone check (refresh portfolio post-trade) ───────────────────
        try:
            fresh_portfolio = agent.get_portfolio()
            xp.check_and_post_milestones(
                portfolio_value=fresh_portfolio["portfolio_value"],
                first_trade=is_first_trade,
            )
            # Also trigger Substack milestone posts for newly hit milestones
            # (milestone keys are returned but we only need to call sub once per key)
            newly_hit = xp.check_and_post_milestones(
                portfolio_value=fresh_portfolio["portfolio_value"],
                first_trade=False,   # already passed above if applicable
            )
            for key in newly_hit:
                sub.generate_milestone_post(key, fresh_portfolio)
        except Exception as exc:
            logger.warning("Milestone check failed: %s", exc)

        # ── Accumulate weekly trades ──────────────────────────────────────────
        if action in ("BUY", "SELL"):
            _state["trades_this_week"].append(decision)

        logger.info("run_cycle() complete: %s", result)

    except Exception as exc:
        logger.error("run_cycle() error: %s", exc, exc_info=True)
        agent.send_telegram(f"⚠️ AlgoMind run_cycle error:\n{exc}")


# ── Scheduled Event Handlers ──────────────────────────────────────────────────

def _handle_morning_outlook(market_data: dict) -> None:
    """Post morning market outlook once per trading day at open."""
    today = date.today()
    if today in _state["morning_outlook_posted"]:
        return
    xp.post_morning_outlook(market_data)
    _state["morning_outlook_posted"].add(today)
    logger.info("Morning outlook posted for %s.", today)


def _handle_eod(portfolio: dict) -> None:
    """Post EOD tweet, Telegram daily summary, and (Fridays) weekly recap + Substack."""
    today  = date.today()
    now_et = datetime.now(ET_ZONE)

    # EOD tweet
    if today not in _state["eod_summary_posted"]:
        xp.post_eod_summary(portfolio)
        _state["eod_summary_posted"].add(today)

    # Telegram daily summary
    if today not in _state["daily_summary_sent"]:
        agent.send_daily_summary()
        _state["daily_summary_sent"].add(today)

    # Friday: weekly recap tweet + Substack review
    if now_et.weekday() == 4:   # Friday
        if today not in _state["weekly_recap_posted"]:
            xp.post_weekly_recap(portfolio)
            sub.generate_weekly_review(portfolio, _state["trades_this_week"])
            _state["weekly_recap_posted"].add(today)
            _state["trades_this_week"] = []   # reset for next week
            logger.info("Weekly recap + Substack review posted.")


def _handle_monthly_deep_dive(portfolio: dict) -> None:
    """Generate Substack monthly deep dive on the 1st of each month."""
    now_et = datetime.now(ET_ZONE)
    key    = (now_et.year, now_et.month)
    if now_et.day == 1 and key not in _state["monthly_deep_dive_posted"]:
        sub.generate_monthly_deep_dive(portfolio)
        _state["monthly_deep_dive_posted"].add(key)
        logger.info("Monthly deep dive posted: %s/%s", *key)


# ── Main Scheduler Loop ───────────────────────────────────────────────────────

def start() -> None:
    """
    Start the unified scheduler loop.  Polls every 60 seconds and dispatches
    events based on ET market time.
    """
    logger.info("The Fifty Fund — AlgoMind with X + Substack started.")
    agent.send_telegram(
        "🤖 *The Fifty Fund* is online.\n"
        "AlgoMind is scanning the market. First cycle begins at next 30-min mark."
    )

    while True:
        now_et = datetime.now(ET_ZONE)
        today  = now_et.date()
        is_weekday = now_et.weekday() < 5

        # ── Market data snapshot (cheap; used by multiple handlers) ──────────
        market_data = {}
        portfolio   = {}
        try:
            if is_weekday:
                market_data = agent.fetch_market_data(agent.TICKERS)
                portfolio   = agent.get_portfolio()
        except Exception as exc:
            logger.warning("Could not fetch market snapshot: %s", exc)

        # ── Morning outlook at 9:30am ET on weekdays ─────────────────────────
        market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        in_market_hours = is_weekday and market_open <= now_et < market_close

        if is_weekday and now_et >= market_open and today not in _state["morning_outlook_posted"]:
            if market_data:
                _handle_morning_outlook(market_data)

        # ── Trade cycle every 30 minutes during market hours ─────────────────
        if in_market_hours:
            elapsed = (
                (now_et - _state["last_cycle_dt"]).total_seconds()
                if _state["last_cycle_dt"] is not None
                else CYCLE_INTERVAL_SECONDS   # force run on first entry
            )
            if elapsed >= CYCLE_INTERVAL_SECONDS:
                run_cycle()
                _state["last_cycle_dt"] = now_et

        # ── EOD events at 4:05pm ET on weekdays ──────────────────────────────
        eod_threshold = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
        if is_weekday and now_et >= eod_threshold and portfolio:
            _handle_eod(portfolio)

        # ── Monthly deep dive on the 1st ──────────────────────────────────────
        if is_weekday and portfolio:
            _handle_monthly_deep_dive(portfolio)

        time.sleep(60)   # poll every minute


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start()
