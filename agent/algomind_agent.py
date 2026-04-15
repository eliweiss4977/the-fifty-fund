"""
algomind_agent.py — Core trading agent for The Fifty Fund
==========================================================
Connects to Alpaca for real trade execution, uses Claude AI for every
decision, fetches live market data via yfinance, and notifies via Telegram.

Schedule (when run standalone):
  - Trade cycle every 30 minutes during NYSE hours (9:30am–4:00pm ET, Mon–Fri)
  - Daily summary at 4:05pm ET on weekdays

All API credentials are loaded from environment variables / .env file.
"""

import json
import logging
import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import alpaca_trade_api as tradeapi
import numpy as np
import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

EMAIL_FROM     = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO       = os.getenv("EMAIL_TO", "")

TICKERS = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL", "SPY", "QQQ"]

CLAUDE_MODEL    = "claude-sonnet-4-20250514"
STARTING_CASH   = 50.00          # reference for P&L display
CASH_BUFFER     = 2.00           # always keep $2 in cash
MAX_POSITION_PCT = 0.30          # cap any single position at 30% of portfolio

ET_ZONE = pytz.timezone("America/New_York")

# ── API Clients ───────────────────────────────────────────────────────────────

alpaca = tradeapi.REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2",
)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Market Data ───────────────────────────────────────────────────────────────

def fetch_market_data(tickers: list) -> dict:
    """
    Download 30 days of daily OHLCV data for each ticker via yfinance and
    compute RSI-14, 1-day price-change %, and today's volume.

    Returns:
        {
          ticker: {
            "price":      float,
            "change_pct": float,   # % change from prior close
            "volume":     int,
            "rsi":        float,   # 14-period RSI
          },
          ...
        }
    """
    data = {}
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="30d")
            if hist.empty or len(hist) < 2:
                logger.warning("Insufficient history for %s — skipping.", ticker)
                continue

            closes = hist["Close"].values.astype(float)
            price      = closes[-1]
            change_pct = (price - closes[-2]) / closes[-2] * 100
            volume     = int(hist["Volume"].values[-1])
            rsi        = _calc_rsi(closes, period=14)

            data[ticker] = {
                "price":      round(price, 4),
                "change_pct": round(change_pct, 3),
                "volume":     volume,
                "rsi":        round(rsi, 2),
            }
        except Exception as exc:
            logger.error("Error fetching data for %s: %s", ticker, exc)

    return data


def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    """
    Compute RSI-14 for the most recent bar using Wilder's averaging.
    Returns 50.0 if there is not enough data.
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Portfolio Snapshot ────────────────────────────────────────────────────────

def get_portfolio() -> dict:
    """
    Fetch the current Alpaca account and open positions.

    Returns:
        {
          "cash":            float,
          "portfolio_value": float,
          "positions": {
            ticker: {
              "qty":           float,
              "market_value":  float,
              "unrealized_pl": float,
            },
            ...
          },
        }
    """
    account   = alpaca.get_account()
    positions = alpaca.list_positions()

    pos_dict = {
        p.symbol: {
            "qty":           float(p.qty),
            "market_value":  float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    }

    return {
        "cash":            float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "positions":       pos_dict,
    }


# ── Claude Decision Engine ────────────────────────────────────────────────────

def ask_claude(market_data: dict, portfolio: dict) -> dict:
    """
    Send a market snapshot + portfolio to Claude and get back a structured
    JSON trade decision.

    Expected response JSON schema:
        {
          "action":         "BUY" | "SELL" | "HOLD",
          "ticker":         str | null,
          "dollar_amount":  float | null,   # dollar amount for BUY orders
          "qty":            float | null,   # shares to sell for SELL orders
          "reasoning":      str,            # Claude's explanation
          "confidence":     int,            # 1–10
          "market_summary": str,            # one-sentence macro view
        }

    Raises ValueError if Claude's response is not parseable JSON.
    """
    market_lines = [
        f"  {sym}: price=${d['price']:.2f}, "
        f"change={d['change_pct']:+.2f}%, "
        f"volume={d['volume']:,}, "
        f"RSI={d['rsi']:.1f}"
        for sym, d in market_data.items()
    ]

    position_lines = [
        f"  {sym}: {p['qty']:.4f} shares @ ${p['market_value']:.2f} "
        f"(P&L: ${p['unrealized_pl']:+.2f})"
        for sym, p in portfolio["positions"].items()
    ] or ["  (no open positions)"]

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real portfolio that started with exactly $50. Your mandate is long-term growth.
You can buy fractional shares, so any dollar amount can be deployed.

CURRENT PORTFOLIO
  Cash available : ${portfolio['cash']:.2f}
  Total value    : ${portfolio['portfolio_value']:.2f}
  Open positions :
{chr(10).join(position_lines)}

MARKET SNAPSHOT  (30-day data, RSI-14, 1-day change)
{chr(10).join(market_lines)}

DECISION RULES
- Use RSI as a primary signal: RSI < 30 = oversold/possible buy, RSI > 70 = overbought/possible sell.
- Combine with price momentum (change_pct) and volume for conviction.
- Never allocate more than {int(MAX_POSITION_PCT * 100)}% of total portfolio value to a single ticker.
- Always maintain at least ${CASH_BUFFER:.2f} cash buffer.
- If no strong signal exists, HOLD is the correct answer.
- Prefer quality signals over frequent trading.

Respond ONLY with valid JSON — no markdown fences, no extra text outside the JSON object.

REQUIRED RESPONSE FORMAT
{{
  "action": "BUY" | "SELL" | "HOLD",
  "ticker": "<SYMBOL>" or null,
  "dollar_amount": <float> or null,
  "qty": <float> or null,
  "reasoning": "<detailed explanation of the signal and why this action>",
  "confidence": <integer 1–10>,
  "market_summary": "<one sentence on overall market conditions today>"
}}"""

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.debug("Claude raw response: %s", raw)

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned non-JSON response: {raw[:200]}") from exc

    return decision


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(decision: dict) -> str:
    """
    Submit the trade to Alpaca based on Claude's decision.

    Uses notional (dollar-based) orders for BUY to support fractional shares.
    Returns a human-readable result string for logging and notifications.
    """
    action  = (decision.get("action") or "HOLD").upper()
    ticker  = decision.get("ticker")
    reason  = decision.get("reasoning", "")

    if action == "HOLD" or not ticker:
        return f"HOLD — {reason}"

    try:
        if action == "BUY":
            dollar_amount = float(decision.get("dollar_amount") or 0)
            if dollar_amount <= 0:
                return f"BUY skipped — dollar_amount was {dollar_amount}"
            alpaca.submit_order(
                symbol=ticker,
                notional=round(dollar_amount, 2),
                side="buy",
                type="market",
                time_in_force="day",
            )
            return f"BUY ${dollar_amount:.2f} of {ticker} — {reason}"

        elif action == "SELL":
            qty = float(decision.get("qty") or 0)
            if qty <= 0:
                return f"SELL skipped — qty was {qty}"
            alpaca.submit_order(
                symbol=ticker,
                qty=qty,
                side="sell",
                type="market",
                time_in_force="day",
            )
            return f"SELL {qty} shares of {ticker} — {reason}"

    except Exception as exc:
        logger.error("Trade execution error: %s", exc)
        return f"ERROR: {action} {ticker} failed — {exc}"

    return "HOLD — unrecognised action"


# ── Notifications ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    """Send a message to the configured Telegram chat. Silently skips if not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def send_email(subject: str, body: str) -> None:
    """
    Send an email via Gmail SMTP. Silently skips if EMAIL_FROM / EMAIL_PASSWORD
    / EMAIL_TO are not all set in the environment.
    """
    if not (EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO):
        logger.debug("Email not configured — skipping.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logger.info("Email sent: %s", subject)
    except Exception as exc:
        logger.warning("Email send failed: %s", exc)


# ── Core Trade Cycle ──────────────────────────────────────────────────────────

def run_trade_cycle() -> dict | None:
    """
    Execute one full trade cycle:
      1. Fetch live market data
      2. Snapshot the portfolio
      3. Ask Claude for a decision
      4. Execute the trade via Alpaca
      5. Notify via Telegram (+ optional email)

    Returns the decision dict with a "result" key added, or None on fatal error.
    """
    now_et = datetime.now(ET_ZONE)
    logger.info("=== Trade cycle starting at %s ET ===", now_et.strftime("%H:%M"))

    try:
        market_data = fetch_market_data(TICKERS)
        if not market_data:
            logger.warning("No market data retrieved — aborting cycle.")
            return None

        portfolio = get_portfolio()
        decision  = ask_claude(market_data, portfolio)
        result    = execute_trade(decision)

        decision["result"]    = result
        decision["timestamp"] = now_et.isoformat()

        pv        = portfolio["portfolio_value"]
        pnl       = pv - STARTING_CASH
        pnl_pct   = (pnl / STARTING_CASH) * 100

        tg_msg = (
            f"🤖 *AlgoMind* | {now_et.strftime('%b %d, %H:%M ET')}\n"
            f"Action     : *{decision.get('action', '?')}* "
            f"{decision.get('ticker') or ''}\n"
            f"Result     : {result}\n"
            f"Reasoning  : {decision.get('reasoning', '')}\n"
            f"Confidence : {decision.get('confidence', '?')}/10\n"
            f"Portfolio  : ${pv:.2f} ({pnl_pct:+.1f}% vs start)"
        )
        send_telegram(tg_msg)
        send_email(
            f"[TheFiftyFund] {decision.get('action', '?')} {decision.get('ticker') or ''}",
            tg_msg,
        )

        logger.info("Trade cycle complete: %s", result)
        return decision

    except Exception as exc:
        logger.error("Trade cycle failed: %s", exc, exc_info=True)
        send_telegram(f"⚠️ AlgoMind error during trade cycle:\n{exc}")
        return None


def send_daily_summary() -> None:
    """
    Build and send a portfolio summary. Called at 4:05pm ET on weekdays.
    """
    logger.info("Sending daily summary…")
    try:
        portfolio = get_portfolio()
        positions = portfolio["positions"]
        pv        = portfolio["portfolio_value"]
        pnl       = pv - STARTING_CASH
        pnl_pct   = (pnl / STARTING_CASH) * 100

        lines = [
            "📊 *Daily Summary — The Fifty Fund*",
            f"Date           : {datetime.now(ET_ZONE).strftime('%A, %B %d %Y')}",
            f"Portfolio value: ${pv:.2f}",
            f"Cash available : ${portfolio['cash']:.2f}",
            f"Total P&L      : ${pnl:+.2f} ({pnl_pct:+.1f}% vs start)",
            "",
            "Open Positions:",
        ]
        if positions:
            for sym, p in positions.items():
                lines.append(
                    f"  {sym}: {p['qty']:.4f} sh, "
                    f"${p['market_value']:.2f} (P&L: ${p['unrealized_pl']:+.2f})"
                )
        else:
            lines.append("  (no open positions)")

        summary = "\n".join(lines)
        send_telegram(summary)
        send_email("[TheFiftyFund] Daily Summary", summary)
    except Exception as exc:
        logger.error("Daily summary failed: %s", exc)
        send_telegram(f"⚠️ Daily summary error: {exc}")


# ── Market Hours Helper ───────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """
    Return True if the current ET time is within NYSE trading hours:
    Monday–Friday, 9:30am–4:00pm ET.
    """
    now = datetime.now(ET_ZONE)
    if now.weekday() >= 5:   # Saturday = 5, Sunday = 6
        return False
    open_time  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now < close_time


# ── Standalone Scheduler ──────────────────────────────────────────────────────

def start_scheduler() -> None:
    """
    Run a polling loop that:
    - Executes a trade cycle every 30 minutes during NYSE market hours.
    - Sends a daily summary at (or after) 4:05pm ET on weekdays.

    Note: agent_with_x.py has its own scheduler that calls run_trade_cycle()
    directly. Use this only when running algomind_agent.py standalone.
    """
    last_cycle_dt      = None
    daily_summary_sent = set()   # tracks dates (datetime.date) already summarised
    cycle_interval_s   = 30 * 60  # 30 minutes

    logger.info("AlgoMind scheduler started (standalone mode).")

    while True:
        now_et = datetime.now(ET_ZONE)
        today  = now_et.date()

        # ── Trade cycle ──────────────────────────────────────────────────────
        if is_market_hours():
            elapsed = (
                (now_et - last_cycle_dt).total_seconds()
                if last_cycle_dt is not None
                else cycle_interval_s  # force run on first entry
            )
            if elapsed >= cycle_interval_s:
                run_trade_cycle()
                last_cycle_dt = now_et

        # ── Daily summary (4:05pm ET on weekdays) ────────────────────────────
        summary_threshold = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
        if (
            now_et >= summary_threshold
            and now_et.weekday() < 5
            and today not in daily_summary_sent
        ):
            send_daily_summary()
            daily_summary_sent.add(today)

        time.sleep(60)   # check every minute


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
