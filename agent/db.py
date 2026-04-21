"""
agent/db.py — Postgres helpers for The Fifty Fund
Writes trades, AI log, and performance to shared Arena DB.
"""
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

logger = logging.getLogger(__name__)

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pool.SimpleConnectionPool(1, 5, dsn=os.environ["DATABASE_URL"])
        logger.info("FF DB pool initialized.")
    return _pool

def insert_trade(cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post=None):
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ff_trades (cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post))
            conn.commit()
            p.putconn(conn)
    except Exception as e:
        logger.error("insert_trade failed: %s", e)

def insert_ai_log(message, tags):
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ff_ai_log (message, tags) VALUES (%s, %s)", (message, tags))
            conn.commit()
            p.putconn(conn)
    except Exception as e:
        logger.error("insert_ai_log failed: %s", e)

def upsert_performance(date_str, portfolio_value, return_pct):
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ff_performance (date, portfolio_value, return_pct)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (date) DO UPDATE SET portfolio_value = EXCLUDED.portfolio_value, return_pct = EXCLUDED.return_pct
                """, (date_str, portfolio_value, return_pct))
            conn.commit()
            p.putconn(conn)
    except Exception as e:
        logger.error("upsert_performance failed: %s", e)

def get_trades(limit=50):
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_trades ORDER BY created_at DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_trades failed: %s", e)
        return []

def get_ai_log(limit=100):
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_ai_log ORDER BY created_at DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_ai_log failed: %s", e)
        return []

def get_performance():
    try:
        p = get_pool()
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_performance ORDER BY date ASC")
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_performance failed: %s", e)
        return []
