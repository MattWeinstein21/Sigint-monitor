"""
SIGINT Keyword Monitor - Backend
Monitors Trump social media, major news outlets, and government sources
for user-defined keywords. Sends timestamped alerts via a local API.

Setup:
    pip install feedparser requests flask flask-cors beautifulsoup4 tweepy

Usage:
    1. Configure your API keys in config.json (see generate_config())
    2. python monitor.py
    3. Open the frontend at localhost:5000
"""

import json
import time
import hashlib
import logging
import threading
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
import calendar

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sigint")


# ─── Data Store (SQLite) ─────────────────────────────────────────────────────
# Foundation for article storage, trend analysis, and AI learning.
# Schema designed to enable:
# - Debugging: see what was fetched, how it was scored, where AI disagreed
# - Trend analysis: track keyword/sector frequency over time
# - AI learning: feed historical context into Ollama for better responses
# - Feedback loop: user corrections improve future analysis

DATA_STORE_PATH = Path("sigint_data.db")


def init_data_store():
    """Initialize the SQLite database with required tables."""
    conn = sqlite3.connect(str(DATA_STORE_PATH))
    c = conn.cursor()

    # Core article storage
    c.execute("""CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        published_at TEXT,
        source_id TEXT,
        source_name TEXT,
        title TEXT,
        url TEXT UNIQUE,
        keyword TEXT,
        article_text TEXT,
        summary TEXT,
        sentiment TEXT,
        sentiment_score REAL,
        ai_sentiment TEXT,
        word_sentiment TEXT,
        confidence REAL,
        severity TEXT,
        positive_signals TEXT,
        negative_signals TEXT,
        article_fetched INTEGER DEFAULT 0,
        ai_summary INTEGER DEFAULT 0,
        -- Extended fields for AI learning
        sectors TEXT DEFAULT '',
        entities TEXT DEFAULT '',
        user_corrected_sentiment TEXT DEFAULT '',
        ai_analysis_raw TEXT DEFAULT '',
        fetch_duration_ms INTEGER DEFAULT 0
    )""")

    # Chat conversation log
    c.execute("""CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        alert_id INTEGER,
        article_title TEXT,
        article_url TEXT DEFAULT '',
        question TEXT,
        answer TEXT,
        source_id TEXT DEFAULT '',
        keyword TEXT DEFAULT ''
    )""")

    # Sentiment correction feedback — user tells us the AI got it wrong
    c.execute("""CREATE TABLE IF NOT EXISTS sentiment_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        article_url TEXT,
        article_title TEXT,
        original_sentiment TEXT,
        corrected_sentiment TEXT,
        original_impact TEXT DEFAULT '',
        corrected_impact TEXT DEFAULT '',
        user_context TEXT DEFAULT '',
        source_id TEXT,
        keyword TEXT
    )""")

    # System event log for debugging
    c.execute("""CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event_type TEXT,
        message TEXT,
        details TEXT DEFAULT ''
    )""")

    # Indexes for fast queries
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_timestamp ON articles(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_sentiment ON articles(sentiment)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_keyword ON articles(keyword)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_sectors ON articles(sectors)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_alert ON chat_logs(alert_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_corrections_url ON sentiment_corrections(article_url)")

    # Add columns if they don't exist (for upgrades from older schema)
    for col, coltype in [("sectors", "TEXT DEFAULT ''"), ("entities", "TEXT DEFAULT ''"),
                          ("user_corrected_sentiment", "TEXT DEFAULT ''"),
                          ("ai_analysis_raw", "TEXT DEFAULT ''"),
                          ("fetch_duration_ms", "INTEGER DEFAULT 0"),
                          ("market_impact", "TEXT DEFAULT ''"),
                          ("word_market_impact", "TEXT DEFAULT ''"),
                          ("ai_market_impact", "TEXT DEFAULT ''"),
                          ("published_at_utc", "TEXT DEFAULT ''"),
                          ("user_corrected_impact", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE articles ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # Column already exists
    for col, coltype in [("article_url", "TEXT DEFAULT ''"), ("source_id", "TEXT DEFAULT ''"), ("keyword", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE chat_logs ADD COLUMN {col} {coltype}")
        except Exception:
            pass
    for col, coltype in [("original_impact", "TEXT DEFAULT ''"), ("corrected_impact", "TEXT DEFAULT ''"), ("user_context", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE sentiment_corrections ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    conn.commit()
    conn.close()
    log.info(f"Data store initialized: {DATA_STORE_PATH}")


# ─── Entity & Sector Extraction ──────────────────────────────────────────────

SECTOR_KEYWORDS = {
    "tech": ["apple", "google", "microsoft", "nvidia", "meta", "amazon", "ai ", "artificial intelligence",
             "semiconductor", "chip", "software", "cloud", "data center", "saas", "cybersecurity"],
    "finance": ["bank", "fed ", "federal reserve", "interest rate", "treasury", "wall street", "s&p",
                "nasdaq", "dow jones", "goldman", "jpmorgan", "morgan stanley", "credit", "loan"],
    "energy": ["oil", "opec", "natural gas", "lng", "pipeline", "drilling", "renewable", "solar",
               "wind energy", "nuclear", "exxon", "chevron", "bp ", "shell"],
    "crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "stablecoin",
               "coinbase", "binance", "altcoin", "mining"],
    "healthcare": ["fda", "pharma", "biotech", "drug", "vaccine", "clinical trial", "hospital",
                   "healthcare", "pfizer", "moderna", "johnson & johnson", "medical"],
    "defense": ["military", "defense", "pentagon", "lockheed", "raytheon", "boeing", "nato",
                "troops", "missile", "nuclear weapon"],
    "real_estate": ["housing", "mortgage", "real estate", "reit", "property", "rent", "foreclosure"],
    "consumer": ["retail", "walmart", "target", "consumer spending", "inflation", "cpi"],
    "trade": ["tariff", "trade war", "import", "export", "sanctions", "embargo", "customs"],
}

ENTITY_PATTERNS = [
    # Companies (detect common tickers and names)
    (r"\b(AAPL|Apple Inc)\b", "Apple"),
    (r"\b(GOOGL?|Alphabet|Google)\b", "Google"),
    (r"\b(MSFT|Microsoft)\b", "Microsoft"),
    (r"\b(NVDA|Nvidia|NVIDIA)\b", "Nvidia"),
    (r"\b(META|Meta Platforms|Facebook)\b", "Meta"),
    (r"\b(AMZN|Amazon)\b", "Amazon"),
    (r"\b(TSLA|Tesla)\b", "Tesla"),
    (r"\b(JPM|JPMorgan|JP Morgan)\b", "JPMorgan"),
    (r"\b(GS|Goldman Sachs)\b", "Goldman Sachs"),
    # People
    (r"\bTrump\b", "Donald Trump"),
    (r"\bBiden\b", "Joe Biden"),
    (r"\bPowell\b", "Jerome Powell"),
    (r"\bYellen\b", "Janet Yellen"),
    (r"\bMusk\b", "Elon Musk"),
    (r"\bBezos\b", "Jeff Bezos"),
    # Countries
    (r"\bChina\b", "China"), (r"\bRussia\b", "Russia"), (r"\bIran\b", "Iran"),
    (r"\bUkraine\b", "Ukraine"), (r"\bNorth Korea\b", "North Korea"),
    (r"\bEuropean Union\b|\bEU\b", "EU"),
]


def extract_sectors(text):
    """Identify which sectors an article relates to."""
    text_lower = text.lower()
    matched = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.append(sector)
                break
    return list(set(matched))


def extract_entities(text):
    """Extract known entities (companies, people, countries) from text."""
    found = set()
    for pattern, entity in ENTITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            found.add(entity)
    return list(found)


# ─── Ticker Linking ──────────────────────────────────────────────────────────

TICKER_EVENT_MAP = {
    "tariff": ["SPY", "QQQ", "DIA", "AAPL", "TSLA", "AMZN"],
    "trade war": ["SPY", "QQQ", "DIA", "AAPL", "TSLA", "AMZN"],
    "sanctions": ["SPY", "XOM", "CVX", "GLD"],
    "fed": ["SPY", "TLT", "GLD", "DIA"], "federal reserve": ["SPY", "TLT", "GLD"],
    "interest rate": ["SPY", "TLT", "GLD", "XLF"], "rate hike": ["SPY", "TLT", "XLF"],
    "inflation": ["SPY", "TLT", "GLD", "TIP"], "cpi": ["SPY", "TLT", "GLD"],
    "oil": ["XOM", "CVX", "USO", "XLE"], "opec": ["XOM", "CVX", "USO", "XLE"],
    "natural gas": ["XLE", "LNG"], "energy": ["XLE", "XOM", "CVX"],
    "semiconductor": ["NVDA", "AMD", "SMH", "INTC", "TSM"], "chips": ["NVDA", "AMD", "SMH"],
    "bitcoin": ["BTC", "COIN", "MARA", "MSTR"], "ethereum": ["ETH", "COIN"],
    "crypto": ["BTC", "ETH", "COIN", "MARA"], "stablecoin": ["BTC", "COIN"],
    "fda": ["XLV", "XBI"], "pharma": ["XLV", "XBI"], "biotech": ["XBI"],
    "bank": ["XLF", "JPM", "GS", "BAC"], "housing": ["XHB", "ITB"],
    "treasury": ["TLT", "SHY", "IEF"], "debt ceiling": ["SPY", "TLT"],
    "executive order": ["SPY", "QQQ"], "regulation": ["SPY", "QQQ"],
    "ipo": ["SPY"], "earnings": ["SPY", "QQQ"],
    "apple": ["AAPL"], "nvidia": ["NVDA"], "tesla": ["TSLA"], "google": ["GOOGL"],
    "microsoft": ["MSFT"], "amazon": ["AMZN"], "meta": ["META"],
    "jpmorgan": ["JPM"], "goldman": ["GS"], "boeing": ["BA"],
}

# Common entity-to-ticker mapping
ENTITY_TICKER_MAP = {
    "Apple": "AAPL", "Google": "GOOGL", "Microsoft": "MSFT", "Nvidia": "NVDA",
    "Meta": "META", "Amazon": "AMZN", "Tesla": "TSLA", "JPMorgan": "JPM",
    "Goldman Sachs": "GS", "Donald Trump": None, "Joe Biden": None,
    "Jerome Powell": None, "Elon Musk": "TSLA", "Jeff Bezos": "AMZN",
    "China": None, "Russia": None, "Iran": None, "EU": None,
}


def link_tickers_to_alert(title, text, keyword, entities):
    """Link an alert to potentially affected tickers. Deterministic mapping."""
    affected = set()
    combined_text = f"{title} {text}".lower()

    # Keyword-based mapping
    for kw, tickers in TICKER_EVENT_MAP.items():
        if kw in keyword.lower() or kw in combined_text[:500]:
            for t in tickers:
                affected.add(t)

    # Entity-based mapping
    for entity in entities:
        ticker = ENTITY_TICKER_MAP.get(entity)
        if ticker:
            affected.add(ticker)

    # Filter to watchlist if it exists
    try:
        wl_path = Path("watchlist.json")
        if wl_path.exists():
            wl = json.loads(wl_path.read_text())
            wl_symbols = set()
            for item in wl:
                if isinstance(item, dict):
                    wl_symbols.add(item.get("symbol", "").upper())
                elif isinstance(item, str):
                    wl_symbols.add(item.upper())
            if wl_symbols:
                # Return intersection with watchlist, plus always include broad indices
                broad = {"SPY", "QQQ", "DIA", "BTC", "ETH"}
                watchlist_matches = affected & wl_symbols
                broad_matches = affected & broad
                return list(watchlist_matches | broad_matches)[:8]
    except Exception:
        pass

    return list(affected)[:8]


# ─── Data Logging Functions ───────────────────────────────────────────────────

def log_article(alert_data, article_text=""):
    """Log an article and its analysis to the data store.
    Uses INSERT OR IGNORE + UPDATE to avoid silently dropping columns."""
    try:
        full_text = article_text or alert_data.get("title", "")
        sectors = extract_sectors(full_text)
        entities = extract_entities(full_text)
        url = alert_data.get("url", "")
        if not url:
            return

        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        # Try INSERT first (only fires if url is new)
        c.execute("""INSERT OR IGNORE INTO articles
            (timestamp, url, title, source_id, source_name, keyword)
            VALUES (?,?,?,?,?,?)""",
            (alert_data.get("timestamp", ""), url,
             alert_data.get("title", ""),
             alert_data.get("source", ""),
             alert_data.get("source_name", ""),
             alert_data.get("keyword", "")))

        # Always UPDATE — safe for both new and existing rows
        pub_utc = alert_data.get("published_at_utc", "")
        c.execute("""UPDATE articles SET
            timestamp = COALESCE(NULLIF(?, ''), timestamp),
            published_at = COALESCE(NULLIF(?, ''), published_at),
            published_at_utc = COALESCE(NULLIF(?, ''), published_at_utc),
            source_id = COALESCE(NULLIF(?, ''), source_id),
            source_name = COALESCE(NULLIF(?, ''), source_name),
            title = COALESCE(NULLIF(?, ''), title),
            keyword = COALESCE(NULLIF(?, ''), keyword),
            article_text = CASE WHEN LENGTH(?) > LENGTH(COALESCE(article_text, '')) THEN ? ELSE article_text END,
            summary = COALESCE(NULLIF(?, ''), summary),
            sentiment = COALESCE(NULLIF(?, ''), sentiment),
            sentiment_score = CASE WHEN ? != 0 THEN ? ELSE sentiment_score END,
            ai_sentiment = COALESCE(NULLIF(?, ''), ai_sentiment),
            word_sentiment = COALESCE(NULLIF(?, ''), word_sentiment),
            market_impact = COALESCE(NULLIF(?, ''), market_impact),
            word_market_impact = COALESCE(NULLIF(?, ''), word_market_impact),
            ai_market_impact = COALESCE(NULLIF(?, ''), ai_market_impact),
            confidence = CASE WHEN ? > 0 THEN ? ELSE confidence END,
            severity = COALESCE(NULLIF(?, ''), severity),
            positive_signals = COALESCE(NULLIF(?, '[]'), positive_signals),
            negative_signals = COALESCE(NULLIF(?, '[]'), negative_signals),
            article_fetched = MAX(COALESCE(article_fetched, 0), ?),
            ai_summary = MAX(COALESCE(ai_summary, 0), ?),
            sectors = COALESCE(NULLIF(?, '[]'), sectors),
            entities = COALESCE(NULLIF(?, '[]'), entities),
            ai_analysis_raw = COALESCE(NULLIF(?, ''), ai_analysis_raw),
            fetch_duration_ms = CASE WHEN ? > 0 THEN ? ELSE fetch_duration_ms END
            WHERE url = ?""",
            (alert_data.get("timestamp", ""),
             alert_data.get("published_at", ""),
             pub_utc,
             alert_data.get("source", ""),
             alert_data.get("source_name", ""),
             alert_data.get("title", ""),
             alert_data.get("keyword", ""),
             article_text[:8000] if article_text else "",
             article_text[:8000] if article_text else "",
             alert_data.get("summary", ""),
             alert_data.get("sentiment", ""),
             alert_data.get("sentiment_score", 0),
             alert_data.get("sentiment_score", 0),
             alert_data.get("ai_sentiment", "") or "",
             alert_data.get("word_sentiment", "") or "",
             alert_data.get("market_impact", "") or "",
             alert_data.get("word_market_impact", "") or "",
             alert_data.get("ai_market_impact", "") or "",
             alert_data.get("confidence", 0),
             alert_data.get("confidence", 0),
             alert_data.get("severity", ""),
             json.dumps(alert_data.get("positive_signals", [])),
             json.dumps(alert_data.get("negative_signals", [])),
             1 if alert_data.get("article_fetched") else 0,
             1 if alert_data.get("ai_summary") else 0,
             json.dumps(sectors),
             json.dumps(entities),
             alert_data.get("ai_analysis_raw", "") or "",
             alert_data.get("fetch_duration_ms", 0),
             alert_data.get("fetch_duration_ms", 0),
             url))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Data store log error: {e}")


def log_chat(alert_id, article_title, question, answer, article_url="", source_id="", keyword=""):
    """Log a chat interaction to the data store."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("""INSERT INTO chat_logs
            (timestamp, alert_id, article_title, article_url, question, answer, source_id, keyword)
            VALUES (?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), alert_id, article_title,
             article_url, question, answer, source_id, keyword))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Chat log error: {e}")


def log_system_event(event_type, message, details=""):
    """Log a system event for debugging."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("INSERT INTO system_logs (timestamp, event_type, message, details) VALUES (?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), event_type, message, details))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"System log error: {e}")


def log_sentiment_correction(url, title, original, corrected, source_id="", keyword="",
                              original_impact="", corrected_impact="", user_context=""):
    """Log a user sentiment correction with optional impact correction and context."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("""INSERT INTO sentiment_corrections
            (timestamp, article_url, article_title, original_sentiment, corrected_sentiment,
             original_impact, corrected_impact, user_context, source_id, keyword)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), url, title, original, corrected,
             original_impact, corrected_impact, user_context, source_id, keyword))
        c.execute("UPDATE articles SET user_corrected_sentiment = ? WHERE url = ?", (corrected, url))
        # Persist corrected impact so trend context uses it
        if corrected_impact and corrected_impact != "duplicate":
            try:
                c.execute("UPDATE articles SET user_corrected_impact = ? WHERE url = ?",
                          (corrected_impact, url))
            except Exception:
                pass  # Column may not exist in old DBs
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Correction log error: {e}")


def get_related_articles(keyword="", sector="", limit=5):
    """
    Fetch recent related articles from the data store.
    Includes user-corrected labels when available for trend context.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        cols = "title, summary, sentiment, market_impact, published_at, source_name, user_corrected_sentiment"
        # Safely add user_corrected_impact if column exists
        try:
            c.execute("SELECT user_corrected_impact FROM articles LIMIT 1")
            cols += ", user_corrected_impact"
        except Exception:
            pass  # Column doesn't exist in old DBs
        if keyword:
            c.execute(f"SELECT {cols} FROM articles WHERE keyword = ? ORDER BY id DESC LIMIT ?",
                      (keyword.lower(), limit))
        elif sector:
            c.execute(f"SELECT {cols} FROM articles WHERE sectors LIKE ? ORDER BY id DESC LIMIT ?",
                      (f"%{sector}%", limit))
        else:
            c.execute(f"SELECT {cols} FROM articles ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def build_trend_context(keyword="", sector="", limit=10):
    """
    Build a text summary of recent trends for a keyword/sector.
    Prefers user-corrected impact and tone when available.
    """
    articles = get_related_articles(keyword=keyword, sector=sector, limit=limit)
    if not articles:
        return ""
    lines = [f"Recent coverage on '{keyword or sector}':"]
    for a in articles:
        # Prefer corrected labels for both impact and tone
        corrected_tone = a.get("user_corrected_sentiment", "")
        corrected_impact = a.get("user_corrected_impact", "")
        tone = corrected_tone if corrected_tone else a.get("sentiment", "neutral")
        impact = corrected_impact if corrected_impact else a.get("market_impact", "neutral")
        corrected_tag = " (user-corrected)" if (corrected_tone or corrected_impact) else ""
        lines.append(f"- [impact:{impact} tone:{tone}{corrected_tag}] {a.get('title', '')} ({a.get('source_name', '')}, {a.get('published_at', '')})")
    return "\n".join(lines)


# Initialize on import
init_data_store()
log_system_event("startup", "SIGINT monitor initialized")


# ─── Self-Improvement / Historical Learning ───────────────────────────────────

def get_correction_stats(source_id="", keyword=""):
    """
    Query historical corrections to determine AI vs lexicon accuracy for both tone and impact.
    Uses AND when both filters provided, falls back to global stats if sample too small.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        # Build query — AND when both provided, not OR
        where_parts = []
        params = []
        if source_id:
            where_parts.append("sc.source_id = ?")
            params.append(source_id)
        if keyword:
            where_parts.append("sc.keyword = ?")
            params.append(keyword)

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        # Tone accuracy (backward compat)
        c.execute(f"""SELECT
            SUM(CASE WHEN sc.corrected_sentiment = a.ai_sentiment THEN 1 ELSE 0 END) as ai_right,
            SUM(CASE WHEN sc.corrected_sentiment = a.word_sentiment THEN 1 ELSE 0 END) as word_right,
            COUNT(*) as total
            FROM sentiment_corrections sc
            LEFT JOIN articles a ON sc.article_url = a.url
            {where_clause}""", params)
        row = c.fetchone()

        # Impact accuracy (new)
        c.execute(f"""SELECT
            SUM(CASE WHEN sc.corrected_impact = a.ai_market_impact THEN 1 ELSE 0 END) as ai_impact_right,
            SUM(CASE WHEN sc.corrected_impact = a.word_market_impact THEN 1 ELSE 0 END) as word_impact_right,
            SUM(CASE WHEN sc.corrected_impact != '' THEN 1 ELSE 0 END) as impact_total
            FROM sentiment_corrections sc
            LEFT JOIN articles a ON sc.article_url = a.url
            {where_clause}""", params)
        impact_row = c.fetchone()

        conn.close()

        result = {"llm_accuracy": 0.7, "lexicon_accuracy": 0.5, "sample_size": 0,
                  "llm_impact_accuracy": 0.7, "lexicon_impact_accuracy": 0.5, "impact_sample_size": 0}

        if row and row[2] and row[2] > 5:
            total = row[2]
            result["llm_accuracy"] = (row[0] or 0) / total
            result["lexicon_accuracy"] = (row[1] or 0) / total
            result["sample_size"] = total
        elif where_parts:
            # Fallback to global stats if filtered sample is too small
            c2 = sqlite3.connect(str(DATA_STORE_PATH)).cursor()
            c2.execute("""SELECT
                SUM(CASE WHEN sc.corrected_sentiment = a.ai_sentiment THEN 1 ELSE 0 END),
                SUM(CASE WHEN sc.corrected_sentiment = a.word_sentiment THEN 1 ELSE 0 END),
                COUNT(*)
                FROM sentiment_corrections sc LEFT JOIN articles a ON sc.article_url = a.url""")
            grow = c2.fetchone()
            c2.connection.close()
            if grow and grow[2] and grow[2] > 5:
                result["llm_accuracy"] = (grow[0] or 0) / grow[2]
                result["lexicon_accuracy"] = (grow[1] or 0) / grow[2]
                result["sample_size"] = grow[2]

        if impact_row and impact_row[2] and impact_row[2] > 3:
            itotal = impact_row[2]
            result["llm_impact_accuracy"] = (impact_row[0] or 0) / itotal
            result["lexicon_impact_accuracy"] = (impact_row[1] or 0) / itotal
            result["impact_sample_size"] = itotal

        return result
    except Exception as e:
        log.debug(f"Correction stats error: {e}")

    return {"llm_accuracy": 0.7, "lexicon_accuracy": 0.5, "sample_size": 0,
            "llm_impact_accuracy": 0.7, "lexicon_impact_accuracy": 0.5, "impact_sample_size": 0}


def recalibrate_thresholds():
    """
    Analyze user corrections to find signal words that frequently misfire.
    Returns list of adjustments to consider applying to signal sets.
    Run periodically (daily/weekly) or on-demand.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        c.execute("""
            SELECT a.positive_signals, a.negative_signals,
                   sc.original_sentiment, sc.corrected_sentiment
            FROM sentiment_corrections sc
            JOIN articles a ON sc.article_url = a.url
        """)

        bad_positive = {}  # Positive words that were in wrongly-positive articles
        bad_negative = {}  # Negative words that were in wrongly-negative articles
        total_corrections = 0

        for row in c.fetchall():
            total_corrections += 1
            try:
                pos_sigs = json.loads(row[0] or "[]")
                neg_sigs = json.loads(row[1] or "[]")
            except json.JSONDecodeError:
                continue
            original = row[2]
            corrected = row[3]

            if original == "positive" and corrected in ("negative", "neutral"):
                for word in pos_sigs:
                    bad_positive[word] = bad_positive.get(word, 0) + 1
            elif original == "negative" and corrected in ("positive", "neutral"):
                for word in neg_sigs:
                    bad_negative[word] = bad_negative.get(word, 0) + 1

        conn.close()

        adjustments = []
        for word, count in bad_positive.items():
            if count >= 3:
                adjustments.append({
                    "word": word, "current_type": "positive",
                    "action": "reduce_weight", "misfire_count": count,
                    "recommendation": f'Word "{word}" triggered positive {count} times but articles were corrected to negative/neutral'
                })
        for word, count in bad_negative.items():
            if count >= 3:
                adjustments.append({
                    "word": word, "current_type": "negative",
                    "action": "reduce_weight", "misfire_count": count,
                    "recommendation": f'Word "{word}" triggered negative {count} times but articles were corrected to positive/neutral'
                })

        return {"adjustments": adjustments, "total_corrections_analyzed": total_corrections}

    except Exception as e:
        log.debug(f"Recalibration error: {e}")
        return {"adjustments": [], "total_corrections_analyzed": 0}


def compute_source_accuracy():
    """
    Track which sources tend to produce misclassified articles.
    Sources with high correction rates may need confidence discounting.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        c.execute("""
            SELECT a.source_id, a.source_name,
                   COUNT(*) as total_corrected
            FROM sentiment_corrections sc
            JOIN articles a ON sc.article_url = a.url
            GROUP BY a.source_id
            ORDER BY total_corrected DESC
        """)
        correction_rows = c.fetchall()

        c.execute("SELECT source_id, COUNT(*) FROM articles GROUP BY source_id")
        total_by_source = dict(c.fetchall())

        conn.close()

        source_accuracy = {}
        for source_id, source_name, corrected in correction_rows:
            total = total_by_source.get(source_id, 0)
            if total > 0:
                error_rate = corrected / total
                source_accuracy[source_id] = {
                    "name": source_name,
                    "error_rate": round(error_rate, 3),
                    "corrections": corrected,
                    "total_articles": total,
                    "accuracy": round(1.0 - error_rate, 3),
                }

        return source_accuracy

    except Exception as e:
        log.debug(f"Source accuracy error: {e}")
        return {}


def calibrate_confidence():
    """
    Check: when the system says confidence=0.8, is it actually correct 80% of the time?
    Returns calibration data for each confidence bucket.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        buckets = [(0, 0.3, "low"), (0.3, 0.5, "medium-low"), (0.5, 0.7, "medium"),
                   (0.7, 0.85, "medium-high"), (0.85, 1.01, "high")]
        results = []

        for low, high, label in buckets:
            c.execute("SELECT COUNT(*) FROM articles WHERE confidence >= ? AND confidence < ?", (low, high))
            total = c.fetchone()[0]

            c.execute("""SELECT COUNT(*) FROM articles a
                JOIN sentiment_corrections sc ON a.url = sc.article_url
                WHERE a.confidence >= ? AND a.confidence < ?""", (low, high))
            corrected = c.fetchone()[0]

            if total > 0:
                actual_accuracy = 1.0 - (corrected / total)
                claimed_midpoint = (low + high) / 2
                results.append({
                    "bucket": label,
                    "range": f"{low:.0%}-{high:.0%}",
                    "total_articles": total,
                    "corrections": corrected,
                    "actual_accuracy": round(actual_accuracy, 3),
                    "claimed_confidence": round(claimed_midpoint, 2),
                    "calibration_ratio": round(actual_accuracy / max(claimed_midpoint, 0.01), 2),
                    "overconfident": actual_accuracy < claimed_midpoint,
                })

        conn.close()
        return results

    except Exception as e:
        log.debug(f"Calibration error: {e}")
        return []


def get_disagreement_outcomes():
    """
    When AI and word-based disagreed, who was right?
    Tracks both tone and impact disagreements from corrections data.
    """
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()

        # ─── Tone disagreements ───
        c.execute("""
            SELECT
                a.ai_sentiment, a.word_sentiment, sc.corrected_sentiment,
                a.source_id, a.keyword
            FROM sentiment_corrections sc
            JOIN articles a ON sc.article_url = a.url
            WHERE a.ai_sentiment != '' AND a.ai_sentiment IS NOT NULL
              AND a.word_sentiment != '' AND a.word_sentiment IS NOT NULL
              AND a.ai_sentiment != a.word_sentiment
        """)

        ai_wins = 0
        words_wins = 0
        neither_wins = 0
        total = 0
        examples = []

        for row in c.fetchall():
            total += 1
            ai_sent, word_sent, corrected, source, keyword = row
            if corrected == ai_sent:
                ai_wins += 1
            elif corrected == word_sent:
                words_wins += 1
            else:
                neither_wins += 1
            if len(examples) < 10:
                examples.append({
                    "ai_said": ai_sent, "words_said": word_sent,
                    "correct_was": corrected, "source": source, "keyword": keyword
                })

        tone_result = {
            "total_disagreements_corrected": total,
            "ai_was_right": ai_wins,
            "words_were_right": words_wins,
            "neither_right": neither_wins,
            "ai_accuracy_on_disagreements": round(ai_wins / max(total, 1), 3),
            "word_accuracy_on_disagreements": round(words_wins / max(total, 1), 3),
            "examples": examples,
        }

        # ─── Impact disagreements ───
        impact_result = {"total": 0, "ai_was_right": 0, "words_were_right": 0,
                         "neither_right": 0, "examples": []}
        try:
            c.execute("""
                SELECT
                    a.ai_market_impact, a.word_market_impact, sc.corrected_impact,
                    a.source_id, a.keyword
                FROM sentiment_corrections sc
                JOIN articles a ON sc.article_url = a.url
                WHERE a.ai_market_impact != '' AND a.ai_market_impact IS NOT NULL
                  AND a.word_market_impact != '' AND a.word_market_impact IS NOT NULL
                  AND a.ai_market_impact != a.word_market_impact
                  AND sc.corrected_impact != '' AND sc.corrected_impact IS NOT NULL
            """)

            iai_wins = 0
            iwords_wins = 0
            ineither = 0
            itotal = 0
            iexamples = []

            for row in c.fetchall():
                itotal += 1
                ai_imp, word_imp, corrected_imp, source, keyword = row
                if corrected_imp == ai_imp:
                    iai_wins += 1
                elif corrected_imp == word_imp:
                    iwords_wins += 1
                else:
                    ineither += 1
                if len(iexamples) < 10:
                    iexamples.append({
                        "ai_said": ai_imp, "words_said": word_imp,
                        "correct_was": corrected_imp, "source": source, "keyword": keyword
                    })

            impact_result = {
                "total": itotal,
                "ai_was_right": iai_wins,
                "words_were_right": iwords_wins,
                "neither_right": ineither,
                "ai_accuracy": round(iai_wins / max(itotal, 1), 3),
                "word_accuracy": round(iwords_wins / max(itotal, 1), 3),
                "examples": iexamples,
            }
        except Exception:
            pass  # Columns may not exist in old DBs

        conn.close()

        # Merge: tone_result is the backward-compatible top level,
        # impact_result is added as a nested key
        tone_result["impact_disagreements"] = impact_result
        return tone_result

    except Exception as e:
        log.debug(f"Disagreement outcomes error: {e}")
        return {"total_disagreements_corrected": 0, "ai_was_right": 0, "words_were_right": 0,
                "impact_disagreements": {"total": 0, "ai_was_right": 0, "words_were_right": 0}}


CONFIG_PATH = Path("config.json")

DEFAULT_CONFIG = {
    "_comment": "Add your API keys below. All are optional — the monitor will skip sources without keys.",
    "keywords": [
        "tariff", "china", "fed", "rate", "ban", "deal", "executive order",
        "sanctions", "oil", "crypto", "bitcoin", "regulation", "tax",
        "trade war", "deficit", "inflation", "interest rate", "treasury",
        "nasdaq", "dow", "s&p", "semiconductor", "chips", "energy",
        "drill", "opec", "iran", "russia", "ukraine", "north korea",
    ],
    "monitored_accounts": [
        {"platform": "x", "username": "realDonaldTrump", "label": "Donald Trump"},
        {"platform": "truthsocial", "username": "realDonaldTrump", "label": "Donald Trump"},
    ],
    "poll_interval_seconds": 120,
    "max_article_age_hours": 4,
    "ollama_url": "",
    "ollama_model": "llama3",
    "ollama_chat_url": "",
    "ollama_chat_model": "",
    "ollama_analysis_url": "",
    "ollama_analysis_model": "",
    "newsapi_key": "",
    "twitter_bearer_token": "",
    "fred_api_key": "",
    "alpha_vantage_key": "",
    "etherscan_key": "",
    "truthsocial_enabled": True,
    "rss_enabled": True,
    "whitehouse_enabled": True,
    "sec_edgar_enabled": True,
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log.info(f"Created default config at {CONFIG_PATH} — edit it to add API keys.")
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


# ─── Ollama Lane Configuration ──────────────────────────────────────────────
# Two logical lanes: "chat" (interactive, low latency) and "analysis" (background, structured output)
# Each lane can point to a different Ollama host and/or model.
# Falls back to the shared ollama_url/ollama_model if lane-specific keys are empty.

# Per-lane generation settings
OLLAMA_CHAT_TIMEOUT = 30        # Chat should respond fast or fail fast
OLLAMA_CHAT_NUM_PREDICT = 512   # Cap chat response length for speed
OLLAMA_CHAT_TEMPERATURE = 0.3   # Low creativity for factual chat

OLLAMA_ANALYSIS_TIMEOUT = 90    # Analysis can take longer (structured JSON + retries)
OLLAMA_ANALYSIS_NUM_PREDICT = 2048  # Thinking models need extra room for <think> tokens
OLLAMA_ANALYSIS_TEMPERATURE = 0.1   # Very low for consistent JSON output


def get_ollama_lane(config, lane="chat"):
    """Resolve Ollama URL and model for a given lane.
    Lane is 'chat' or 'analysis'. Falls back to shared config if lane-specific keys are empty."""
    if lane == "chat":
        url = config.get("ollama_chat_url", "") or config.get("ollama_url", "")
        model = config.get("ollama_chat_model", "") or config.get("ollama_model", "llama3")
    else:
        url = config.get("ollama_analysis_url", "") or config.get("ollama_url", "")
        model = config.get("ollama_analysis_model", "") or config.get("ollama_model", "llama3")
    return url, model


# Active chat counter — background enrichment yields when chat is active
_chat_active = 0
_chat_active_lock = threading.Lock()


RSS_SOURCES = {
    # ── Major News Outlets ──
    "reuters": {
        "name": "Reuters",
        "feeds": [
            "https://feeds.reuters.com/reuters/topNews",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://feeds.reuters.com/reuters/politicsNews",
        ],
    },
    "ap_news": {
        "name": "AP News",
        "feeds": [
            "https://rsshub.app/apnews/topics/apf-topnews",
            "https://rsshub.app/apnews/topics/apf-politics",
            "https://rsshub.app/apnews/topics/apf-business",
        ],
    },
    "cnbc": {
        "name": "CNBC",
        "feeds": [
            "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
            "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
            "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135",
        ],
    },
    "wsj": {
        "name": "Wall Street Journal",
        "feeds": [
            "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
            "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        ],
    },
    "foxnews": {
        "name": "Fox News",
        "feeds": [
            "https://moxie.foxnews.com/google-publisher/politics.xml",
            "https://moxie.foxnews.com/google-publisher/latest.xml",
        ],
    },
    "cnn": {
        "name": "CNN",
        "feeds": [
            "https://rss.cnn.com/rss/cnn_topstories.rss",
            "https://rss.cnn.com/rss/money_latest.rss",
            "https://rss.cnn.com/rss/cnn_allpolitics.rss",
        ],
    },
    "nytimes": {
        "name": "New York Times",
        "feeds": [
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        ],
    },
    "bloomberg": {
        "name": "Bloomberg",
        "feeds": [
            "https://feeds.bloomberg.com/politics/news.rss",
            "https://feeds.bloomberg.com/markets/news.rss",
        ],
    },
    # ── Government / Regulatory ──
    "sec_edgar": {
        "name": "SEC EDGAR",
        "feeds": [
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&start=0&output=atom",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-K&dateb=&owner=include&count=20&search_text=&start=0&output=atom",
            "https://www.sec.gov/rss/news/press.xml",
        ],
    },
    "federal_register": {
        "name": "Federal Register",
        "feeds": [
            "https://www.federalregister.gov/documents/search.atom?conditions%5Btype%5D=RULE",
            "https://www.federalregister.gov/documents/search.atom?conditions%5Btype%5D=PRESDOCU",
            "https://www.federalregister.gov/documents/search.atom?conditions%5Btype%5D=NOTICE",
        ],
    },
    "congress": {
        "name": "Congress.gov",
        "feeds": [
            "https://www.congress.gov/rss/most-viewed-bills.xml",
            "https://www.congress.gov/rss/presented-to-president.xml",
        ],
    },
    "fed_reserve": {
        "name": "Federal Reserve",
        "feeds": [
            "https://www.federalreserve.gov/feeds/press_all.xml",
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
        ],
    },
    "treasury": {
        "name": "U.S. Treasury",
        "feeds": [
            "https://home.treasury.gov/system/files/136/press-releases.xml",
        ],
    },
    # ── Financial / Markets ──
    "marketwatch": {
        "name": "MarketWatch",
        "feeds": [
            "https://feeds.marketwatch.com/marketwatch/topstories",
            "https://feeds.marketwatch.com/marketwatch/marketpulse",
        ],
    },
    "ft": {
        "name": "Financial Times",
        "feeds": [
            "https://www.ft.com/rss/home",
            "https://www.ft.com/rss/companies",
            "https://www.ft.com/rss/markets",
        ],
    },
    "yahoo_finance": {
        "name": "Yahoo Finance",
        "feeds": [
            "https://finance.yahoo.com/news/rssindex",
        ],
    },
    # ── Broad Aggregation ──
    "google_news": {
        "name": "Google News",
        "feeds": [
            "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",  # Business
            "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",  # Technology
            "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTRGdvSUwyMHZNRFZ4ZERBU0FtVnVLQUFQAQ?hl=en-US&gl=US&ceid=US:en",          # Politics
            "https://news.google.com/rss/search?q=Trump+tariff+trade&hl=en-US&gl=US&ceid=US:en",
            "https://news.google.com/rss/search?q=Trump+executive+order&hl=en-US&gl=US&ceid=US:en",
        ],
    },
    # ── Energy / Commodities ──
    "eia": {
        "name": "EIA (Energy Info)",
        "feeds": [
            "https://www.eia.gov/rss/todayinenergy.xml",
        ],
    },
    "opec": {
        "name": "OPEC",
        "feeds": [
            "https://www.opec.org/opec_web/en/pressreleases.rss",
        ],
    },
    # ── Crypto ──
    "coindesk": {
        "name": "CoinDesk",
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
        ],
    },
    "cointelegraph": {
        "name": "CoinTelegraph",
        "feeds": [
            "https://cointelegraph.com/rss",
        ],
    },
}

WHITEHOUSE_URL = "https://www.whitehouse.gov/presidential-actions/"


def parse_account_url(url):
    """
    Parse a profile URL into (platform, username).
    Supports:
      https://x.com/username
      https://twitter.com/username
      https://truthsocial.com/@username
    Returns (platform, username) or (None, None).
    """
    url = (url or "").strip().rstrip("/")
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        path = parsed.path.strip("/")
    except Exception:
        return None, None

    if not path:
        return None, None

    # Strip leading @ if present
    username = path.split("/")[0].lstrip("@")
    if not username or "/" in username:
        return None, None

    if host in ("x.com", "twitter.com"):
        return "x", username
    elif host == "truthsocial.com":
        return "truthsocial", username
    return None, None


class AlertStore:
    """Thread-safe in-memory alert store with deduplication and async AI enrichment."""

    def __init__(self, max_alerts=500):
        self.alerts = []
        self.seen_hashes = set()
        self.max_alerts = max_alerts
        self.lock = threading.Lock()
        # Analysis lane config (set from config.json on each monitor_loop cycle)
        self.ollama_url = ""
        self.ollama_model = "llama3"
        # Chat lane config (used by worker 2 when chat is idle)
        self.ollama_chat_url = ""
        self.ollama_chat_model = "llama3"
        self._ai_queue = []
        self._ai_lock = threading.Lock()
        # Worker 1: analysis lane (always runs, yields to chat)
        self._ai_thread = threading.Thread(target=self._ai_worker, args=("analysis",), daemon=True)
        self._ai_thread.start()
        # Worker 2: chat lane model for analysis (only when chat is idle)
        self._ai_thread2 = threading.Thread(target=self._ai_worker, args=("chat_assist",), daemon=True)
        self._ai_thread2.start()

    def _hash(self, source_id, title, url):
        dedupe_basis = url.strip() if url else f"{source_id}:{title.strip().lower()}"
        return hashlib.md5(dedupe_basis.encode()).hexdigest()

    def _is_semantic_duplicate(self, title):
        """Check if a title is too similar to a recent alert (Jaccard similarity)."""
        title_words = set(re.sub(r'[^\w\s]', '', title.lower()).split())
        if len(title_words) < 3:
            return False, None
        for alert in self.alerts[:60]:
            existing_words = set(re.sub(r'[^\w\s]', '', alert["title"].lower()).split())
            if len(existing_words) < 3:
                continue
            intersection = title_words & existing_words
            union = title_words | existing_words
            similarity = len(intersection) / len(union)
            if similarity > 0.65:
                return True, alert["id"]
        return False, None

    def _ai_worker(self, role="analysis"):
        """Background worker for AI enrichment.
        role='analysis': uses analysis lane, yields only during active chat.
        role='chat_assist': uses chat lane model for analysis, pauses entirely during chat."""
        while True:
            with _chat_active_lock:
                chat_busy = _chat_active > 0

            if role == "chat_assist":
                # Chat-assist worker: only runs when chat is completely idle
                if chat_busy or not self.ollama_chat_url:
                    time.sleep(2)
                    continue
            else:
                # Analysis worker: yields briefly during chat, doesn't fully stop
                if chat_busy:
                    time.sleep(1)
                    continue

            job = None
            with self._ai_lock:
                if self._ai_queue:
                    job = self._ai_queue.pop(0)
            if job:
                try:
                    # Choose which model to use based on role
                    if role == "chat_assist":
                        job["_use_chat_lane"] = True
                    self._enrich_with_ai(job)
                except Exception as e:
                    log.warning(f"AI enrichment error ({role}): {e}")
            else:
                time.sleep(2)

    def _enrich_with_ai(self, job):
        """Fetch article (if needed), run Ollama analysis, ensemble with lexicon, update alert."""
        alert_id = job["alert_id"]
        source_id = job["source_id"]
        keyword = job["keyword"]

        # Phase 1: Fetch article text if not already done
        analysis_text = job.get("snippet_text", "")
        if job.get("needs_fetch") and job.get("url"):
            t0 = time.time()
            article_text, fetched = fetch_article_text(job["url"])
            fetch_ms = int((time.time() - t0) * 1000)
            if fetched and article_text:
                analysis_text = article_text
                # Re-run lexicon on full text for better accuracy
                word_result = analyze_sentiment_words_only(
                    analysis_text, source_id=source_id, matched_keywords=[keyword],
                )
                with self.lock:
                    for a in self.alerts:
                        if a["id"] == alert_id:
                            a["article_fetched"] = True
                            a["fetch_duration_ms"] = fetch_ms
                            a["word_sentiment"] = word_result["article_tone"]
                            a["word_market_impact"] = word_result["market_impact"]
                            a["positive_signals"] = word_result["positive_signals"]
                            a["negative_signals"] = word_result["negative_signals"]
                            # Update lexicon classification with full text
                            if not a.get("user_corrected"):
                                a["article_tone"] = word_result["article_tone"]
                                a["market_impact"] = word_result["market_impact"]
                                a["sentiment"] = word_result["article_tone"]
                                a["confidence"] = word_result["confidence"]
                            # Update summary
                            sentences = re.split(r'(?<=[.!?])\s+', analysis_text[:600])
                            a["summary"] = " ".join(sentences[:2]).strip()[:250]
                            break
                log_article({"url": job["url"], "article_fetched": True,
                             "fetch_duration_ms": fetch_ms,
                             "word_sentiment": word_result["article_tone"],
                             "word_market_impact": word_result["market_impact"]}, article_text)

        # Determine which Ollama lane to use
        use_chat = job.get("_use_chat_lane", False)
        active_url = self.ollama_chat_url if use_chat else self.ollama_url
        active_model = self.ollama_chat_model if use_chat else self.ollama_model

        if not active_url:
            with self.lock:
                for a in self.alerts:
                    if a["id"] == alert_id:
                        a["ai_processing"] = False
                        break
            return

        # Phase 2: Run Ollama
        llm_result, raw_response, success = ollama_analyze(
            analysis_text, active_url, active_model, keyword=keyword
        )

        with self.lock:
            for alert in self.alerts:
                if alert["id"] == alert_id:
                    alert["ai_processing"] = False
                    alert["ai_analysis_raw"] = raw_response[:2000] if raw_response else ""

                    if not success:
                        log.debug(f"AI enrichment failed for alert {alert_id}")
                        log_system_event("ollama_fail", f"Enrichment failed: {alert.get('title', '')[:60]}", raw_response[:500])
                        break

                    # ─── Ensemble: combine lexicon + LLM ───
                    lexicon_tone = alert.get("article_tone", alert.get("sentiment", "neutral"))
                    lexicon_impact = alert.get("word_market_impact", alert.get("market_impact", "neutral"))
                    llm_tone = llm_result.get("article_tone", "neutral")
                    llm_impact = llm_result.get("market_impact", "neutral")
                    llm_conf = llm_result.get("confidence", 0.5)
                    lex_conf = alert.get("confidence", 0.3)
                    source_reliability = SOURCE_WEIGHTS.get(source_id, 0.5)

                    # Store per-engine results
                    alert["ai_sentiment"] = llm_tone
                    alert["ai_market_impact"] = llm_impact

                    # Summary (always prefer LLM)
                    if llm_result.get("summary"):
                        alert["summary"] = llm_result["summary"][:500]
                        alert["ai_summary"] = True

                    # ─── Tone ensemble ───
                    if lexicon_tone == llm_tone:
                        final_tone = llm_tone
                        final_conf = min(1.0, llm_conf * 0.6 + lex_conf * 0.3 + 0.1)
                        method = "ensemble_agree"
                    elif lexicon_tone == "neutral":
                        final_tone = llm_tone
                        final_conf = llm_conf * 0.7
                        method = "llm_dominant"
                    elif llm_tone == "neutral":
                        if lex_conf > 0.5:
                            final_tone = lexicon_tone
                            final_conf = lex_conf * 0.5
                            method = "lexicon_dominant"
                        else:
                            final_tone = "neutral"
                            final_conf = 0.4
                            method = "both_weak"
                    else:
                        stats = get_correction_stats(source_id, keyword)
                        if stats["sample_size"] > 5 and stats["llm_accuracy"] > stats["lexicon_accuracy"] + 0.1:
                            final_tone = llm_tone
                            final_conf = 0.4
                            method = "llm_override_learned"
                        elif stats["sample_size"] > 5 and stats["lexicon_accuracy"] > stats["llm_accuracy"] + 0.1:
                            final_tone = lexicon_tone
                            final_conf = 0.4
                            method = "lexicon_override_learned"
                        else:
                            final_tone = llm_tone
                            final_conf = 0.35
                            method = "llm_override"

                    # ─── Impact ensemble (separate from tone) ───
                    # Confidence starts from the tone-derived value; impact modifies it
                    if lexicon_impact == llm_impact:
                        final_impact = llm_impact
                        # Agreement on impact boosts confidence
                        final_conf = min(1.0, final_conf * 1.1)
                        impact_method = "impact_agree"
                    elif lexicon_impact == "neutral":
                        final_impact = llm_impact
                        impact_method = "impact_llm_dominant"
                    elif llm_impact == "neutral":
                        final_impact = lexicon_impact if lex_conf > 0.5 else "neutral"
                        impact_method = "impact_lex_dominant"
                    else:
                        # Direct disagreement on impact — use impact-specific learned accuracy
                        stats = get_correction_stats(source_id, keyword)
                        if stats["impact_sample_size"] > 3 and stats["llm_impact_accuracy"] > stats["lexicon_impact_accuracy"] + 0.1:
                            final_impact = llm_impact
                            impact_method = "impact_llm_learned"
                        elif stats["impact_sample_size"] > 3 and stats["lexicon_impact_accuracy"] > stats["llm_impact_accuracy"] + 0.1:
                            final_impact = lexicon_impact
                            impact_method = "impact_lex_learned"
                        else:
                            final_impact = llm_impact  # default LLM
                            impact_method = "impact_llm_default"
                        # Disagreement on impact reduces confidence
                        final_conf *= 0.8

                    # Source reliability affects confidence only
                    final_conf *= (0.7 + source_reliability * 0.3)

                    # Update alert
                    alert["article_tone"] = final_tone
                    alert["market_impact"] = final_impact
                    alert["sentiment"] = final_tone
                    alert["confidence"] = round(min(1.0, final_conf), 2)
                    alert["classification_method"] = method
                    alert["event_type"] = llm_result.get("event_type", "other")
                    alert["scope"] = llm_result.get("scope", "broad_market")
                    alert["time_horizon"] = llm_result.get("time_horizon", "unclear")
                    alert["mixed_signals"] = llm_result.get("mixed_signals", False) or alert.get("mixed_signals", False)

                    event = llm_result.get("event")
                    if event and isinstance(event, dict):
                        alert["event"] = event

                    # Severity
                    if final_impact in ("bullish", "bearish") and final_conf > 0.6:
                        alert["severity"] = "high"
                    elif final_conf > 0.4:
                        alert["severity"] = "medium"
                    else:
                        alert["severity"] = "low"

                    # Re-log with all enriched data
                    log_article(alert, analysis_text)

                    emoji_map = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}
                    emoji = emoji_map.get(final_impact, "➡️")
                    disagree = " ⚡DISAGREE" if lexicon_tone != llm_tone and lexicon_tone != "neutral" else ""
                    impact_disagree = " ⚡IMPACT" if lexicon_impact != llm_impact and lexicon_impact != "neutral" else ""
                    log.info(f"🤖{emoji}{disagree}{impact_disagree} [{method}] tone={final_tone} impact={final_impact} conf={alert['confidence']} → {alert['title'][:55]}")
                    break

    def add(self, source_id, source_name, title, url, snippet, keyword, severity="medium", published_at=None):
        if not title or not is_valid_source_url(url):
            return False

        # Canonicalize URL for better dedup
        clean_url = re.sub(r'[?#].*$', '', url.strip()) if url else ""
        h = self._hash(source_id, title, clean_url or url)

        with self.lock:
            if h in self.seen_hashes:
                return False
            self.seen_hashes.add(h)

            # Semantic duplicate check — merge if similar title exists
            is_dup, dup_id = self._is_semantic_duplicate(title)
            if is_dup and dup_id:
                for a in self.alerts:
                    if a["id"] == dup_id:
                        also = a.get("also_reported_by", [])
                        if source_name not in also and source_name != a.get("source_name"):
                            also.append(source_name)
                            a["also_reported_by"] = also
                            a["report_count"] = a.get("report_count", 1) + 1
                        break
                log.debug(f"Semantic dup merged: '{title[:50]}' → existing alert {dup_id}")
                return False

        # Run lexicon on title + snippet ONLY (fast, no network)
        quick_text = f"{title}. {snippet or ''}"
        word_result = analyze_sentiment_words_only(
            quick_text, source_id=source_id, matched_keywords=[keyword],
        )

        # Fallback summary from snippet
        fallback_summary = (snippet or title)[:250]

        # Store published_at as UTC; frontend handles display timezone
        pub_str = ""
        pub_utc = ""
        if published_at:
            try:
                pub_utc = published_at.astimezone(timezone.utc).isoformat()
                pub_str = published_at.astimezone(timezone.utc).strftime("%b %d, %I:%M %p UTC")
            except Exception:
                pub_str = str(published_at)

        # Ticker linking
        affected_tickers = link_tickers_to_alert(title, quick_text, keyword,
                                                  extract_entities(quick_text))

        with self.lock:
            alert_id = int(time.time() * 1000)
            alert = {
                "id": alert_id,
                "source": source_id,
                "source_name": source_name,
                "title": title.strip(),
                "url": url.strip(),
                "snippet": (snippet or "").strip(),
                "keyword": keyword.strip().lower(),
                # Multi-dimensional classification
                "article_tone": word_result["article_tone"],
                "market_impact": word_result["market_impact"],
                "word_market_impact": word_result["market_impact"],
                "sentiment": word_result["article_tone"],  # backward compat
                "sentiment_score": word_result["polarity"],
                "confidence": word_result["confidence"],
                "severity": word_result["severity"],
                "mixed_signals": word_result.get("mixed_signals", False),
                "event_type": "other",
                "scope": "broad_market",
                "time_horizon": "unclear",
                "classification_method": "lexicon_only",
                # Event extraction (populated by AI)
                "event": None,
                # Ticker linking
                "affected_tickers": affected_tickers,
                # Duplicate tracking
                "report_count": 1,
                "also_reported_by": [],
                # AI fields — populated async
                "summary": fallback_summary,
                "ai_summary": False,
                "ai_sentiment": None,
                "ai_market_impact": None,
                "ai_processing": bool(self.ollama_url),
                "word_sentiment": word_result["article_tone"],
                "positive_signals": word_result["positive_signals"],
                "negative_signals": word_result["negative_signals"],
                "article_fetched": False,
                "published_at": pub_str,
                "published_at_utc": pub_utc,
                "source_reliability": word_result.get("source_reliability", 0.5),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.alerts.insert(0, alert)
            if len(self.alerts) > self.max_alerts:
                self.alerts = self.alerts[: self.max_alerts]

            log_article(alert, "")

            tickers_str = f" [{','.join(affected_tickers)}]" if affected_tickers else ""
            emoji_map = {"positive": "📈", "negative": "📉", "neutral": "➡️"}
            emoji = emoji_map.get(word_result["article_tone"], "➡️")
            log.info(f"{emoji} [{source_id}] tone={word_result['article_tone']} conf={word_result['confidence']}{tickers_str} keyword='{keyword}' → {title[:60]} [AI queued]")

        # Queue background fetch + AI enrichment (article text fetched async)
        if self.ollama_url:
            with self._ai_lock:
                self._ai_queue.append({
                    "alert_id": alert_id,
                    "url": url,
                    "snippet_text": quick_text,
                    "source_id": source_id,
                    "keyword": keyword,
                    "needs_fetch": True,
                })
        return True

    def get_all(self, since=None, limit=None):
        with self.lock:
            items = self.alerts
            if since:
                items = [a for a in items if a["timestamp"] > since]
            if limit is not None:
                items = items[:limit]
            return list(items)

    def clear(self):
        with self.lock:
            self.alerts.clear()
            self.seen_hashes.clear()


store = AlertStore()


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", text or "").strip()


def is_valid_source_url(url):
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def keyword_regex(keyword):
    escaped = re.escape(keyword.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def match_keywords(text, keywords):
    """Return list of (keyword, severity) tuples found in text with word-boundary-aware matching."""
    clean_text = normalize_whitespace(text)
    matches = []
    for kw in keywords:
        pattern = keyword_regex(kw)
        found = list(pattern.finditer(clean_text))
        if not found:
            continue
        if len(found) > 2:
            sev = "high"
        elif len(found) > 1:
            sev = "medium"
        elif found[0].start() < 140:
            sev = "medium"
        else:
            sev = "low"
        matches.append((kw, sev))
    return matches


def extract_snippet(text, keyword, context_chars=120):
    """Pull a snippet around the keyword occurrence."""
    clean_text = normalize_whitespace(BeautifulSoup(text or "", "html.parser").get_text(" "))
    pattern = keyword_regex(keyword)
    match = pattern.search(clean_text)
    if not match:
        return (clean_text[:250] + "...") if len(clean_text) > 250 else clean_text
    start = max(0, match.start() - context_chars)
    end = min(len(clean_text), match.end() + context_chars)
    snippet = clean_text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(clean_text):
        snippet = snippet + "..."
    return snippet


# ─── Sentiment Analysis Engine ────────────────────────────────────────────────

# Source credibility weights — higher = more market impact
SOURCE_WEIGHTS = {
    "whitehouse": 1.0, "fed_reserve": 1.0, "sec_edgar": 0.95, "treasury": 0.95,
    "federal_register": 0.9, "congress": 0.85,
    "truthsocial": 0.9, "x_twitter": 0.85,
    "reuters": 0.85, "ap_news": 0.85, "bloomberg": 0.85, "wsj": 0.8,
    "ft": 0.8, "cnbc": 0.75, "nytimes": 0.7, "cnn": 0.65, "foxnews": 0.65,
    "marketwatch": 0.7, "yahoo_finance": 0.6, "google_news": 0.55,
    "newsapi": 0.6, "coindesk": 0.65, "cointelegraph": 0.6,
    "eia": 0.75, "opec": 0.8,
}

# Default categorized signal word sets
DEFAULT_SIGNAL_SETS = {
    "general_market": {
        "label": "General Market",
        "enabled": True,
        "positive": {
            "surge": 3, "soar": 3, "rally": 3, "boom": 3, "skyrocket": 3,
            "gain": 2, "rise": 2, "climb": 2, "jump": 2, "advance": 2,
            "recover": 2, "rebound": 2, "uptick": 2, "bullish": 2,
            "growth": 2, "expand": 2, "strong": 1.5, "boost": 2,
            "optimism": 2, "confidence": 1.5, "upgrade": 2,
            "beat expectations": 3, "exceed": 2, "outperform": 2,
            "profit": 1.5, "revenue growth": 2, "record high": 3,
            "breakthrough": 3, "historic deal": 4,
        },
        "negative": {
            "crash": 4, "plunge": 4, "collapse": 4, "plummet": 4, "freefall": 4,
            "drop": 2, "fall": 1.5, "decline": 2, "slip": 1.5, "tumble": 2.5,
            "selloff": 2.5, "sell-off": 2.5, "bearish": 2, "downturn": 2,
            "loss": 1.5, "volatil": 1.5, "risk": 1, "panic": 3,
            "missed expectations": 3, "underperform": 2, "downgrade": 2.5,
            "warn": 2, "fear": 2, "concern": 1.5, "uncertain": 1.5,
        },
    },
    "politics": {
        "label": "Politics & Policy",
        "enabled": True,
        "positive": {
            "bipartisan": 1.5, "unanimously": 2, "signed into law": 2,
            "approve": 2, "agreement": 2, "deal": 1.5, "cooperat": 1.5,
            "peace": 2, "diplomacy": 1.5, "alliance": 1.5,
            "cut taxes": 3, "tax cut": 3, "stimulus": 2.5,
            "deregulat": 2, "ease restrictions": 2, "lift sanctions": 2,
        },
        "negative": {
            "impeach": 2, "veto": 2, "block": 1.5, "reject": 2, "oppose": 1.5,
            "shutdown": 2.5, "government shutdown": 3, "debt ceiling": 2.5,
            "sanction": 2, "tariff": 1.5, "trade war": 2.5, "ban": 2,
            "restrict": 2, "penalt": 2, "investigat": 1.5,
            "indict": 3, "lawsuit": 2, "subpoena": 2,
            "escalat": 2, "retaliat": 2.5, "executive order": 1,
        },
    },
    "economy": {
        "label": "Economy & Fed",
        "enabled": True,
        "positive": {
            "rate cut": 2.5, "jobs added": 2, "unemployment fell": 3,
            "unemployment low": 2, "gdp growth": 2.5, "consumer spending": 2,
            "wage growth": 2, "manufacturing up": 2,
        },
        "negative": {
            "recession": 3, "depression": 3, "inflation": 1.5,
            "deficit": 1.5, "debt": 1, "default": 3,
            "unemployment rose": 3, "unemployment high": 2,
            "rate hike": 2, "layoff": 2.5, "cut jobs": 2.5, "downsiz": 2,
            "bankrupt": 3, "insolven": 3,
        },
    },
    "geopolitics": {
        "label": "Geopolitics & Defense",
        "enabled": True,
        "positive": {
            "ceasefire": 3, "peace deal": 3, "de-escalat": 2.5,
            "withdraw troops": 2, "treaty": 2, "normalize relations": 2.5,
        },
        "negative": {
            "war": 2, "conflict": 2, "invasion": 3, "missile": 2.5,
            "nuclear": 2, "military strike": 3, "troops deployed": 2,
            "threat": 2, "crisis": 3, "catastroph": 3,
            "sanctions russia": 2.5, "sanctions iran": 2.5, "sanctions china": 3,
        },
    },
    "crypto": {
        "label": "Crypto & Digital Assets",
        "enabled": True,
        "positive": {
            "bitcoin rally": 3, "crypto adoption": 2.5, "etf approved": 3,
            "institutional buying": 2.5, "hash rate high": 2,
            "defi growth": 2, "stablecoin": 1, "halving": 2,
            "bullish on crypto": 3, "strategic reserve": 3,
            "crypto capital": 2.5, "web3": 1.5, "blockchain adoption": 2,
        },
        "negative": {
            "crypto crash": 4, "rug pull": 3, "exchange hack": 3,
            "crypto ban": 3, "sec lawsuit crypto": 3, "depegged": 3,
            "liquidation": 2.5, "crypto fraud": 3, "ponzi": 3,
            "mining ban": 2.5, "crypto regulation": 1.5, "stablecoin collapse": 4,
        },
    },
    "tech": {
        "label": "Tech & AI",
        "enabled": True,
        "positive": {
            "ai breakthrough": 3, "chip demand": 2.5, "cloud growth": 2,
            "tech rally": 2.5, "innovation": 2, "patent": 1.5,
            "product launch": 2, "user growth": 2, "tech earnings beat": 3,
            "semiconductor demand": 2.5, "data center": 1.5,
        },
        "negative": {
            "tech layoff": 3, "antitrust": 2.5, "data breach": 2.5,
            "chip shortage": 2, "export ban chips": 3, "tech selloff": 3,
            "ai regulation": 1.5, "monopoly": 2, "tech bubble": 2.5,
            "semiconductor restriction": 2.5,
        },
    },
    "energy": {
        "label": "Energy & Commodities",
        "enabled": True,
        "positive": {
            "oil rally": 2.5, "opec cut": 2.5, "energy independence": 2,
            "drill": 1.5, "lng export": 2, "refinery output": 2,
            "renewable investment": 2, "production increase": 2,
            "solar growth": 2, "wind energy": 1.5, "battery breakthrough": 2.5,
            "ev demand": 2, "lithium supply": 1.5,
        },
        "negative": {
            "oil crash": 3, "opec flood": 2.5, "pipeline shut": 2.5,
            "energy crisis": 3, "gas shortage": 2.5, "oil embargo": 3,
            "refinery fire": 2, "supply disruption": 2.5,
            "ev recall": 2, "grid failure": 2.5, "blackout": 2.5,
        },
    },
    "healthcare": {
        "label": "Healthcare & Biotech",
        "enabled": True,
        "positive": {
            "fda approved": 3, "fda approval": 3, "clinical trial success": 3,
            "drug breakthrough": 3, "vaccine effective": 2.5, "patent granted": 2,
            "merger healthcare": 2, "biotech rally": 2.5, "phase 3 success": 3,
            "revenue beat pharma": 2.5, "healthcare expansion": 2,
        },
        "negative": {
            "fda rejected": 3, "clinical trial failed": 3, "drug recall": 3,
            "side effects": 2, "patent expired": 2, "pricing pressure": 2,
            "opioid lawsuit": 2.5, "pandemic": 3, "outbreak": 2.5,
            "healthcare cuts": 2, "medicaid cut": 2.5,
        },
    },
    "financial_services": {
        "label": "Financial Services",
        "enabled": True,
        "positive": {
            "ipo successful": 2.5, "earnings beat bank": 2.5, "loan growth": 2,
            "fintech adoption": 2, "credit upgrade": 2.5, "dividend increase": 2,
            "buyback": 2, "asset growth": 2, "merger acquisition": 2,
        },
        "negative": {
            "bank failure": 4, "credit downgrade": 3, "loan default": 3,
            "margin call": 3, "liquidity crisis": 3, "run on bank": 4,
            "fraud": 3, "sec investigation": 2.5, "ponzi": 3,
            "interest rate risk": 2, "bad loans": 2.5, "write-down": 2,
        },
    },
    "real_estate": {
        "label": "Real Estate",
        "enabled": False,
        "positive": {
            "housing starts": 2, "home sales up": 2, "mortgage rate drop": 2.5,
            "reit dividend": 2, "commercial lease": 1.5, "property value": 2,
        },
        "negative": {
            "housing crash": 3, "mortgage default": 3, "foreclosure": 2.5,
            "commercial vacancy": 2, "rent decline": 2, "property devalue": 2.5,
            "housing bubble": 2.5, "eviction": 1.5,
        },
    },
    "custom": {
        "label": "Custom",
        "enabled": True,
        "positive": {},
        "negative": {},
    },
}

AMPLIFIERS = {
    "breaking": 1.5, "just in": 1.5, "alert": 1.3, "urgent": 1.5,
    "exclusive": 1.3, "developing": 1.2, "major": 1.3,
    "immediately": 1.4, "effective immediately": 1.6,
    "executive order": 1.4, "signed": 1.2,
}

HIGH_IMPACT_COMBOS = [
    ({"tariff", "china"}, 2.0), ({"tariff", "eu"}, 1.8),
    ({"tariff", "canada"}, 1.8), ({"tariff", "mexico"}, 1.8),
    ({"rate", "cut"}, 1.8), ({"rate", "hike"}, 1.8),
    ({"executive order"}, 1.5), ({"fed", "rate"}, 1.7),
    ({"sanctions", "russia"}, 1.6), ({"sanctions", "iran"}, 1.6),
    ({"sanctions", "china"}, 1.8), ({"ban", "import"}, 1.7),
    ({"trade", "deal"}, 1.6), ({"debt", "ceiling"}, 1.8),
    ({"government", "shutdown"}, 1.9), ({"nuclear"}, 1.5),
    ({"war"}, 1.5), ({"invasion"}, 1.8),
    ({"default"}, 2.0), ({"recession"}, 1.8),
]

SIGNAL_SETS_PATH = Path("signal_sets.json")


def load_signal_sets():
    """Load signal sets from file, or create defaults."""
    if not SIGNAL_SETS_PATH.exists():
        SIGNAL_SETS_PATH.write_text(json.dumps(DEFAULT_SIGNAL_SETS, indent=2))
    try:
        return json.loads(SIGNAL_SETS_PATH.read_text())
    except Exception:
        return DEFAULT_SIGNAL_SETS


def save_signal_sets(sets):
    SIGNAL_SETS_PATH.write_text(json.dumps(sets, indent=2))


def get_active_signals():
    """Merge all enabled signal sets into flat positive/negative dicts."""
    sets = load_signal_sets()
    pos = {}
    neg = {}
    for cat_id, cat in sets.items():
        if not cat.get("enabled", True):
            continue
        for word, weight in cat.get("positive", {}).items():
            pos[word] = max(pos.get(word, 0), weight)
        for word, weight in cat.get("negative", {}).items():
            neg[word] = max(neg.get(word, 0), weight)
    return pos, neg


# ─── Ollama Integration (Redesigned) ──────────────────────────────────────────

OLLAMA_PROMPT = """You are a financial news classifier for a trading alert system.

TASK: Analyze the article and return a JSON classification. Think step by step:
1. What happened? (the event)
2. Who is affected? (entities, sectors)
3. How does the article frame it? (tone)
4. What does this mean for markets? (impact)
5. How certain is this? (confidence)

DEFINITIONS:
- article_tone: "positive" = optimistic/celebratory writing. "negative" = alarming/critical. "neutral" = factual reporting. Default to neutral.
- market_impact: "bullish" = likely prices rise. "bearish" = likely prices fall. "neutral" = unclear or no effect. Default to neutral.
- confidence: 0.0-1.0. Use 0.3-0.5 for ambiguous. Use 0.7+ only when the event is clear and confirmed.
- event_type: One of: policy, legal, macro, earnings, geopolitics, crypto, social_post, rumor, other
- scope: "single_company" if one company, "sector" if an industry, "broad_market" if economy-wide
- time_horizon: "immediate" = this week, "short_term" = weeks, "long_term" = months+, "unclear"

RULES:
1. Tone and impact CAN differ. Neutral reporting of bad news = neutral tone, bearish impact.
2. Do NOT hallucinate tickers, numbers, or dates not in the article.
3. Do NOT copy the headline verbatim as the summary. Synthesize.
4. If speculative language ("may/could/reportedly"), lower confidence by 0.2.
5. If both positive and negative elements, set mixed_signals: true.
6. Prefer neutral over guessing.

EXAMPLES:

Article: "The Federal Reserve held interest rates steady at 5.25-5.50% on Wednesday, as expected by markets."
Output: {{"article_tone":"neutral","market_impact":"neutral","confidence":0.85,"event_type":"macro","scope":"broad_market","time_horizon":"immediate","mixed_signals":false,"summary":"The Fed held rates steady as expected. No surprise for markets — already priced in.","event":{{"action":"rate_hold","actor":"Federal Reserve","target":"US economy","magnitude":"rates unchanged at 5.25-5.50%","is_confirmed":true}}}}

Article: "Markets rallied sharply after Trump posted on Truth Social that he would pause tariffs on China for 90 days, sending the S&P 500 up 3.2%."
Output: {{"article_tone":"positive","market_impact":"bullish","confidence":0.82,"event_type":"policy","scope":"broad_market","time_horizon":"short_term","mixed_signals":false,"summary":"Trump announced a 90-day pause on China tariffs, triggering a broad rally with S&P 500 up 3.2%. Impact may be short-term given the limited pause duration.","event":{{"action":"tariff_pause","actor":"Donald Trump","target":"China","magnitude":"90-day pause","is_confirmed":true}}}}

Article: "Oil prices edged higher despite OPEC announcing increased production quotas, as traders weighed ongoing tensions in the Strait of Hormuz."
Output: {{"article_tone":"neutral","market_impact":"bullish","confidence":0.55,"event_type":"geopolitics","scope":"sector","time_horizon":"short_term","mixed_signals":true,"summary":"Oil rose despite OPEC increasing output, as geopolitical risk in the Strait of Hormuz outweighed supply concerns. Conflicting forces suggest continued volatility.","event":{{"action":"production_increase","actor":"OPEC","target":"oil markets","magnitude":"increased quotas","is_confirmed":true}}}}

{trend_context}ARTICLE:
{article_text}

Respond with ONLY valid JSON (no other text):
{{"article_tone":"string","market_impact":"string","confidence":0.0,"event_type":"string","scope":"string","time_horizon":"string","mixed_signals":false,"summary":"string","event":{{"action":"string","actor":"string","target":"string","magnitude":"string","is_confirmed":true}}}}"""


def parse_llm_response(raw_response):
    """Parse and validate LLM JSON response with fallbacks.
    Handles thinking models (Qwen, DeepSeek R1) that emit <think>...</think> blocks."""
    text = raw_response.strip()

    # Strip thinking model output — Qwen 3.5, DeepSeek R1 emit <think>...</think> before JSON
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Find JSON object — handle nested braces for event object
    # Greedy match from first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, "no_json_found"

    json_str = text[start:end+1]

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        # Try to fix common issues
        json_str = json_str.replace("'", '"').replace("True", "true").replace("False", "false")
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            return None, "json_parse_error"

    # Validate required fields
    required = {"article_tone", "market_impact", "confidence", "summary"}
    missing = required - set(parsed.keys())
    if missing:
        return None, f"missing_fields:{','.join(missing)}"

    # Validate enums
    valid_tones = {"positive", "neutral", "negative"}
    valid_impacts = {"bullish", "neutral", "bearish"}
    if parsed.get("article_tone") not in valid_tones:
        parsed["article_tone"] = "neutral"
    if parsed.get("market_impact") not in valid_impacts:
        parsed["market_impact"] = "neutral"

    # Clamp confidence
    try:
        parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
    except (ValueError, TypeError):
        parsed["confidence"] = 0.5

    # Fill defaults
    parsed.setdefault("event_type", "other")
    parsed.setdefault("scope", "broad_market")
    parsed.setdefault("time_horizon", "unclear")
    parsed.setdefault("mixed_signals", False)

    # Validate event object if present
    event = parsed.get("event")
    if event and isinstance(event, dict):
        event.setdefault("action", "")
        event.setdefault("actor", "")
        event.setdefault("target", "")
        event.setdefault("magnitude", "")
        event.setdefault("is_confirmed", False)
    else:
        parsed["event"] = None

    return parsed, "ok"


def get_macro_context():
    """Build macro context string from Fear & Greed and cached FRED data for AI prompts."""
    parts = []
    try:
        fg_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if fg_resp.status_code == 200:
            fg = fg_resp.json().get("data", [{}])[0]
            parts.append(f"Market Sentiment: Fear & Greed Index = {fg.get('value', '?')} ({fg.get('value_classification', '?')})")
    except Exception:
        pass
    return " | ".join(parts) if parts else ""


def ollama_analyze(text, ollama_url, model="llama3", keyword=""):
    """
    Call Ollama with JSON-schema prompt on the ANALYSIS lane.
    Returns (parsed_dict, raw_response, success).
    Uses analysis-optimized generation settings.
    """
    if not ollama_url:
        return None, "", False

    trend_ctx = ""
    if keyword:
        ctx = build_trend_context(keyword=keyword, limit=3)  # Reduced from 5 to 3
        if ctx:
            trend_ctx = f"RECENT CONTEXT:\n{ctx}\n\n"

    # Add macro sentiment context
    macro = get_macro_context()
    if macro:
        trend_ctx += f"MACRO CONTEXT: {macro}\n\n"

    # Add Wikipedia background context for entities (supplementary, not primary)
    wiki_ctx = build_wiki_context(text, keyword=keyword)
    if wiki_ctx:
        trend_ctx += wiki_ctx

    for attempt in range(3):
        try:
            prompt = OLLAMA_PROMPT.format(
                article_text=text[:1800],  # Reduced from 2500 to 1800
                trend_context=trend_ctx,
            )
            if attempt > 0:
                prompt += "\n\nIMPORTANT: Respond with valid JSON ONLY. No other text."

            resp = requests.post(
                f"{ollama_url.rstrip('/')}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "num_predict": OLLAMA_ANALYSIS_NUM_PREDICT,
                        "temperature": OLLAMA_ANALYSIS_TEMPERATURE,
                    },
                },
                timeout=OLLAMA_ANALYSIS_TIMEOUT,
            )
            if resp.status_code != 200:
                log.debug(f"Analysis lane: Ollama returned {resp.status_code} on attempt {attempt+1}")
                continue

            raw = resp.json().get("response", "")
            parsed, status = parse_llm_response(raw)

            if parsed:
                log_system_event("ollama_success", f"Analysis parsed on attempt {attempt+1}", status)
                return parsed, raw, True

            log.debug(f"Analysis lane parse attempt {attempt+1} failed: {status}")

        except requests.exceptions.Timeout:
            log.warning(f"Analysis lane: timeout on attempt {attempt+1} ({OLLAMA_ANALYSIS_TIMEOUT}s)")
            log_system_event("ollama_timeout", f"Analysis timeout attempt {attempt+1}",
                             f"model={model} timeout={OLLAMA_ANALYSIS_TIMEOUT}s")
        except Exception as e:
            log.debug(f"Analysis lane call attempt {attempt+1} error: {e}")

    log_system_event("ollama_fail", "All analysis parse attempts failed", f"model={model}")
    return None, "", False


def fetch_article_text(url, timeout=12):
    """
    Attempt to fetch and extract the main text from an article URL.
    Returns (text, success). Gracefully fails for paywalled/blocked content.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return "", False

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise elements
        for tag in soup.find_all(["script", "style", "nav", "footer", "aside",
                                   "iframe", "noscript", "header", "form",
                                   "button", "svg", "figure", "figcaption"]):
            tag.decompose()
        # Remove common ad/tracker divs
        for cls in ["ad", "ads", "advertisement", "social-share", "newsletter",
                     "related-articles", "sidebar", "comment", "popup", "modal",
                     "cookie", "banner", "promo"]:
            for el in soup.find_all(class_=re.compile(cls, re.I)):
                el.decompose()
            for el in soup.find_all(id=re.compile(cls, re.I)):
                el.decompose()

        # Try article containers in priority order
        article = None
        selectors = [
            "article", '[role="main"]', '[itemprop="articleBody"]',
            ".article-body", ".article__body", ".story-body", ".story-content",
            ".post-content", ".entry-content", ".article-content",
            "#article-body", "#story-body", ".body-text", ".article-text",
            ".content-body", ".text-block", ".paywall", ".article__content",
            "main", ".main-content",
        ]
        for selector in selectors:
            article = soup.select_one(selector)
            if article and len(article.get_text(strip=True)) > 150:
                break
            article = None

        if article:
            text = article.get_text(" ", strip=True)
        else:
            # Fallback: get all paragraphs with substantial text
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")
                          if len(p.get_text(strip=True)) > 40]
            text = " ".join(paragraphs)

        text = normalize_whitespace(text)

        # Filter out common non-article text patterns
        junk_patterns = [
            r"sign up for our newsletter",
            r"subscribe to continue reading",
            r"cookies? (policy|consent|settings)",
            r"accept all cookies",
            r"privacy policy",
            r"terms of (use|service)",
        ]
        for pat in junk_patterns:
            text = re.sub(pat, "", text, flags=re.I)
        text = normalize_whitespace(text)

        if len(text) < 80:
            return "", False
        return text[:4000], True

    except Exception as e:
        log.debug(f"Article fetch failed [{url[:60]}]: {e}")
        return "", False


def split_sentences(text):
    """Split text into sentences."""
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 10]


def compute_position_weight(idx, total):
    """Lead paragraph matters most. Middle is background. End has conclusions."""
    if total <= 1:
        return 1.0
    ratio = idx / total
    if ratio < 0.15:
        return 2.0
    elif ratio < 0.3:
        return 1.5
    elif ratio > 0.85:
        return 1.3
    return 0.8


def quick_polarity(text_fragment):
    """Fast polarity check on a text fragment for contrast clause handling."""
    pos_signals, neg_signals = get_active_signals()
    score = 0.0
    text_lower = text_fragment.lower()
    for word, weight in pos_signals.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text_lower):
            score += weight
    for word, weight in neg_signals.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text_lower):
            score -= weight
    return score


def score_sentence(sentence, pos_signals, neg_signals):
    """Score a single sentence with context-aware rules."""
    tokens = sentence.lower()
    score = 0.0
    signals = []

    # 1. Standard keyword matching with proper word boundaries
    for word, weight in pos_signals.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", tokens):
            score += weight
            signals.append(("pos", word))
    for word, weight in neg_signals.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", tokens):
            score -= weight
            signals.append(("neg", word))

    # 2. Negation detection — flip if preceded by negation
    negation_words = ["not", "no", "never", "neither", "fail", "failed",
                      "unlikely", "unable", "without", "lack", "lacks", "don't", "doesn't", "won't"]
    signal_words = [w for _, w in signals]
    if signal_words:
        for neg in negation_words:
            pattern = rf"\b{neg}\b.{{0,30}}({'|'.join(re.escape(w) for w in signal_words)})"
            if re.search(pattern, tokens):
                score *= -0.7
                break

    # 3. Contrast clause detection — clause after "but/however" carries the point
    contrast_words = ["but", "however", "despite", "although", "nevertheless", "yet", "though", "whereas"]
    for cw in contrast_words:
        if f" {cw} " in f" {tokens} ":
            parts = tokens.split(cw, 1)
            if len(parts) == 2 and len(parts[1]) > 15:
                before = quick_polarity(parts[0])
                after = quick_polarity(parts[1])
                score = (before + after * 2.0) / 3.0

    # 4. Expectation framing
    if re.search(r"better\s+than\s+(expected|feared|forecast|anticipated)", tokens):
        score += 2.5
        signals.append(("pos", "better than expected"))
    if re.search(r"worse\s+than\s+(expected|hoped|forecast|anticipated)", tokens):
        score -= 2.5
        signals.append(("neg", "worse than expected"))
    if re.search(r"(easing|eased|ease)\s+(fears?|concerns?|tensions?|worries)", tokens):
        score += 1.5
        signals.append(("pos", "easing fears"))
    if re.search(r"(escalating|escalated|heightened?)\s+(fears?|concerns?|tensions?|worries)", tokens):
        score -= 1.5
        signals.append(("neg", "escalating tensions"))

    # 5. Modality / uncertainty discount
    uncertain = ["may", "might", "could", "reportedly", "rumored", "considering",
                 "expected to", "likely to", "possibly", "potentially", "alleged"]
    if any(f" {u} " in f" {tokens} " for u in uncertain):
        score *= 0.6

    polarity = max(-1.0, min(1.0, score / 5.0))
    return polarity, signals


def analyze_sentiment_words_only(text, source_id="", matched_keywords=None):
    """
    Sentence-level lexicon engine. Produces structured features, not a final verdict.
    Used for immediate alert posting. AI enrichment happens async.
    """
    matched_keywords = matched_keywords or []
    pos_signals_dict, neg_signals_dict = get_active_signals()

    title = text.split(".")[0] if "." in text[:200] else text[:150]
    sentences = split_sentences(text)

    # Score title with 3x weight
    title_pol, title_sig = score_sentence(title, pos_signals_dict, neg_signals_dict)

    # Score each sentence with position weighting
    scored = []
    for i, sent in enumerate(sentences):
        pol, sigs = score_sentence(sent, pos_signals_dict, neg_signals_dict)
        weight = compute_position_weight(i, len(sentences))
        scored.append({"polarity": pol, "weight": weight, "signals": sigs})

    # Weighted aggregate
    weighted_sum = title_pol * 3.0
    total_weight = 3.0
    for s in scored:
        weighted_sum += s["polarity"] * s["weight"]
        total_weight += s["weight"]

    final_polarity = weighted_sum / max(total_weight, 1)

    # Collect all signals
    all_pos = list(set(w for s in scored for t, w in s["signals"] if t == "pos"))
    all_neg = list(set(w for s in scored for t, w in s["signals"] if t == "neg"))
    all_pos += [w for t, w in title_sig if t == "pos"]
    all_neg += [w for t, w in title_sig if t == "neg"]

    # Mixed signal detection
    has_pos = any(s["polarity"] > 0.3 for s in scored) or title_pol > 0.3
    has_neg = any(s["polarity"] < -0.3 for s in scored) or title_pol < -0.3
    mixed = has_pos and has_neg

    # Confidence: agreement + signal density
    if scored:
        polarities = [s["polarity"] for s in scored if abs(s["polarity"]) > 0.05]
        if polarities:
            pos_count = sum(1 for p in polarities if p > 0)
            neg_count = sum(1 for p in polarities if p < 0)
            agreement = max(pos_count, neg_count) / max(pos_count + neg_count, 1)
        else:
            agreement = 0.5
        total_signals = len(all_pos) + len(all_neg)
        density = min(1.0, total_signals / 10)
        confidence = agreement * 0.6 + density * 0.4
    else:
        confidence = 0.15

    # Polarity to tone
    if final_polarity > 0.15:
        tone = "positive"
    elif final_polarity < -0.15:
        tone = "negative"
    else:
        tone = "neutral"

    # Severity from polarity magnitude
    abs_pol = abs(final_polarity)
    severity = "high" if abs_pol > 0.6 else "medium" if abs_pol > 0.25 else "low"

    # Source reliability affects CONFIDENCE not polarity
    source_reliability = SOURCE_WEIGHTS.get(source_id, 0.5)
    confidence *= (0.7 + source_reliability * 0.3)

    return {
        "article_tone": tone,
        "market_impact": {"positive": "bullish", "negative": "bearish", "neutral": "neutral"}[tone],
        "polarity": round(final_polarity, 3),
        "confidence": round(min(1.0, confidence), 2),
        "severity": severity,
        "mixed_signals": mixed,
        "positive_signals": list(set(all_pos))[:8],
        "negative_signals": list(set(all_neg))[:8],
        "source_reliability": source_reliability,
    }


def cleaned_entry_text(*parts):
    return normalize_whitespace(BeautifulSoup(" ".join([p for p in parts if p]), "html.parser").get_text(" "))


# ─── Article Freshness Checking ───────────────────────────────────────────────

# All internal timestamps use UTC; display formatting is done at render time


def now_utc():
    """Current time in UTC."""
    return datetime.now(timezone.utc)


def parse_entry_date(entry):
    """
    Extract a datetime from an RSS/Atom feed entry.
    Tries multiple fields and formats. Returns a timezone-aware datetime or None.
    """
    # feedparser provides parsed time tuples in several fields
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(field)
        if t:
            try:
                ts = calendar.timegm(t)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                continue

    # Try raw string fields
    for field in ("published", "updated", "created", "dc_date"):
        raw = entry.get(field, "")
        if not raw:
            continue
        # Try RFC 2822 (common in RSS)
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass
        # Try ISO 8601
        try:
            # Handle Z suffix
            raw_clean = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    return None


def parse_iso_date(iso_str):
    """Parse an ISO date string (e.g. from NewsAPI publishedAt). Returns aware datetime or None."""
    if not iso_str:
        return None
    try:
        raw = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_fresh(dt, max_age_hours=4):
    """
    Check if a datetime is within max_age_hours of now.
    Rejects articles with no parseable date (previously allowed them through).
    Also rejects articles with future timestamps (bad data) or dates > 48h old.
    """
    if dt is None:
        return False  # No timestamp = don't trust it
    now = datetime.now(timezone.utc)
    age = now - dt
    # Reject future dates (more than 5 min ahead = bad timestamp)
    if age < timedelta(minutes=-5):
        return False
    # Reject anything older than max_age_hours
    if age > timedelta(hours=max_age_hours):
        return False
    return True


# Global max age — reloaded from config each cycle
_max_article_age_hours = 4


def poll_rss(keywords):
    """Poll all configured RSS feeds for keyword matches."""
    for source_id, source in RSS_SOURCES.items():
        for feed_url in source["feeds"]:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:
                    # Check freshness
                    entry_date = parse_entry_date(entry)
                    if not is_fresh(entry_date, _max_article_age_hours):
                        continue

                    title = normalize_whitespace(entry.get("title", ""))
                    summary = entry.get("summary", entry.get("description", ""))
                    link = normalize_whitespace(entry.get("link", ""))
                    if not title or not is_valid_source_url(link):
                        continue
                    full_text = cleaned_entry_text(title, summary)
                    matches = match_keywords(full_text, keywords)
                    for kw, sev in matches:
                        snippet = extract_snippet(full_text, kw)
                        store.add(
                            source_id=source_id,
                            source_name=source["name"],
                            title=title,
                            url=link,
                            snippet=snippet,
                            keyword=kw,
                            severity=sev,
                            published_at=entry_date,
                        )
            except Exception as e:
                log.warning(f"RSS error [{source_id}] {feed_url}: {e}")


def poll_newsapi(keywords, api_key):
    """Poll NewsAPI for keyword matches (requires free API key from newsapi.org).
    Config option newsapi_mode: 'trump_only' (legacy) or 'all_news' (default)."""
    if not api_key:
        return
    try:
        config = load_config()
        newsapi_mode = config.get("newsapi_mode", "all_news")
        priority_kw = keywords[:10]
        for kw in priority_kw:
            url = "https://newsapi.org/v2/everything"
            if newsapi_mode == "trump_only":
                q_param = f'Trump AND "{kw}"'
            else:
                q_param = f'"{kw}"'
            params = {
                "q": q_param,
                "sortBy": "publishedAt",
                "pageSize": 10,
                "apiKey": api_key,
                "language": "en",
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                log.warning(f"NewsAPI error: {resp.status_code}")
                continue
            data = resp.json()
            for article in data.get("articles", []):
                # Check freshness
                pub_date = parse_iso_date(article.get("publishedAt"))
                if not is_fresh(pub_date, _max_article_age_hours):
                    continue

                title = normalize_whitespace(article.get("title", ""))
                desc = normalize_whitespace(article.get("description", "") or "")
                source_name = article.get("source", {}).get("name", "NewsAPI")
                link = normalize_whitespace(article.get("url", ""))
                if not title or not is_valid_source_url(link):
                    continue
                full_text = cleaned_entry_text(title, desc)
                matches = match_keywords(full_text, keywords)
                for matched_kw, sev in matches:
                    snippet = extract_snippet(full_text, matched_kw)
                    store.add(
                        source_id="newsapi",
                        source_name=source_name,
                        title=title,
                        url=link,
                        snippet=snippet,
                        keyword=matched_kw,
                        severity=sev,
                        published_at=pub_date,
                    )
            time.sleep(1)
    except Exception as e:
        log.warning(f"NewsAPI error: {e}")


def poll_whitehouse(keywords):
    """Scrape White House presidential actions page for new items.
    Prefers article containers, filters nav links, checks freshness."""
    try:
        resp = requests.get(WHITEHOUSE_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KeywordMonitor/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")

        # Skip common navigation/footer/header patterns
        nav_patterns = ["menu", "nav", "footer", "header", "sidebar", "breadcrumb"]

        # Try structured article containers first
        articles = soup.find_all("article") or soup.find_all("div", class_=re.compile(r"post|entry|item|news"))
        search_tags = articles if articles else soup.find_all("a", href=True)

        for tag in search_tags:
            # If we found <article> elements, get the link inside
            if tag.name != "a":
                link_tag = tag.find("a", href=True)
                if not link_tag:
                    continue
            else:
                link_tag = tag
                # Skip links inside nav/footer/header elements
                parent_classes = " ".join(p.get("class", []) for p in link_tag.parents if p.get("class"))
                parent_ids = " ".join(p.get("id", "") for p in link_tag.parents if p.get("id"))
                parent_str = (parent_classes + " " + parent_ids).lower()
                if any(n in parent_str for n in nav_patterns):
                    continue

            text = normalize_whitespace(link_tag.get_text(" ", strip=True))
            href = normalize_whitespace(link_tag["href"])
            if not text or len(text) < 20 or len(text) > 300:
                continue
            if not href.startswith("http"):
                href = f"https://www.whitehouse.gov{href}"
            if not is_valid_source_url(href):
                continue
            # Skip non-content URLs
            if any(x in href.lower() for x in ["/search", "/contact", "/privacy", "/accessibility", "#", "javascript:"]):
                continue
            matches = match_keywords(text, keywords)
            for kw, sev in matches:
                store.add(
                    source_id="whitehouse",
                    source_name="White House",
                    title=text,
                    url=href,
                    snippet=extract_snippet(text, kw),
                    keyword=kw,
                    severity="high",
                )
    except Exception as e:
        log.warning(f"White House scrape error: {e}")


# ─── SEC EDGAR Filing Monitor ────────────────────────────────────────────────
# Uses EDGAR's full-text search ATOM feed — no library needed, fits existing feedparser model.
# Monitors watchlist tickers for new 8-K, 10-K, 10-Q filings.

SEC_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
SEC_FILING_TYPES = {"8-K", "10-K", "10-Q", "8-K/A", "10-K/A", "10-Q/A", "S-1", "4"}
_sec_seen_urls = set()


def poll_sec_edgar(keywords):
    """Poll SEC EDGAR for new filings from watchlist tickers and keyword-matched companies."""
    global _sec_seen_urls
    wl = load_watchlist()
    # Only check stock tickers (not crypto)
    tickers = [item["symbol"] for item in wl if item.get("type", "stock") == "stock"]
    if not tickers:
        return

    for ticker in tickers[:10]:  # Cap to avoid rate limiting
        try:
            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q": f'"{ticker}"',
                    "dateRange": "custom",
                    "startdt": (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d"),
                    "enddt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "forms": "8-K,10-K,10-Q",
                },
                headers={"User-Agent": "SIGINT Monitor matthew@sigint.local", "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])

            for hit in hits[:5]:
                source = hit.get("_source", {})
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{source.get('file_num', '')}"
                form_type = source.get("form_type", "")
                entity = source.get("entity_name", "")
                filed_date = source.get("file_date", "")
                title = f"SEC {form_type}: {entity} ({ticker})"

                # Dedup
                dedup_key = f"{ticker}:{form_type}:{filed_date}"
                if dedup_key in _sec_seen_urls:
                    continue
                _sec_seen_urls.add(dedup_key)

                # Parse date
                pub_dt = None
                if filed_date:
                    try:
                        pub_dt = datetime.strptime(filed_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                # Determine severity based on filing type
                severity = "high" if form_type in ("8-K", "8-K/A") else "medium"

                # Match against keywords
                combined = f"{entity} {ticker} {form_type}".lower()
                matched_kw = ""
                for kw in keywords:
                    if kw.lower() in combined:
                        matched_kw = kw
                        break
                if not matched_kw:
                    matched_kw = ticker.lower()

                snippet = f"{form_type} filed by {entity} on {filed_date}"

                store.add(
                    source_id="sec_edgar",
                    source_name="SEC EDGAR",
                    title=title,
                    url=filing_url,
                    snippet=snippet,
                    keyword=matched_kw,
                    severity=severity,
                    published_at=pub_dt,
                )

        except Exception as e:
            log.debug(f"SEC EDGAR poll error [{ticker}]: {e}")

    # Trim seen set to prevent memory growth
    if len(_sec_seen_urls) > 500:
        _sec_seen_urls = set(list(_sec_seen_urls)[-200:])


def poll_truthsocial(keywords, accounts):
    """
    Poll Truth Social for each monitored account via RSSHub proxy.
    accounts: list of {"username": "...", "label": "..."} dicts
    """
    for acct in accounts:
        username = acct.get("username", "")
        label = acct.get("label", "") or f"@{username}"
        if not username:
            continue
        try:
            feed_url = f"https://rsshub.app/truthsocial/user/{username}"
            profile_url = f"https://truthsocial.com/@{username}"
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                # Check freshness
                entry_date = parse_entry_date(entry)
                if not is_fresh(entry_date, _max_article_age_hours):
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = normalize_whitespace(entry.get("link", profile_url))
                if not is_valid_source_url(link):
                    link = profile_url
                clean = cleaned_entry_text(title, summary)
                if not clean:
                    continue
                matches = match_keywords(clean, keywords)
                for kw, sev in matches:
                    snippet = extract_snippet(clean, kw)
                    store.add(
                        source_id="truthsocial",
                        source_name=f"Truth Social – {label}",
                        title=clean[:150],
                        url=link,
                        snippet=snippet,
                        keyword=kw,
                        severity="high",
                    )
        except Exception as e:
            log.warning(f"Truth Social error [@{username}]: {e}")


def poll_twitter(keywords, bearer_token, accounts):
    """
    Poll X/Twitter for keyword mentions from each monitored account.
    Strategy:
      1. If bearer_token is set, try the user timeline API (requires Basic $100/mo tier).
      2. Always try RSSHub RSS proxy as fallback (free, no key needed).
    """
    for acct in accounts:
        username = acct.get("username", "")
        label = acct.get("label", "") or f"@{username}"
        if not username:
            continue

        fetched_via_api = False

        # ── Attempt 1: Twitter API v2 user timeline ──
        if bearer_token:
            try:
                headers = {"Authorization": f"Bearer {bearer_token}"}
                # First resolve username → user ID
                user_resp = requests.get(
                    f"https://api.twitter.com/2/users/by/username/{username}",
                    headers=headers, timeout=15,
                )
                if user_resp.status_code == 200:
                    user_id = user_resp.json().get("data", {}).get("id")
                    if user_id:
                        timeline_resp = requests.get(
                            f"https://api.twitter.com/2/users/{user_id}/tweets",
                            headers=headers, timeout=15,
                            params={"max_results": 20, "tweet.fields": "created_at,text"},
                        )
                        if timeline_resp.status_code == 200:
                            fetched_via_api = True
                            for tweet in timeline_resp.json().get("data", []):
                                text = normalize_whitespace(tweet.get("text", ""))
                                tweet_id = tweet.get("id", "")
                                if not text or not tweet_id:
                                    continue
                                link = f"https://x.com/{username}/status/{tweet_id}"
                                matches = match_keywords(text, keywords)
                                for matched_kw, sev in matches:
                                    snippet = extract_snippet(text, matched_kw)
                                    store.add(
                                        source_id="x_twitter",
                                        source_name=f"X – @{username}",
                                        title=text[:150],
                                        url=link,
                                        snippet=snippet,
                                        keyword=matched_kw,
                                        severity="high",
                                    )
                        else:
                            log.warning(f"Twitter timeline API {timeline_resp.status_code} for @{username} — "
                                        f"free tier doesn't include this endpoint, falling back to RSS")
                elif user_resp.status_code == 403:
                    log.warning(f"Twitter API 403 for @{username} — free tier is limited, using RSS fallback")
                time.sleep(1)
            except Exception as e:
                log.warning(f"Twitter API error [@{username}]: {e}")

        # ── Attempt 2: RSSHub proxy (always works, no key needed) ──
        if not fetched_via_api:
            try:
                feed_url = f"https://rsshub.app/twitter/user/{username}"
                feed = feedparser.parse(feed_url)
                if feed.bozo and not feed.entries:
                    # Try alternative Nitter-based route
                    feed = feedparser.parse(f"https://rsshub.app/twitter/tweets/{username}")
                for entry in feed.entries[:20]:
                    # Check freshness
                    entry_date = parse_entry_date(entry)
                    if not is_fresh(entry_date, _max_article_age_hours):
                        continue

                    title = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))
                    link = normalize_whitespace(entry.get("link", f"https://x.com/{username}"))
                    if not is_valid_source_url(link):
                        link = f"https://x.com/{username}"
                    clean = cleaned_entry_text(title, summary)
                    if not clean:
                        continue
                    matches = match_keywords(clean, keywords)
                    for kw, sev in matches:
                        snippet = extract_snippet(clean, kw)
                        store.add(
                            source_id="x_twitter",
                            source_name=f"X – @{username}",
                            title=clean[:150],
                            url=link,
                            snippet=snippet,
                            keyword=kw,
                            severity="high",
                        )
                if feed.entries:
                    log.info(f"X/@{username}: fetched {len(feed.entries)} posts via RSS")
            except Exception as e:
                log.warning(f"X RSS fallback error [@{username}]: {e}")


_refresh_requested = threading.Event()


def monitor_loop():
    """Main polling loop — runs in a background thread."""
    global _max_article_age_hours
    while True:
        try:
            config = load_config()
            keywords = [normalize_whitespace(k).lower() for k in config.get("keywords", []) if normalize_whitespace(k)]
            interval = config.get("poll_interval_seconds", 120)
            _max_article_age_hours = config.get("max_article_age_hours", 4)

            # Ollama config — both lanes
            analysis_url, analysis_model = get_ollama_lane(config, "analysis")
            chat_url, chat_model = get_ollama_lane(config, "chat")
            store.ollama_url = analysis_url
            store.ollama_model = analysis_model
            # Chat lane config for worker 2 (uses chat model for analysis when idle)
            store.ollama_chat_url = chat_url
            store.ollama_chat_model = chat_model
            # Log both lanes for observability
            ollama_status = f"Analysis: {analysis_model}@{analysis_url}" if analysis_url else "Analysis: off"
            if chat_url != analysis_url or chat_model != analysis_model:
                ollama_status += f" | Chat: {chat_model}@{chat_url}"

            # Split accounts by platform
            all_accounts = config.get("monitored_accounts", [])
            x_accounts = [a for a in all_accounts if a.get("platform") == "x"]
            ts_accounts = [a for a in all_accounts if a.get("platform") == "truthsocial"]

            log.info(f"Monitor cycle — {len(keywords)} kw, age≤{_max_article_age_hours}h, {len(x_accounts)} X, {len(ts_accounts)} TS | {ollama_status}")
            log.info("── Polling cycle start ──")

            if config.get("rss_enabled", True):
                poll_rss(keywords)

            if config.get("newsapi_key"):
                poll_newsapi(keywords, config["newsapi_key"])

            if config.get("whitehouse_enabled", True):
                poll_whitehouse(keywords)

            # SEC filings are shown in Markets tab via /api/sec/recent, not as alerts

            if config.get("truthsocial_enabled", True) and ts_accounts:
                poll_truthsocial(keywords, ts_accounts)

            if x_accounts:
                poll_twitter(keywords, config.get("twitter_bearer_token", ""), x_accounts)

            log.info(f"── Cycle complete — {len(store.alerts)} total alerts ──")

            # Sleep in small increments so manual refresh can interrupt
            for _ in range(interval):
                if _refresh_requested.is_set():
                    _refresh_requested.clear()
                    log.info("Manual refresh requested — starting new cycle")
                    break
                time.sleep(1)

        except Exception as e:
            log.error(f"Monitor loop error: {e}")
            time.sleep(30)


app = Flask(__name__, static_folder="static")
CORS(app)


@app.route("/")
def index():
    """Serve the frontend if static/index.html exists."""
    static_index = Path(app.static_folder or "static") / "index.html"
    if static_index.exists():
        return app.send_static_file("index.html")
    return "<h3>SIGINT Monitor API is running.</h3><p>Place your frontend build in ./static/index.html</p>", 200


@app.route("/api/alerts")
def get_alerts():
    since = request.args.get("since")
    limit = request.args.get("limit", type=int)
    return jsonify({"alerts": store.get_all(since=since, limit=limit)})


@app.route("/api/alerts", methods=["DELETE"])
def clear_alerts():
    store.clear()
    return jsonify({"status": "cleared"})


@app.route("/api/keywords")
def get_keywords():
    config = load_config()
    return jsonify({"keywords": config["keywords"]})


@app.route("/api/keywords", methods=["POST"])
def update_keywords():
    data = request.get_json(silent=True) or {}
    config = load_config()
    current = [normalize_whitespace(k).lower() for k in config.get("keywords", []) if normalize_whitespace(k)]

    if isinstance(data.get("keywords"), list):
        current = [normalize_whitespace(k).lower() for k in data["keywords"] if normalize_whitespace(k)]
    elif data.get("add"):
        new_kw = normalize_whitespace(str(data["add"]).lower())
        if new_kw and new_kw not in current:
            current.append(new_kw)
    elif data.get("remove"):
        remove_kw = normalize_whitespace(str(data["remove"]).lower())
        current = [k for k in current if k != remove_kw]

    config["keywords"] = current
    save_config(config)
    return jsonify({"status": "updated", "keywords": config["keywords"]})


@app.route("/api/signal-sets")
def get_signal_sets():
    return jsonify({"sets": load_signal_sets()})


@app.route("/api/signal-sets", methods=["POST"])
def update_signal_sets():
    """
    Update signal sets. Supports:
      - {"toggle": "crypto"}  — enable/disable a set
      - {"set_id": "crypto", "sentiment": "positive", "add": "moon", "weight": 3}
      - {"set_id": "crypto", "sentiment": "negative", "remove": "crash"}
      - {"sets": {...}}  — replace all sets
    """
    data = request.get_json(silent=True) or {}
    sets = load_signal_sets()

    if data.get("toggle"):
        cat_id = data["toggle"]
        if cat_id in sets:
            sets[cat_id]["enabled"] = not sets[cat_id].get("enabled", True)
            save_signal_sets(sets)
            return jsonify({"status": "toggled", "sets": sets})
        return jsonify({"error": f"Set '{cat_id}' not found"}), 404

    elif data.get("set_id") and data.get("sentiment"):
        cat_id = data["set_id"]
        sent = data["sentiment"]  # "positive" or "negative"
        if cat_id not in sets:
            return jsonify({"error": f"Set '{cat_id}' not found"}), 404
        if sent not in ("positive", "negative"):
            return jsonify({"error": "sentiment must be 'positive' or 'negative'"}), 400

        if data.get("add"):
            word = normalize_whitespace(data["add"]).lower()
            weight = float(data.get("weight", 2))
            sets[cat_id][sent][word] = weight
        elif data.get("remove"):
            word = normalize_whitespace(data["remove"]).lower()
            sets[cat_id][sent].pop(word, None)

        save_signal_sets(sets)
        return jsonify({"status": "updated", "sets": sets})

    elif isinstance(data.get("sets"), dict):
        save_signal_sets(data["sets"])
        return jsonify({"status": "replaced", "sets": data["sets"]})

    return jsonify({"error": "Invalid request"}), 400


@app.route("/api/accounts")
def get_accounts():
    config = load_config()
    return jsonify({"accounts": config.get("monitored_accounts", [])})


@app.route("/api/accounts", methods=["POST"])
def update_accounts():
    """
    Add or remove a monitored account.
    To add:    POST {"url": "https://x.com/elonmusk"} or {"url": "https://truthsocial.com/@realDonaldTrump"}
    To remove: POST {"remove": "x:elonmusk"} (platform:username)
    """
    data = request.get_json(silent=True) or {}
    config = load_config()
    accounts = config.get("monitored_accounts", [])

    if data.get("url"):
        platform, username = parse_account_url(data["url"])
        if not platform:
            return jsonify({"error": "Could not parse URL. Supported: https://x.com/username or https://truthsocial.com/@username"}), 400
        # Check for duplicates
        for a in accounts:
            if a.get("platform") == platform and a.get("username", "").lower() == username.lower():
                return jsonify({"status": "exists", "accounts": accounts})
        label = data.get("label", "") or f"@{username}"
        accounts.append({"platform": platform, "username": username, "label": label})
        config["monitored_accounts"] = accounts
        save_config(config)
        log.info(f"Account added: {platform}/@{username}")
        return jsonify({"status": "added", "accounts": accounts})

    elif data.get("remove"):
        # Format: "platform:username"
        parts = str(data["remove"]).split(":", 1)
        if len(parts) != 2:
            return jsonify({"error": "Use format 'platform:username'"}), 400
        rm_platform, rm_user = parts[0].strip(), parts[1].strip().lower()
        accounts = [a for a in accounts if not (a.get("platform") == rm_platform and a.get("username", "").lower() == rm_user)]
        config["monitored_accounts"] = accounts
        save_config(config)
        log.info(f"Account removed: {rm_platform}/@{rm_user}")
        return jsonify({"status": "removed", "accounts": accounts})

    return jsonify({"error": "Provide 'url' to add or 'remove' to delete"}), 400


@app.route("/api/refresh", methods=["POST"])
def trigger_refresh():
    """Trigger an immediate poll cycle."""
    _refresh_requested.set()
    return jsonify({"status": "refresh_triggered"})


@app.route("/api/status")
def status():
    return jsonify({
        "status": "running",
        "alert_count": len(store.alerts),
        "sources": list(RSS_SOURCES.keys()) + ["whitehouse", "truthsocial", "newsapi", "x_twitter"],
    })


@app.route("/metrics")
def prometheus_metrics():
    high = sum(1 for a in store.alerts if a["severity"] == "high")
    med = sum(1 for a in store.alerts if a["severity"] == "medium")
    low = sum(1 for a in store.alerts if a["severity"] == "low")
    metrics = (
        f"# HELP sigint_alerts_total Total keyword alerts\n"
        f"# TYPE sigint_alerts_total gauge\n"
        f"sigint_alerts_total {len(store.alerts)}\n"
        f"# HELP sigint_alerts_high High severity alerts\n"
        f"# TYPE sigint_alerts_high gauge\n"
        f"sigint_alerts_high {high}\n"
        f"# HELP sigint_alerts_medium Medium severity alerts\n"
        f"# TYPE sigint_alerts_medium gauge\n"
        f"sigint_alerts_medium {med}\n"
        f"# HELP sigint_alerts_low Low severity alerts\n"
        f"# TYPE sigint_alerts_low gauge\n"
        f"sigint_alerts_low {low}\n"
    )
    return metrics, 200, {"Content-Type": "text/plain"}


# ─── CSV Import for Signal Sets ───────────────────────────────────────────────

@app.route("/api/signal-sets/import", methods=["POST"])
def import_signal_csv():
    """
    Import signal words from CSV. Expects multipart form with:
      - file: CSV file with columns: word, sentiment (positive/negative), weight (optional)
      - set_id: which set to add to (form field)
      - new_set_label: if set_id is "__new__", create a new set with this label
    CSV format: word,sentiment,weight
    Example:
      moon,positive,3
      rug pull,negative,4
    """
    import csv, io
    f = request.files.get("file")
    set_id = request.form.get("set_id", "custom")
    new_set_label = request.form.get("new_set_label", "")

    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    sets = load_signal_sets()

    # Create new set if requested
    if set_id == "__new__" and new_set_label:
        new_id = re.sub(r"[^a-z0-9_]", "_", new_set_label.lower().strip())
        if new_id not in sets:
            sets[new_id] = {"label": new_set_label.strip(), "enabled": True, "positive": {}, "negative": {}}
        set_id = new_id

    if set_id not in sets:
        return jsonify({"error": f"Set '{set_id}' not found"}), 404

    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(content))
        added = 0
        for row in reader:
            if len(row) < 2:
                continue
            word = normalize_whitespace(row[0]).lower()
            sentiment = row[1].strip().lower()
            weight = float(row[2]) if len(row) > 2 and row[2].strip() else 2.0
            if not word or sentiment not in ("positive", "negative"):
                continue
            sets[set_id][sentiment][word] = weight
            added += 1
        save_signal_sets(sets)
        return jsonify({"status": "imported", "added": added, "sets": sets})
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {str(e)}"}), 400


# ─── Ollama Chat (Follow-up Questions) ────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def ollama_chat():
    """
    Ask a follow-up question about an article using Ollama (CHAT lane).
    """
    global _chat_active
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    alert_id = data.get("alert_id")
    history = data.get("history", [])

    config = load_config()
    chat_url, chat_model = get_ollama_lane(config, "chat")

    if not chat_url:
        return jsonify({"error": "Ollama not configured. Add ollama_url to config.json"}), 400
    if not question:
        return jsonify({"error": "No question provided"}), 400

    # Find the alert and its context
    context = ""
    with store.lock:
        for a in store.alerts:
            if a["id"] == alert_id:
                context = f"Title: {a['title']}\nSource: {a['source_name']}\nSummary: {a.get('summary', '')}\nSnippet: {a.get('snippet', '')}"
                break

    if not context:
        context = "No specific article context available."

    # Reduced history from 10 to 5
    conv_text = ""
    for msg in history[-5:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        conv_text += f"\n{role}: {msg.get('text', '')}"

    # Signal that chat is active so background enrichment yields
    with _chat_active_lock:
        _chat_active += 1

    try:
        prompt = (
            f"You are a financial market analyst. Based on the following article, answer the user's question concisely.\n\n"
            f"Article Context:\n{context}\n"
        )
        if conv_text:
            prompt += f"\nPrevious conversation:{conv_text}\n"
        prompt += f"\nUser: {question}\n\nAssistant:"

        resp = requests.post(
            f"{chat_url.rstrip('/')}/api/generate",
            json={
                "model": chat_model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": OLLAMA_CHAT_NUM_PREDICT,
                    "temperature": OLLAMA_CHAT_TEMPERATURE,
                },
            },
            timeout=OLLAMA_CHAT_TIMEOUT,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            article_title = ""
            article_url = ""
            source_id_chat = ""
            keyword_chat = ""
            with store.lock:
                for a in store.alerts:
                    if a["id"] == alert_id:
                        article_title = a.get("title", "")
                        article_url = a.get("url", "")
                        source_id_chat = a.get("source", "")
                        keyword_chat = a.get("keyword", "")
                        break
            log_chat(alert_id, article_title, question, answer,
                     article_url=article_url, source_id=source_id_chat, keyword=keyword_chat)
            return jsonify({"answer": answer})
        return jsonify({"error": f"Ollama returned {resp.status_code}"}), 500
    except requests.exceptions.Timeout:
        log_system_event("ollama_timeout", "Per-article chat timeout",
                         f"model={chat_model} timeout={OLLAMA_CHAT_TIMEOUT}s")
        return jsonify({"error": "Ollama took too long to respond. Try a shorter question or wait for background analysis to finish."}), 504
    except Exception as e:
        return jsonify({"error": f"Ollama error: {str(e)}"}), 500
    finally:
        with _chat_active_lock:
            _chat_active = max(0, _chat_active - 1)


@app.route("/api/chat/general", methods=["POST"])
def general_chat():
    """
    General-purpose AI chat using stored articles as context (CHAT lane).
    Prompts are kept lightweight for fast interactive response.
    """
    global _chat_active
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    history = data.get("history", [])
    current_tab = data.get("tab", "")

    config = load_config()
    chat_url, chat_model = get_ollama_lane(config, "chat")

    if not chat_url:
        return jsonify({"error": "Ollama not configured. Add ollama_url to config.json"}), 400
    if not question:
        return jsonify({"error": "No question provided"}), 400

    # ─── Build lightweight context ───
    context_parts = []

    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        q_lower = question.lower()
        search_words = [w for w in q_lower.split() if len(w) > 3 and w not in
                        ("what", "when", "where", "which", "about", "could", "would",
                         "should", "think", "your", "that", "this", "have", "from",
                         "they", "been", "with", "will", "does", "going")]

        safe_cols = "title, summary, sentiment, source_name, published_at, keyword"
        try:
            c.execute("SELECT market_impact FROM articles LIMIT 1")
            safe_cols += ", market_impact"
        except Exception:
            pass
        try:
            c.execute("SELECT user_corrected_impact FROM articles LIMIT 1")
            safe_cols += ", user_corrected_impact"
        except Exception:
            pass

        # Matched articles — reduced from 8 to 4
        matched_articles = []
        if search_words:
            where_clauses = []
            params = []
            for w in search_words[:4]:
                where_clauses.append("(LOWER(title) LIKE ? OR keyword LIKE ?)")
                params.extend([f"%{w}%", f"%{w}%"])
            query = f"SELECT {safe_cols} FROM articles WHERE {' OR '.join(where_clauses)} ORDER BY id DESC LIMIT 4"
            c.execute(query, params)
            matched_articles = [dict(r) for r in c.fetchall()]

        # Recent articles — reduced from 5 to 3
        c.execute(f"SELECT {safe_cols} FROM articles ORDER BY id DESC LIMIT 3")
        recent_articles = [dict(r) for r in c.fetchall()]

        # 24h mood — keep lightweight
        c.execute("SELECT sentiment, COUNT(*) FROM articles WHERE timestamp > datetime('now', '-24 hours') GROUP BY sentiment")
        sentiment_24h = dict(c.fetchall())

        conn.close()
    except Exception as e:
        log.debug(f"General chat context error: {e}")
        matched_articles = []
        recent_articles = []
        sentiment_24h = {}

    if matched_articles:
        context_parts.append("RELEVANT ARTICLES:")
        for a in matched_articles:
            impact = a.get("user_corrected_impact", "") or a.get("market_impact", "")
            # Trim summaries aggressively
            summary = a.get("summary", "")[:100]
            context_parts.append(f"- [{impact}] {a.get('title', '')} ({a.get('source_name', '')})")
            if summary:
                context_parts.append(f"  {summary}")

    if recent_articles:
        context_parts.append("\nLATEST:")
        for a in recent_articles:
            impact = a.get("user_corrected_impact", "") or a.get("market_impact", "")
            context_parts.append(f"- [{impact}] {a.get('title', '')} ({a.get('source_name', '')})")

    if sentiment_24h:
        pos = sentiment_24h.get("positive", 0)
        neg = sentiment_24h.get("negative", 0)
        neu = sentiment_24h.get("neutral", 0)
        context_parts.append(f"\n24h mood: {pos}+ {neg}- {neu}= articles")

    # Live alerts — reduced from 5 to 3
    with store.lock:
        recent_alerts = store.alerts[:3]
    if recent_alerts:
        context_parts.append("\nLIVE:")
        for a in recent_alerts:
            context_parts.append(f"- [{a.get('market_impact', 'neutral')}] {a.get('title', '')} (via {a.get('source_name', '')})")

    context_str = "\n".join(context_parts) if context_parts else "No article data available yet."

    # Add Wikipedia context for key terms in the question (supplementary)
    wiki_ctx = build_wiki_context(question, keyword="")
    if wiki_ctx:
        context_str += "\n" + wiki_ctx

    # Conversation history — reduced from 10 to 5
    conv_text = ""
    for msg in history[-5:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        conv_text += f"\n{role}: {msg.get('text', '')}"

    prompt = (
        "You are a financial market analyst assistant with real-time news data. "
        "Be concise and specific. Reference articles when relevant.\n\n"
        f"DATA:\n{context_str}\n"
    )
    if conv_text:
        prompt += f"\nHISTORY:{conv_text}\n"
    prompt += f"\nUser: {question}\n\nAssistant:"

    # Signal chat is active
    with _chat_active_lock:
        _chat_active += 1

    try:
        resp = requests.post(
            f"{chat_url.rstrip('/')}/api/generate",
            json={
                "model": chat_model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": OLLAMA_CHAT_NUM_PREDICT,
                    "temperature": OLLAMA_CHAT_TEMPERATURE,
                },
            },
            timeout=OLLAMA_CHAT_TIMEOUT,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            log_chat(0, "general_chat", question, answer)
            return jsonify({
                "answer": answer,
                "context_articles": len(matched_articles),
                "recent_articles": len(recent_articles),
            })
        return jsonify({"error": f"Ollama returned {resp.status_code}"}), 500
    except requests.exceptions.Timeout:
        log_system_event("ollama_timeout", "General chat timeout",
                         f"model={chat_model} timeout={OLLAMA_CHAT_TIMEOUT}s prompt_len={len(prompt)}")
        return jsonify({"error": "Ollama took too long to respond. Try a shorter question or wait for background analysis to finish."}), 504
    except Exception as e:
        return jsonify({"error": f"Ollama error: {str(e)}"}), 500
    finally:
        with _chat_active_lock:
            _chat_active = max(0, _chat_active - 1)


# ─── Market Data API ──────────────────────────────────────────────────────────

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

# yfinance — optional but recommended: pip install yfinance
try:
    import yfinance as yf
    _HAS_YFINANCE = True
    log.info("yfinance available — using for market data")
except ImportError:
    _HAS_YFINANCE = False
    log.info("yfinance not installed — using raw Yahoo API (pip install yfinance for better reliability)")

# Stored watchlist — items are {"symbol": "NVDA", "type": "stock"} or {"symbol": "bitcoin", "type": "crypto"}
WATCHLIST_PATH = Path("watchlist.json")
DEFAULT_WATCHLIST = [{"symbol": "NVDA", "type": "stock"}]
DEFAULT_INDICES = ["^GSPC", "^IXIC", "^DJI", "SPY"]

# All available indices the user can choose from
AVAILABLE_INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ",
    "^DJI": "Dow Jones",
    "SPY": "SPY ETF",
    "^RUT": "Russell 2000",
    "^VIX": "VIX (Volatility)",
    "^TNX": "10-Year Treasury",
    "^TYX": "30-Year Treasury",
    "GC=F": "Gold Futures",
    "SI=F": "Silver Futures",
    "CL=F": "Crude Oil WTI",
    "NG=F": "Natural Gas",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "^FTSE": "FTSE 100",
    "^N225": "Nikkei 225",
    "^HSI": "Hang Seng",
    "DX-Y.NYB": "US Dollar Index",
}

INDICES_CONFIG_PATH = Path("indices_config.json")


def load_indices_config():
    if not INDICES_CONFIG_PATH.exists():
        INDICES_CONFIG_PATH.write_text(json.dumps(DEFAULT_INDICES))
    try:
        return json.loads(INDICES_CONFIG_PATH.read_text())
    except Exception:
        return DEFAULT_INDICES


def save_indices_config(indices):
    INDICES_CONFIG_PATH.write_text(json.dumps(indices))

# Common crypto ID mapping (coingecko uses slugs)
CRYPTO_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "DOT": "polkadot", "AVAX": "avalanche-2",
    "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
    "LTC": "litecoin", "SHIB": "shiba-inu", "ARB": "arbitrum", "OP": "optimism",
    "APT": "aptos", "SUI": "sui", "NEAR": "near", "FIL": "filecoin",
    "PEPE": "pepe", "BONK": "bonk", "WIF": "dogwifcoin",
}

# Market news source config — separate from alert sources
MARKET_NEWS_PATH = Path("market_news_config.json")
DEFAULT_MARKET_NEWS_SOURCES = {
    "reuters_markets": {"name": "Reuters Markets", "enabled": True, "feed": "https://feeds.reuters.com/reuters/businessNews"},
    "cnbc_markets": {"name": "CNBC Markets", "enabled": True, "feed": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147"},
    "bloomberg_markets": {"name": "Bloomberg Markets", "enabled": True, "feed": "https://feeds.bloomberg.com/markets/news.rss"},
    "marketwatch": {"name": "MarketWatch", "enabled": True, "feed": "https://feeds.marketwatch.com/marketwatch/topstories"},
    "yahoo_finance": {"name": "Yahoo Finance", "enabled": True, "feed": "https://finance.yahoo.com/news/rssindex"},
    "coindesk": {"name": "CoinDesk", "enabled": False, "feed": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    "cointelegraph": {"name": "CoinTelegraph", "enabled": False, "feed": "https://cointelegraph.com/rss"},
}


def load_watchlist():
    if not WATCHLIST_PATH.exists():
        WATCHLIST_PATH.write_text(json.dumps(DEFAULT_WATCHLIST))
    try:
        wl = json.loads(WATCHLIST_PATH.read_text())
        # Migrate old format (list of strings) to new format
        if wl and isinstance(wl[0], str):
            wl = [{"symbol": s, "type": "stock"} for s in wl]
            WATCHLIST_PATH.write_text(json.dumps(wl))
        return wl
    except Exception:
        return DEFAULT_WATCHLIST


def save_watchlist(wl):
    WATCHLIST_PATH.write_text(json.dumps(wl))


def load_market_news_sources():
    if not MARKET_NEWS_PATH.exists():
        MARKET_NEWS_PATH.write_text(json.dumps(DEFAULT_MARKET_NEWS_SOURCES, indent=2))
    try:
        return json.loads(MARKET_NEWS_PATH.read_text())
    except Exception:
        return DEFAULT_MARKET_NEWS_SOURCES


def save_market_news_sources(sources):
    MARKET_NEWS_PATH.write_text(json.dumps(sources, indent=2))


def fetch_yahoo_quote(symbol, range_str="1d", interval="5m"):
    """Fetch quote + chart. Uses yfinance if installed, raw API as fallback."""
    if _HAS_YFINANCE:
        result = _fetch_yahoo_yfinance(symbol, range_str, interval)
        if result:
            return result
    return _fetch_yahoo_raw(symbol, range_str, interval)


def _fetch_yahoo_yfinance(symbol, range_str="1d", interval="5m"):
    """Fetch via yfinance library — more reliable, adds fundamentals."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=range_str, interval=interval)
        if hist.empty:
            return None

        chart_data = []
        for ts, row in hist.iterrows():
            chart_data.append({
                "time": ts.isoformat(),
                "open": round(float(row.get("Open", row.get("Close", 0))), 2),
                "high": round(float(row.get("High", row.get("Close", 0))), 2),
                "low": round(float(row.get("Low", row.get("Close", 0))), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row.get("Volume", 0)),
            })
        if not chart_data:
            return None

        fi = ticker.fast_info if hasattr(ticker, "fast_info") else {}
        current = chart_data[-1]["close"]
        prev_close = round(float(getattr(fi, "previous_close", 0) or chart_data[0]["open"]), 2)
        change = round(current - prev_close, 2) if prev_close else 0
        change_pct = round((change / prev_close * 100), 2) if prev_close else 0

        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        return {
            "symbol": symbol.upper(),
            "name": info.get("shortName", info.get("longName", symbol)),
            "type": "stock",
            "price": current,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "currency": info.get("currency", "USD"),
            "exchange": info.get("exchange", ""),
            "market_state": "",
            "day_high": round(float(getattr(fi, "day_high", 0) or 0), 2),
            "day_low": round(float(getattr(fi, "day_low", 0) or 0), 2),
            "volume": int(getattr(fi, "last_volume", 0) or 0),
            "market_cap": int(getattr(fi, "market_cap", 0) or 0),
            "fifty_two_wk_high": round(float(getattr(fi, "year_high", 0) or 0), 2),
            "fifty_two_wk_low": round(float(getattr(fi, "year_low", 0) or 0), 2),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "pe_ratio": info.get("trailingPE"),
            "eps": info.get("trailingEps"),
            "chart": chart_data,
            "valid": True,
        }
    except Exception as e:
        log.debug(f"yfinance error [{symbol}]: {e}")
        return None


def _fetch_yahoo_raw(symbol, range_str="1d", interval="5m"):
    """Fetch from raw Yahoo chart API (fallback when yfinance unavailable)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        params = {"range": range_str, "interval": interval, "includePrePost": "true"}
        resp = requests.get(YAHOO_QUOTE_URL.format(symbol=symbol), params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        timestamps = result[0].get("timestamp", [])
        indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = indicators.get("close", [])
        opens = indicators.get("open", [])
        highs = indicators.get("high", [])
        lows = indicators.get("low", [])
        volumes = indicators.get("volume", [])

        chart_data = []
        for i, ts in enumerate(timestamps):
            if i < len(closes) and closes[i] is not None:
                chart_data.append({
                    "time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "open": round(opens[i], 2) if i < len(opens) and opens[i] is not None else round(closes[i], 2),
                    "high": round(highs[i], 2) if i < len(highs) and highs[i] is not None else round(closes[i], 2),
                    "low": round(lows[i], 2) if i < len(lows) and lows[i] is not None else round(closes[i], 2),
                    "close": round(closes[i], 2),
                    "volume": volumes[i] if i < len(volumes) else 0,
                })

        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
        current = meta.get("regularMarketPrice", 0)
        change = current - prev_close if prev_close else 0
        change_pct = (change / prev_close * 100) if prev_close else 0

        return {
            "symbol": meta.get("symbol", symbol).upper(),
            "name": meta.get("shortName", meta.get("longName", symbol)),
            "type": "stock",
            "price": round(current, 2),
            "prev_close": round(prev_close, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "currency": meta.get("currency", "USD"),
            "exchange": meta.get("exchangeName", ""),
            "market_state": meta.get("marketState", ""),
            "day_high": round(meta.get("regularMarketDayHigh", 0), 2),
            "day_low": round(meta.get("regularMarketDayLow", 0), 2),
            "volume": meta.get("regularMarketVolume", 0),
            "market_cap": meta.get("marketCap", 0),
            "fifty_two_wk_high": round(meta.get("fiftyTwoWeekHigh", 0), 2),
            "fifty_two_wk_low": round(meta.get("fiftyTwoWeekLow", 0), 2),
            "chart": chart_data,
            "valid": True,
        }
    except Exception as e:
        log.warning(f"Yahoo Finance raw API error [{symbol}]: {e}")
        return None


def fetch_crypto_quote(crypto_id, range_str="1d"):
    """Fetch crypto data from CoinGecko (free, no key needed)."""
    try:
        # Map range to CoinGecko days param
        days_map = {"1d": "1", "5d": "5", "1mo": "30", "3mo": "90", "6mo": "180", "1y": "365"}
        days = days_map.get(range_str, "1")

        # Get current price + market data
        resp = requests.get(f"{COINGECKO_URL}/coins/{crypto_id}", params={
            "localization": "false", "tickers": "false", "community_data": "false",
            "developer_data": "false", "sparkline": "false"
        }, timeout=10)
        if resp.status_code != 200:
            return None
        coin = resp.json()
        md = coin.get("market_data", {})

        # Get chart data
        chart_resp = requests.get(f"{COINGECKO_URL}/coins/{crypto_id}/market_chart", params={
            "vs_currency": "usd", "days": days
        }, timeout=10)
        chart_data = []
        if chart_resp.status_code == 200:
            prices = chart_resp.json().get("prices", [])
            for ts, price in prices:
                chart_data.append({
                    "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                    "close": round(price, 2),
                    "volume": 0,
                })

        current = md.get("current_price", {}).get("usd", 0)
        change_24h = md.get("price_change_24h", 0) or 0
        change_pct = md.get("price_change_percentage_24h", 0) or 0
        symbol = coin.get("symbol", crypto_id).upper()

        return {
            "symbol": symbol,
            "name": coin.get("name", crypto_id),
            "type": "crypto",
            "price": round(current, 2),
            "prev_close": round(current - change_24h, 2),
            "change": round(change_24h, 2),
            "change_pct": round(change_pct, 2),
            "currency": "USD",
            "exchange": "Crypto",
            "market_state": "24/7",
            "day_high": round(md.get("high_24h", {}).get("usd", 0), 2),
            "day_low": round(md.get("low_24h", {}).get("usd", 0), 2),
            "volume": md.get("total_volume", {}).get("usd", 0),
            "market_cap": md.get("market_cap", {}).get("usd", 0),
            "fifty_two_wk_high": round(md.get("ath", {}).get("usd", 0), 2),
            "fifty_two_wk_low": round(md.get("atl", {}).get("usd", 0), 2),
            "chart": chart_data,
            "valid": True,
            "coingecko_id": crypto_id,
        }
    except Exception as e:
        log.warning(f"CoinGecko error [{crypto_id}]: {e}")
        return None


def resolve_crypto_id(symbol):
    """Resolve a crypto ticker to a CoinGecko ID."""
    sym = symbol.upper().strip()
    if sym in CRYPTO_MAP:
        return CRYPTO_MAP[sym]
    # Try searching CoinGecko
    try:
        resp = requests.get(f"{COINGECKO_URL}/search", params={"query": sym}, timeout=10)
        if resp.status_code == 200:
            coins = resp.json().get("coins", [])
            if coins:
                return coins[0]["id"]
    except Exception:
        pass
    return None


@app.route("/api/market/indices")
def get_indices():
    """Get user-configured index data."""
    range_str = request.args.get("range", "1d")
    interval = request.args.get("interval", "5m")
    selected = load_indices_config()
    results = {}
    for sym in selected:
        data = fetch_yahoo_quote(sym, range_str, interval)
        if data:
            results[sym] = data
    return jsonify({"indices": results})


@app.route("/api/market/indices/config")
def get_indices_config():
    """Get available indices and which ones are selected."""
    return jsonify({
        "available": AVAILABLE_INDICES,
        "selected": load_indices_config(),
    })


@app.route("/api/market/indices/config", methods=["POST"])
def update_indices_config():
    """Update which indices are shown. Expects {"selected": ["^GSPC", "^IXIC", ...]}"""
    data = request.get_json(silent=True) or {}
    selected = data.get("selected")
    if not selected or not isinstance(selected, list):
        return jsonify({"error": "Provide selected as a list of symbols"}), 400
    # Validate — only allow known symbols
    valid = [s for s in selected if s in AVAILABLE_INDICES]
    if not valid:
        valid = DEFAULT_INDICES
    save_indices_config(valid)
    return jsonify({"selected": valid, "available": AVAILABLE_INDICES})


# Sector ETFs for the sector heatmap
SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Healthcare",
    "XLY": "Consumer Disc.", "XLP": "Consumer Staples", "XLE": "Energy",
    "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLC": "Communication",
}


@app.route("/api/market/sectors")
def get_sectors():
    """Get sector performance data using sector ETFs."""
    results = []
    for sym, name in SECTOR_ETFS.items():
        try:
            data = fetch_yahoo_quote(sym, "1d", "5m")
            if data:
                results.append({
                    "symbol": sym,
                    "name": name,
                    "price": data.get("price", 0),
                    "change_pct": data.get("change_pct", 0),
                    "change": data.get("change", 0),
                    "market_cap": data.get("market_cap", 0),
                })
        except Exception:
            pass
    # Sort by absolute market cap (approximate via ETF AUM)
    results.sort(key=lambda x: abs(x.get("market_cap", 0)), reverse=True)
    return jsonify({"sectors": results})


@app.route("/api/market/quote/<symbol>")
def get_quote(symbol):
    """Get detailed quote + chart for a stock or crypto."""
    range_str = request.args.get("range", "1d")
    interval = request.args.get("interval", "5m")
    asset_type = request.args.get("type", "stock")

    if asset_type == "crypto":
        crypto_id = resolve_crypto_id(symbol)
        if crypto_id:
            data = fetch_crypto_quote(crypto_id, range_str)
            if data:
                return jsonify({"quote": data})
        return jsonify({"error": f"Crypto '{symbol}' not found"}), 404
    else:
        data = fetch_yahoo_quote(symbol.upper(), range_str, interval)
        if data:
            return jsonify({"quote": data})
        return jsonify({"error": f"Stock '{symbol}' not found"}), 404


@app.route("/api/market/watchlist")
def get_watchlist():
    """Get watchlist with live quotes."""
    wl = load_watchlist()
    quotes = {}
    errors = []
    for item in wl:
        sym = item["symbol"]
        atype = item.get("type", "stock")
        if atype == "crypto":
            crypto_id = resolve_crypto_id(sym)
            if crypto_id:
                data = fetch_crypto_quote(crypto_id)
                if data:
                    quotes[sym] = data
                    continue
            errors.append(sym)
        else:
            data = fetch_yahoo_quote(sym, "1d", "5m")
            if data:
                quotes[sym] = data
            else:
                errors.append(sym)
    return jsonify({"watchlist": wl, "quotes": quotes, "errors": errors})


@app.route("/api/market/watchlist", methods=["POST"])
def update_watchlist():
    """Add, remove, or reorder symbols from watchlist with validation."""
    data = request.get_json(silent=True) or {}
    wl = load_watchlist()

    if data.get("add"):
        sym = data["add"].upper().strip()
        atype = data.get("type", "stock")
        # Check for duplicates
        for item in wl:
            if item["symbol"] == sym:
                return jsonify({"watchlist": wl, "status": "exists"})

        # Auto-detect: try stock first, then crypto
        if atype == "auto":
            test = fetch_yahoo_quote(sym, "1d", "5m")
            if test:
                atype = "stock"
            else:
                crypto_id = resolve_crypto_id(sym)
                if crypto_id:
                    atype = "crypto"
                else:
                    return jsonify({"error": f"'{sym}' not found as stock or crypto"}), 404

        if atype == "crypto":
            crypto_id = resolve_crypto_id(sym)
            if not crypto_id:
                return jsonify({"error": f"Crypto '{sym}' not found. Try the full name or common ticker (BTC, ETH, SOL, etc.)"}), 404
            wl.append({"symbol": sym, "type": "crypto"})
        else:
            test = fetch_yahoo_quote(sym, "1d", "5m")
            if not test:
                return jsonify({"error": f"Stock ticker '{sym}' not found"}), 404
            wl.append({"symbol": sym, "type": "stock"})

        save_watchlist(wl)
        return jsonify({"watchlist": wl, "status": "added"})

    elif data.get("remove"):
        sym = data["remove"].upper().strip()
        wl = [item for item in wl if item["symbol"] != sym]
        save_watchlist(wl)
        return jsonify({"watchlist": wl, "status": "removed"})

    elif data.get("reorder"):
        # Reorder watchlist to match provided symbol order
        order = data["reorder"]
        sym_map = {item["symbol"]: item for item in wl}
        new_wl = []
        for sym in order:
            if sym in sym_map:
                new_wl.append(sym_map[sym])
        # Add any items not in the reorder list at the end
        for item in wl:
            if item["symbol"] not in order:
                new_wl.append(item)
        save_watchlist(new_wl)
        return jsonify({"watchlist": new_wl, "status": "reordered"})

    return jsonify({"watchlist": wl})


@app.route("/api/market/search")
def market_search():
    """Search for stocks and cryptos by name/ticker using Yahoo Finance search API + CoinGecko."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"results": []})
    results = []
    seen = set()

    # Yahoo Finance search API — returns stocks, ETFs, indices, futures
    try:
        resp = requests.get(YAHOO_SEARCH_URL, params={
            "q": q, "quotesCount": 6, "newsCount": 0, "enableNavLinks": "false"
        }, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if resp.status_code == 200:
            for quote in resp.json().get("quotes", []):
                sym = quote.get("symbol", "").upper()
                if sym and sym not in seen:
                    seen.add(sym)
                    qtype = quote.get("quoteType", "").lower()
                    atype = "crypto" if qtype == "cryptocurrency" else "stock"
                    results.append({
                        "symbol": sym,
                        "name": quote.get("shortname", quote.get("longname", sym)),
                        "type": atype,
                        "exchange": quote.get("exchange", ""),
                    })
    except Exception:
        pass

    # CoinGecko search — catches crypto that Yahoo might miss
    try:
        crypto_id = resolve_crypto_id(q.upper())
        if crypto_id and q.upper() not in seen:
            results.append({"symbol": q.upper(), "name": crypto_id.replace("-", " ").title(), "type": "crypto"})
    except Exception:
        pass

    return jsonify({"results": results[:8]})


# ─── Market News ──────────────────────────────────────────────────────────────

@app.route("/api/market/news")
def get_market_news():
    """Fetch market news from configured sources. Optional ?symbol= to filter by ticker."""
    symbol = request.args.get("symbol", "").upper()
    sources = load_market_news_sources()
    articles = []

    for src_id, src in sources.items():
        if not src.get("enabled", True):
            continue
        try:
            feed = feedparser.parse(src["feed"])
            for entry in feed.entries[:10]:
                entry_date = parse_entry_date(entry)
                if not is_fresh(entry_date, 24):  # 24h for market news
                    continue
                title = normalize_whitespace(entry.get("title", ""))
                summary = normalize_whitespace(entry.get("summary", entry.get("description", "")))
                link = normalize_whitespace(entry.get("link", ""))
                if not title or not is_valid_source_url(link):
                    continue
                # If symbol filter, check if mentioned
                if symbol and symbol.lower() not in title.lower() and symbol.lower() not in summary.lower():
                    continue
                pub_str = ""
                if entry_date:
                    try:
                        pub_str = entry_date.astimezone(timezone.utc).strftime("%b %d, %I:%M %p UTC")
                    except Exception:
                        pass
                articles.append({
                    "title": title,
                    "summary": BeautifulSoup(summary or "", "html.parser").get_text(" ")[:200],
                    "url": link,
                    "source": src["name"],
                    "published_at": pub_str,
                })
        except Exception as e:
            log.debug(f"Market news error [{src_id}]: {e}")

    return jsonify({"articles": articles[:30]})


@app.route("/api/market/news-sources")
def get_market_news_sources():
    return jsonify({"sources": load_market_news_sources()})


@app.route("/api/market/news-sources", methods=["POST"])
def update_market_news_sources():
    data = request.get_json(silent=True) or {}
    sources = load_market_news_sources()
    if data.get("toggle"):
        src_id = data["toggle"]
        if src_id in sources:
            sources[src_id]["enabled"] = not sources[src_id].get("enabled", True)
            save_market_news_sources(sources)
    return jsonify({"sources": sources})


# ─── CSV Template Download ────────────────────────────────────────────────────

@app.route("/api/signal-sets/template.csv")
def csv_template():
    """Download a CSV template for signal word import."""
    csv_content = (
        "word,sentiment,weight\n"
        "bull run,positive,3\n"
        "moon,positive,2.5\n"
        "rug pull,negative,4\n"
        "liquidation,negative,3\n"
        "breakout,positive,2\n"
        "flash crash,negative,4\n"
        "accumulation,positive,2\n"
        "pump and dump,negative,3.5\n"
    )
    return csv_content, 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=signal_words_template.csv",
    }


# ─── Signal Set Weight Editing ────────────────────────────────────────────────

@app.route("/api/signal-sets/edit-weight", methods=["POST"])
def edit_signal_weight():
    """Edit the weight of an existing signal word."""
    data = request.get_json(silent=True) or {}
    set_id = data.get("set_id")
    sentiment = data.get("sentiment")
    word = data.get("word", "").strip().lower()
    new_weight = data.get("weight")

    if not all([set_id, sentiment, word, new_weight is not None]):
        return jsonify({"error": "Provide set_id, sentiment, word, and weight"}), 400

    sets = load_signal_sets()
    if set_id not in sets:
        return jsonify({"error": f"Set '{set_id}' not found"}), 404
    if sentiment not in ("positive", "negative"):
        return jsonify({"error": "sentiment must be 'positive' or 'negative'"}), 400
    if word not in sets[set_id].get(sentiment, {}):
        return jsonify({"error": f"Word '{word}' not found in {set_id}/{sentiment}"}), 404

    sets[set_id][sentiment][word] = float(new_weight)
    save_signal_sets(sets)
    return jsonify({"status": "updated", "sets": sets})


# ─── Data Store API ───────────────────────────────────────────────────────────

@app.route("/api/data/stats")
def data_stats():
    """Get data store statistics for debugging and trend overview."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM articles")
        total = c.fetchone()[0]
        c.execute("SELECT sentiment, COUNT(*) FROM articles GROUP BY sentiment")
        by_sentiment = dict(c.fetchall())
        c.execute("SELECT source_id, COUNT(*) FROM articles GROUP BY source_id ORDER BY COUNT(*) DESC LIMIT 10")
        by_source = dict(c.fetchall())
        c.execute("SELECT keyword, COUNT(*) FROM articles GROUP BY keyword ORDER BY COUNT(*) DESC LIMIT 15")
        by_keyword = dict(c.fetchall())
        c.execute("SELECT COUNT(*) FROM chat_logs")
        chats = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM articles WHERE article_fetched = 1")
        fetched = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM articles WHERE ai_summary = 1")
        ai_analyzed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM articles WHERE ai_sentiment != '' AND ai_sentiment IS NOT NULL AND word_sentiment != '' AND ai_sentiment != word_sentiment")
        disagreements = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM sentiment_corrections")
        corrections = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM system_logs")
        sys_logs = c.fetchone()[0]
        conn.close()
        return jsonify({
            "total_articles": total,
            "articles_fetched": fetched,
            "ai_analyzed": ai_analyzed,
            "sentiment_disagreements": disagreements,
            "user_corrections": corrections,
            "by_sentiment": by_sentiment,
            "by_source": by_source,
            "by_keyword": by_keyword,
            "total_chats": chats,
            "system_log_entries": sys_logs,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/trends")
def data_trends():
    """Get trend data — sentiment over time for a keyword or sector."""
    keyword = request.args.get("keyword", "")
    sector = request.args.get("sector", "")
    days = request.args.get("days", 7, type=int)
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if keyword:
            c.execute("SELECT timestamp, sentiment, sentiment_score, title FROM articles WHERE keyword = ? AND timestamp > ? ORDER BY timestamp",
                      (keyword.lower(), cutoff))
        elif sector:
            c.execute("SELECT timestamp, sentiment, sentiment_score, title FROM articles WHERE sectors LIKE ? AND timestamp > ? ORDER BY timestamp",
                      (f"%{sector}%", cutoff))
        else:
            c.execute("SELECT timestamp, sentiment, sentiment_score, title FROM articles WHERE timestamp > ? ORDER BY timestamp", (cutoff,))

        rows = [{"timestamp": r[0], "sentiment": r[1], "score": r[2], "title": r[3]} for r in c.fetchall()]
        # Aggregate
        pos = sum(1 for r in rows if r["sentiment"] == "positive")
        neg = sum(1 for r in rows if r["sentiment"] == "negative")
        neu = sum(1 for r in rows if r["sentiment"] == "neutral")
        avg_score = sum(r["score"] for r in rows) / max(len(rows), 1)
        conn.close()
        return jsonify({
            "query": keyword or sector or "all",
            "days": days,
            "total": len(rows),
            "positive": pos, "negative": neg, "neutral": neu,
            "avg_score": round(avg_score, 2),
            "articles": rows[-50:],  # Last 50 for charting
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/sectors")
def data_sectors():
    """Get sector breakdown from stored articles."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("SELECT sectors FROM articles WHERE sectors != '' AND sectors != '[]'")
        sector_counts = {}
        for row in c.fetchall():
            try:
                for s in json.loads(row[0]):
                    sector_counts[s] = sector_counts.get(s, 0) + 1
            except Exception:
                pass
        conn.close()
        return jsonify({"sectors": sector_counts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/correct", methods=["POST"])
def correct_sentiment():
    """
    User corrects an alert's classification. Accepts:
    - corrected_impact: bullish | neutral | bearish | duplicate (required)
    - context: user's explanation of why (optional, for AI training)
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    corrected_impact = data.get("corrected_impact", "")
    user_context = data.get("context", "").strip()

    if not url or corrected_impact not in ("bullish", "neutral", "bearish", "duplicate"):
        return jsonify({"error": "Provide url and corrected_impact (bullish/neutral/bearish/duplicate)"}), 400

    # Map impact to tone for backward compat
    impact_to_tone = {"bullish": "positive", "bearish": "negative", "neutral": "neutral", "duplicate": "neutral"}
    corrected_tone = impact_to_tone.get(corrected_impact, "neutral")

    title = ""
    original_tone = ""
    original_impact = ""
    source_id = ""
    keyword = ""
    with store.lock:
        for a in store.alerts:
            if a.get("url") == url:
                title = a.get("title", "")
                original_tone = a.get("article_tone", a.get("sentiment", ""))
                original_impact = a.get("market_impact", "")
                source_id = a.get("source", "")
                keyword = a.get("keyword", "")
                if corrected_impact == "duplicate":
                    a["user_corrected"] = True
                    a["is_duplicate"] = True
                else:
                    a["market_impact"] = corrected_impact
                    a["article_tone"] = corrected_tone
                    a["sentiment"] = corrected_tone
                    a["user_corrected"] = True
                break

    log_sentiment_correction(url, title, original_tone, corrected_tone,
                              source_id=source_id, keyword=keyword,
                              original_impact=original_impact,
                              corrected_impact=corrected_impact,
                              user_context=user_context)

    # Log context as a system event if provided (useful for AI training)
    if user_context:
        log_system_event("user_correction_context",
                         f"Corrected {original_impact}→{corrected_impact}: {title[:80]}",
                         user_context)

    return jsonify({
        "status": "corrected",
        "original_impact": original_impact,
        "corrected_impact": corrected_impact,
        "context_logged": bool(user_context),
    })


@app.route("/api/data/recent")
def data_recent():
    """Get recent articles from the data store."""
    limit = request.args.get("limit", 20, type=int)
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM articles ORDER BY id DESC LIMIT ?", (min(limit, 100),))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"articles": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/export")
def data_export():
    """Export all articles as JSON for analysis."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM articles ORDER BY id DESC")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"articles": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/system-logs")
def system_logs():
    """Get recent system logs for debugging."""
    limit = request.args.get("limit", 50, type=int)
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT ?", (min(limit, 200),))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"logs": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/accuracy")
def accuracy_dashboard():
    """
    Full accuracy dashboard — signal word misfires, source error rates,
    confidence calibration, disagreement outcomes.
    """
    return jsonify({
        "signal_word_misfires": recalibrate_thresholds(),
        "source_accuracy": compute_source_accuracy(),
        "confidence_calibration": calibrate_confidence(),
        "disagreement_outcomes": get_disagreement_outcomes(),
        "correction_stats_global": get_correction_stats(),
    })


@app.route("/api/data/apply-recalibration", methods=["POST"])
def apply_recalibration():
    """
    Auto-apply signal word weight reductions based on misfire data.
    Reduces weight of frequently-misfiring words by 30%.
    """
    result = recalibrate_thresholds()
    adjustments = result.get("adjustments", [])
    if not adjustments:
        return jsonify({"status": "no_adjustments_needed", "message": "Not enough correction data or no misfiring words found"})

    sets = load_signal_sets()
    applied = []

    for adj in adjustments:
        word = adj["word"]
        sig_type = adj["current_type"]
        for set_id, s in sets.items():
            if word in s.get(sig_type, {}):
                old_weight = s[sig_type][word]
                new_weight = round(max(0.5, old_weight * 0.7), 1)  # Reduce by 30%, floor at 0.5
                s[sig_type][word] = new_weight
                applied.append({
                    "set": set_id, "word": word, "type": sig_type,
                    "old_weight": old_weight, "new_weight": new_weight,
                    "misfire_count": adj["misfire_count"],
                })

    if applied:
        save_signal_sets(sets)
        log_system_event("recalibration", f"Applied {len(applied)} weight adjustments",
                         json.dumps(applied))

    return jsonify({"status": "applied", "adjustments": applied, "sets": sets})


# ─── External Data APIs ───────────────────────────────────────────────────────
# Integrations that improve context for AI analysis and user experience.

# FRED (Federal Reserve Economic Data) — 800K+ economic time series
# Free key from https://fred.stlouisfed.org/docs/api/api_key.html
FRED_BASE = "https://api.stlouisfed.org/fred"

@app.route("/api/economy/fred/<series_id>")
def fred_series(series_id):
    """Fetch FRED economic data. Useful series: GDP, UNRATE, CPIAUCSL, DFF, T10Y2Y, FEDFUNDS"""
    config = load_config()
    api_key = config.get("fred_api_key", "")
    if not api_key:
        return jsonify({"error": "Add fred_api_key to config.json. Get one free at fred.stlouisfed.org"}), 400
    try:
        params = {
            "series_id": series_id.upper(),
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": request.args.get("limit", 30, type=int),
        }
        resp = requests.get(f"{FRED_BASE}/series/observations", params=params, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": f"FRED returned {resp.status_code}"}), 500
        data = resp.json()
        observations = [{"date": o["date"], "value": float(o["value"]) if o["value"] != "." else None}
                        for o in data.get("observations", []) if o.get("value")]
        # Also get series info
        info_resp = requests.get(f"{FRED_BASE}/series", params={
            "series_id": series_id.upper(), "api_key": api_key, "file_type": "json"
        }, timeout=10)
        info = {}
        if info_resp.status_code == 200:
            serieses = info_resp.json().get("seriess", [])
            if serieses:
                info = {"title": serieses[0].get("title", ""), "units": serieses[0].get("units", ""),
                        "frequency": serieses[0].get("frequency", "")}
        return jsonify({"series_id": series_id.upper(), "info": info, "observations": observations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/economy/fred-dashboard")
def fred_dashboard():
    """Get key economic indicators in one call."""
    config = load_config()
    api_key = config.get("fred_api_key", "")
    if not api_key:
        return jsonify({"error": "Add fred_api_key to config.json"}), 400
    indicators = {
        "FEDFUNDS": "Fed Funds Rate",
        "DFF": "Effective Fed Funds Rate",
        "T10Y2Y": "10Y-2Y Yield Spread",
        "CPIAUCSL": "CPI (Inflation)",
        "UNRATE": "Unemployment Rate",
        "GDP": "GDP",
        "VIXCLS": "VIX (Volatility)",
    }
    results = {}
    for sid, label in indicators.items():
        try:
            resp = requests.get(f"{FRED_BASE}/series/observations", params={
                "series_id": sid, "api_key": api_key, "file_type": "json",
                "sort_order": "desc", "limit": 2
            }, timeout=8)
            if resp.status_code == 200:
                obs = resp.json().get("observations", [])
                if obs:
                    current = obs[0]
                    prev = obs[1] if len(obs) > 1 else obs[0]
                    try:
                        val = float(current["value"])
                        prev_val = float(prev["value"])
                        change = val - prev_val
                    except (ValueError, KeyError):
                        val, prev_val, change = None, None, None
                    results[sid] = {"label": label, "value": val, "prev": prev_val,
                                    "change": round(change, 3) if change is not None else None,
                                    "date": current["date"]}
        except Exception:
            pass
    return jsonify({"indicators": results})


# Alpha Vantage — stock data with free key (25 req/day)
# Free key from https://www.alphavantage.co/support/#api-key
@app.route("/api/market/alpha-vantage/<symbol>")
def alpha_vantage_quote(symbol):
    """Get stock overview from Alpha Vantage (richer data than Yahoo for fundamentals)."""
    config = load_config()
    api_key = config.get("alpha_vantage_key", "")
    if not api_key:
        return jsonify({"error": "Add alpha_vantage_key to config.json"}), 400
    try:
        resp = requests.get("https://www.alphavantage.co/query", params={
            "function": "OVERVIEW", "symbol": symbol.upper(), "apikey": api_key
        }, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": f"Alpha Vantage returned {resp.status_code}"}), 500
        data = resp.json()
        if "Symbol" not in data:
            return jsonify({"error": f"No data for {symbol}"}), 404
        return jsonify({
            "symbol": data.get("Symbol"), "name": data.get("Name"),
            "sector": data.get("Sector"), "industry": data.get("Industry"),
            "market_cap": data.get("MarketCapitalization"),
            "pe_ratio": data.get("PERatio"), "eps": data.get("EPS"),
            "dividend_yield": data.get("DividendYield"),
            "52_week_high": data.get("52WeekHigh"),
            "52_week_low": data.get("52WeekLow"),
            "50_day_avg": data.get("50DayMovingAverage"),
            "200_day_avg": data.get("200DayMovingAverage"),
            "beta": data.get("Beta"),
            "description": data.get("Description", "")[:300],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Binance — public orderbook, trades, ticker (no key needed)
@app.route("/api/crypto/binance/<symbol>")
def binance_ticker(symbol):
    """Get 24h ticker data from Binance for a crypto pair."""
    try:
        pair = symbol.upper() + "USDT"
        resp = requests.get(f"https://api.binance.com/api/v3/ticker/24hr",
                            params={"symbol": pair}, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": f"Binance: pair {pair} not found"}), 404
        d = resp.json()
        return jsonify({
            "symbol": symbol.upper(), "pair": pair,
            "price": round(float(d.get("lastPrice", 0)), 4),
            "change_24h": round(float(d.get("priceChange", 0)), 4),
            "change_pct_24h": round(float(d.get("priceChangePercent", 0)), 2),
            "high_24h": round(float(d.get("highPrice", 0)), 4),
            "low_24h": round(float(d.get("lowPrice", 0)), 4),
            "volume_24h": round(float(d.get("volume", 0)), 2),
            "quote_volume_24h": round(float(d.get("quoteVolume", 0)), 2),
            "trades_24h": int(d.get("count", 0)),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Etherscan — Ethereum gas prices + token data (free key)
@app.route("/api/crypto/eth-gas")
def eth_gas():
    """Get current Ethereum gas prices from Etherscan."""
    config = load_config()
    api_key = config.get("etherscan_key", "")
    if not api_key:
        return jsonify({"error": "Add etherscan_key to config.json"}), 400
    try:
        resp = requests.get("https://api.etherscan.io/api", params={
            "module": "gastracker", "action": "gasoracle", "apikey": api_key
        }, timeout=10)
        if resp.status_code == 200:
            r = resp.json().get("result", {})
            return jsonify({
                "low_gwei": r.get("SafeGasPrice"),
                "avg_gwei": r.get("ProposeGasPrice"),
                "high_gwei": r.get("FastGasPrice"),
                "base_fee": r.get("suggestBaseFee"),
            })
        return jsonify({"error": "Etherscan unavailable"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/crypto/eth-price")
def eth_price():
    """Get ETH price from Etherscan."""
    config = load_config()
    api_key = config.get("etherscan_key", "")
    if not api_key:
        return jsonify({"error": "Add etherscan_key to config.json"}), 400
    try:
        resp = requests.get("https://api.etherscan.io/api", params={
            "module": "stats", "action": "ethprice", "apikey": api_key
        }, timeout=10)
        if resp.status_code == 200:
            r = resp.json().get("result", {})
            return jsonify({
                "eth_usd": round(float(r.get("ethusd", 0)), 2),
                "eth_btc": r.get("ethbtc"),
                "timestamp": r.get("ethusd_timestamp"),
            })
        return jsonify({"error": "Etherscan unavailable"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Hacker News — top tech/startup stories (no key needed)
@app.route("/api/news/hackernews")
def hackernews_top():
    """Get top Hacker News stories — useful for tech/startup/AI news."""
    try:
        limit = request.args.get("limit", 15, type=int)
        resp = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": "HN API unavailable"}), 500
        story_ids = resp.json()[:min(limit, 30)]
        stories = []
        for sid in story_ids:
            sr = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=5)
            if sr.status_code == 200:
                s = sr.json()
                if s and s.get("type") == "story" and s.get("url"):
                    stories.append({
                        "title": s.get("title", ""),
                        "url": s.get("url", ""),
                        "score": s.get("score", 0),
                        "comments": s.get("descendants", 0),
                        "by": s.get("by", ""),
                        "time": datetime.fromtimestamp(s.get("time", 0), tz=timezone.utc).isoformat(),
                    })
        return jsonify({"stories": stories})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Fear & Greed Index — market sentiment gauge (no key needed)
@app.route("/api/market/fear-greed")
def fear_greed_index():
    """Get CNN Fear & Greed Index — useful context for AI analysis."""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            results = []
            for d in data:
                results.append({
                    "value": int(d.get("value", 50)),
                    "label": d.get("value_classification", "Neutral"),
                    "timestamp": datetime.fromtimestamp(int(d.get("timestamp", 0)), tz=timezone.utc).isoformat(),
                })
            return jsonify({"fear_greed": results})
        return jsonify({"error": "Fear & Greed API unavailable"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── CoinGecko Trending & Global Metrics (no key needed) ─────────────────────

@app.route("/api/crypto/trending")
def crypto_trending():
    """Get trending coins from CoinGecko — most searched in last 24h."""
    try:
        resp = requests.get(f"{COINGECKO_URL}/search/trending", timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": "CoinGecko trending unavailable"}), 500
        data = resp.json()
        coins = []
        for item in data.get("coins", [])[:10]:
            c = item.get("item", {})
            coins.append({
                "id": c.get("id", ""),
                "symbol": c.get("symbol", "").upper(),
                "name": c.get("name", ""),
                "market_cap_rank": c.get("market_cap_rank"),
                "price_btc": c.get("price_btc", 0),
                "score": c.get("score", 0),
                "thumb": c.get("thumb", ""),
            })
        return jsonify({"trending": coins})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/crypto/global")
def crypto_global():
    """Get global crypto market metrics from CoinGecko."""
    try:
        resp = requests.get(f"{COINGECKO_URL}/global", timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": "CoinGecko global unavailable"}), 500
        data = resp.json().get("data", {})
        return jsonify({
            "total_market_cap": data.get("total_market_cap", {}).get("usd", 0),
            "total_volume": data.get("total_volume", {}).get("usd", 0),
            "btc_dominance": round(data.get("market_cap_percentage", {}).get("btc", 0), 1),
            "eth_dominance": round(data.get("market_cap_percentage", {}).get("eth", 0), 1),
            "active_cryptos": data.get("active_cryptocurrencies", 0),
            "markets": data.get("markets", 0),
            "market_cap_change_24h": round(data.get("market_cap_change_percentage_24h_usd", 0), 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── SEC EDGAR Filing Search & Recent Filings ────────────────────────────────

SEC_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_EDGAR_HEADERS = {"User-Agent": "SIGINT Monitor sigint@local", "Accept": "application/json"}


@app.route("/api/sec/search")
def sec_search():
    """Search SEC EDGAR for filings by company name, ticker, or keyword."""
    q = request.args.get("q", "").strip()
    forms = request.args.get("forms", "8-K,10-K,10-Q,S-1,4")
    limit = request.args.get("limit", 15, type=int)
    if not q:
        return jsonify({"filings": []})
    try:
        resp = requests.get("https://efts.sec.gov/LATEST/search-index", params={
            "q": q, "forms": forms, "dateRange": "custom",
            "startdt": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "enddt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }, headers=SEC_EDGAR_HEADERS, timeout=10)
        if resp.status_code != 200:
            return jsonify({"filings": [], "error": f"EDGAR returned {resp.status_code}"})
        data = resp.json()
        filings = []
        for hit in data.get("hits", {}).get("hits", [])[:limit]:
            src = hit.get("_source", {})
            file_num = src.get("file_num", "")
            form_type = src.get("form_type", "")
            entity = src.get("entity_name", "")
            # Use file_num or entity for EDGAR company lookup
            cik = file_num if file_num else requests.utils.quote(entity)
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=include&count=10&search_text=&action=getcompany"

            filings.append({
                "form_type": form_type,
                "entity": entity,
                "filed_date": src.get("file_date", ""),
                "description": src.get("display_names", [""])[0] if src.get("display_names") else "",
                "url": filing_url,
            })
        return jsonify({"filings": filings, "query": q})
    except Exception as e:
        return jsonify({"filings": [], "error": str(e)})


@app.route("/api/sec/recent")
def sec_recent():
    """Get recent SEC filings for watchlist tickers."""
    wl = load_watchlist()
    tickers = [item["symbol"] for item in wl if item.get("type", "stock") == "stock"]
    if not tickers:
        return jsonify({"filings": []})
    all_filings = []
    for ticker in tickers[:8]:
        try:
            resp = requests.get("https://efts.sec.gov/LATEST/search-index", params={
                "q": f'"{ticker}"', "forms": "8-K,10-K,10-Q",
                "dateRange": "custom",
                "startdt": (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),
                "enddt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }, headers=SEC_EDGAR_HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            for hit in resp.json().get("hits", {}).get("hits", [])[:3]:
                src = hit.get("_source", {})
                entity = src.get("entity_name", "")
                form_type = src.get("form_type", "")
                filed = src.get("file_date", "")
                # Use ticker-based EDGAR company filing link (reliable)
                filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type}&dateb=&owner=include&count=5&search_text=&action=getcompany"
                all_filings.append({
                    "ticker": ticker,
                    "form_type": form_type,
                    "entity": entity,
                    "filed_date": filed,
                    "url": filing_url,
                })
        except Exception:
            pass
    # Sort by date descending
    all_filings.sort(key=lambda x: x.get("filed_date", ""), reverse=True)
    return jsonify({"filings": all_filings[:20]})


# ─── Wikipedia Context API ───────────────────────────────────────────────────
# Used as supplementary context for AI analysis — NOT a primary data source.
# Provides entity descriptions, historical context, and background for better inferences.

WIKI_API = "https://en.wikipedia.org/api/rest_v1"
_wiki_cache = {}  # Simple in-memory cache: {term: {data, ts}}
WIKI_CACHE_TTL = 3600  # 1 hour


def wiki_summary(term):
    """Fetch a short Wikipedia summary for entity context. Returns string or empty."""
    if not term or len(term) < 2:
        return ""
    term_key = term.lower().strip()
    # Check cache
    cached = _wiki_cache.get(term_key)
    if cached and (time.time() - cached["ts"]) < WIKI_CACHE_TTL:
        return cached["data"]
    try:
        resp = requests.get(
            f"{WIKI_API}/page/summary/{requests.utils.quote(term)}",
            headers={"User-Agent": "SIGINT Monitor/1.0"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            extract = data.get("extract", "")[:300]
            _wiki_cache[term_key] = {"data": extract, "ts": time.time()}
            return extract
    except Exception:
        pass
    _wiki_cache[term_key] = {"data": "", "ts": time.time()}
    return ""


def build_wiki_context(text, keyword=""):
    """Extract key entities from text and fetch Wikipedia context for AI enrichment.
    Returns a short context block or empty string. Lightweight — max 2 lookups."""
    entities = extract_entities(text)
    # Prioritize: keyword, then first extracted entity
    terms = []
    if keyword and len(keyword) > 3:
        terms.append(keyword)
    for ent in entities[:3]:
        if ent not in terms and len(ent) > 3:
            terms.append(ent)
    if not terms:
        return ""
    parts = []
    for term in terms[:2]:  # Max 2 Wikipedia lookups per article
        summary = wiki_summary(term)
        if summary:
            parts.append(f"- {term}: {summary}")
    if parts:
        return "BACKGROUND CONTEXT (Wikipedia):\n" + "\n".join(parts) + "\n\n"
    return ""


@app.route("/api/wiki/summary/<term>")
def wiki_summary_endpoint(term):
    """Get Wikipedia summary for a term — useful for entity context."""
    summary = wiki_summary(term)
    if summary:
        return jsonify({"term": term, "summary": summary})
    return jsonify({"term": term, "summary": "", "error": "Not found"}), 404


_monitor_started = False
_monitor_lock = threading.Lock()


def _start_monitor():
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    log.info("Monitor thread launched")


# NOTE: If using gunicorn, run with --workers 1 to avoid duplicate polling.
# e.g.: gunicorn --bind 0.0.0.0:5000 --workers 1 --timeout 120 monitor:app
_start_monitor()


if __name__ == "__main__":
    log.info("API server starting on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
