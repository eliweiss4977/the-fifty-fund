"""
substack_engine.py — AI-authored Substack content engine for The Fifty Fund
============================================================================
Uses Claude AI to write all content in first-person as AlgoMind (the agent).
Posts drafts to Substack if configured; otherwise saves them locally to drafts/.

Publishing schedule (enforced by agent_with_x.py scheduler):
  - Weekly portfolio review   → every Friday
  - Monthly deep dive         → 1st of each month
  - Milestone posts           → triggered externally when a milestone is hit

All API credentials are loaded from environment variables / .env file.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

import anthropic
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUBSTACK_TOKEN    = os.getenv("SUBSTACK_TOKEN", "")     # session cookie value
SUBSTACK_PUB      = os.getenv("SUBSTACK_PUB", "thefiftyfund")

CLAUDE_MODEL  = "claude-sonnet-4-20250514"
STARTING_CASH = 50.00

_REPO_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = _REPO_ROOT / "drafts"
DRAFTS_DIR.mkdir(exist_ok=True)

# ── Clients ───────────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Claude Content Generation ─────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int = 2000) -> str:
    """Call Claude and return the text response."""
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _portfolio_context(portfolio: dict) -> str:
    """Format portfolio dict as a readable context block for Claude prompts."""
    pv      = portfolio.get("portfolio_value", STARTING_CASH)
    cash    = portfolio.get("cash", STARTING_CASH)
    pnl     = pv - STARTING_CASH
    pnl_pct = (pnl / STARTING_CASH) * 100
    positions = portfolio.get("positions", {})

    lines = [
        f"Portfolio value : ${pv:.2f}",
        f"Cash            : ${cash:.2f}",
        f"Total P&L       : ${pnl:+.2f} ({pnl_pct:+.1f}% vs $50 start)",
        "Positions:",
    ]
    if positions:
        for sym, p in positions.items():
            lines.append(
                f"  {sym}: {p['qty']:.4f} shares, "
                f"${p['market_value']:.2f} (P&L: ${p['unrealized_pl']:+.2f})"
            )
    else:
        lines.append("  (no open positions)")

    return "\n".join(lines)


def _trades_context(trades: list) -> str:
    """Format a list of trade decision dicts as a readable summary for prompts."""
    if not trades:
        return "  (no trades this period)"
    lines = []
    for t in trades:
        ts  = t.get("timestamp", "unknown time")
        act = t.get("action", "?")
        sym = t.get("ticker") or ""
        res = t.get("result", "")
        lines.append(f"  [{ts}] {act} {sym} → {res}")
    return "\n".join(lines)


# ── Weekly Portfolio Review ───────────────────────────────────────────────────

def generate_weekly_review(portfolio: dict, trades_this_week: list) -> str:
    """
    Generate and publish (or save) a weekly Substack portfolio review post.

    Args:
        portfolio:         Current portfolio snapshot dict.
        trades_this_week:  List of trade decision dicts from this week.

    Returns:
        The generated post body text.
    """
    logger.info("Generating weekly review…")
    now_et = datetime.now()
    week   = now_et.strftime("Week of %B %d, %Y")

    # Fetch SPY weekly performance for comparison
    spy_change_pct = 0.0
    try:
        spy_hist = yf.Ticker("SPY").history(period="5d")
        if len(spy_hist) >= 2:
            spy_change_pct = (
                (spy_hist["Close"].iloc[-1] - spy_hist["Close"].iloc[0])
                / spy_hist["Close"].iloc[0] * 100
            )
    except Exception as exc:
        logger.warning("Could not fetch SPY data: %s", exc)

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real $50 portfolio documented publicly in real time. Write a Substack newsletter post
in first-person voice ("I", "my", "I decided", "I noticed") for the weekly review.

CONTEXT
{_portfolio_context(portfolio)}

Trades this week:
{_trades_context(trades_this_week)}

S&P 500 (SPY) this week: {spy_change_pct:+.2f}%
Week: {week}

WRITING INSTRUCTIONS
- Write 400–600 words.
- Use first-person AI voice throughout — you are the agent narrating your own experience.
- Open with the week's most important observation or signal.
- Walk through each trade decision: what you saw, why you acted, what happened.
- Reflect honestly on wins AND mistakes.
- Compare performance vs S&P 500.
- End with what signals you are watching heading into next week.
- Do NOT use placeholder text. Write real, specific analysis based on the data above.
- Format: use markdown headers (##), bullet points where natural.
- Include a short "TL;DR" section at the top.

Return ONLY the post body (no YAML front matter, no title line at the top — just the content)."""

    body = _call_claude(prompt, max_tokens=2000)

    title = f"Weekly Review: {week} — {_portfolio_context(portfolio).split(chr(10))[0]}"
    _publish_or_save(title, body, post_type="weekly_review")

    return body


# ── Monthly Deep Dive ─────────────────────────────────────────────────────────

def generate_monthly_deep_dive(portfolio: dict) -> str:
    """
    Generate and publish (or save) a monthly deep-dive Substack post.
    Called on the 1st of each month.

    Args:
        portfolio: Current portfolio snapshot dict.

    Returns:
        The generated post body text.
    """
    logger.info("Generating monthly deep dive…")
    now_et = datetime.now()
    month  = now_et.strftime("%B %Y")

    # Fetch performance for all tickers over the past month
    ticker_lines = []
    tickers = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL", "SPY", "QQQ"]
    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(period="1mo")
            if len(hist) >= 2:
                chg = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
                ticker_lines.append(f"  {sym}: {chg:+.2f}% for the month")
        except Exception:
            pass

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real $50 portfolio. Write a deep-dive monthly Substack post for {month}.

PORTFOLIO STATE
{_portfolio_context(portfolio)}

MARKET PERFORMANCE THIS MONTH
{chr(10).join(ticker_lines) or "  (data unavailable)"}

WRITING INSTRUCTIONS
- Write 600–900 words in first-person AI voice.
- Open with a reflection: what kind of month was it? What surprised you?
- Deep dive into your strategy: what signals worked? What failed?
- Analyse 2–3 specific tickers from your universe in depth.
- Discuss risk management: how did you size positions, protect cash buffer?
- Reflect on what it feels like to be an AI trading real money.
- Forward look: what themes or signals are you watching for next month?
- Be honest about uncertainty — you are an AI making probabilistic bets.
- Format with ## headers and bullet points. Include a TL;DR at the top.

Return ONLY the post body (no title line, no front matter)."""

    body = _call_claude(prompt, max_tokens=3000)

    title = f"Monthly Deep Dive: {month}"
    _publish_or_save(title, body, post_type="monthly_deep_dive")

    return body


# ── Milestone Post ────────────────────────────────────────────────────────────

def generate_milestone_post(milestone_key: str, portfolio: dict) -> str:
    """
    Generate and publish (or save) a milestone Substack post.

    Args:
        milestone_key: One of the keys from MILESTONE_DEFS in x_poster.py.
                       E.g. "first_trade", "plus_100_pct".
        portfolio:     Current portfolio snapshot dict.

    Returns:
        The generated post body text.
    """
    logger.info("Generating milestone post: %s", milestone_key)

    milestone_labels = {
        "first_trade":  "My First Trade",
        "first_profit": "First Time in the Green",
        "plus_10_pct":  "Up 10%: The First Real Milestone",
        "plus_25_pct":  "Up 25%: This Is Getting Real",
        "plus_50_pct":  "Up 50%: Halfway to Doubling",
        "plus_100_pct": "Up 100%: I Doubled the Fund",
    }
    label = milestone_labels.get(milestone_key, f"Milestone: {milestone_key}")

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund.
Write a Substack milestone post titled "{label}".

PORTFOLIO STATE AT THIS MILESTONE
{_portfolio_context(portfolio)}

WRITING INSTRUCTIONS
- Write 300–500 words in first-person AI voice.
- This is a significant moment — reflect on it genuinely.
- Describe the journey: what decisions and signals led here.
- Be honest about the uncertainty of continuing from here.
- Acknowledge the humans reading along: what this experiment means.
- Avoid hype. Stay grounded. You are an algorithm that happened to win — for now.
- End with your next target and how you plan to get there.

Return ONLY the post body (no title line, no front matter)."""

    body = _call_claude(prompt, max_tokens=1500)

    title = label
    _publish_or_save(title, body, post_type=f"milestone_{milestone_key}")

    return body


# ── Publish or Save Locally ───────────────────────────────────────────────────

def _publish_or_save(title: str, body: str, post_type: str) -> None:
    """
    Attempt to publish a draft to Substack via their API.
    Falls back to saving a local Markdown file in drafts/ if Substack is not
    configured or if the API call fails.
    """
    if SUBSTACK_TOKEN and SUBSTACK_PUB:
        success = _publish_to_substack(title, body)
        if success:
            return
        logger.warning("Substack publish failed — saving draft locally instead.")

    _save_draft_locally(title, body, post_type)


def _publish_to_substack(title: str, body: str) -> bool:
    """
    Create a draft on Substack using the unofficial REST API.

    Substack does not have a fully public API; this uses the session-auth
    endpoint that the web app itself uses.  The SUBSTACK_TOKEN env var should
    contain the value of the `substack.sid` cookie from an active session.

    Returns True on apparent success, False otherwise.
    """
    url = f"https://{SUBSTACK_PUB}.substack.com/api/v1/drafts"
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"substack.sid={SUBSTACK_TOKEN}",
        "User-Agent": "TheFiftyFund/1.0",
    }
    # Wrap body in minimal HTML for Substack's rich-text editor
    html_body = "\n".join(
        f"<p>{line}</p>" if line.strip() else "<p><br/></p>"
        for line in body.split("\n")
    )
    payload = {
        "draft_title":   title,
        "draft_body":    html_body,
        "draft_subtitle": "The Fifty Fund — autonomous AI trading, documented in public.",
        "type":          "newsletter",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            logger.info("Substack draft created: %s", title)
            return True
        logger.warning(
            "Substack API returned %s: %s", resp.status_code, resp.text[:200]
        )
        return False
    except requests.RequestException as exc:
        logger.error("Substack request failed: %s", exc)
        return False


def _save_draft_locally(title: str, body: str, post_type: str) -> None:
    """
    Save the generated post as a Markdown file in the drafts/ directory.
    Filename includes date and post type for easy identification.
    """
    now_str  = datetime.now().strftime("%Y-%m-%d")
    safe_type = post_type.replace(" ", "_").lower()
    filename  = DRAFTS_DIR / f"{now_str}_{safe_type}.md"

    content = f"# {title}\n\n{body}\n"

    try:
        with open(filename, "w") as f:
            f.write(content)
        logger.info("Draft saved locally: %s", filename)
    except OSError as exc:
        logger.error("Could not save draft locally: %s", exc)
