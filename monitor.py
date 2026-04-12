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

DATA_STORE_PATH = Path("sigint_data.db")


def init_data_store():
    """Initialize the SQLite database with required tables."""
    conn = sqlite3.connect(str(DATA_STORE_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
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
        published_at TEXT,
        positive_signals TEXT,
        negative_signals TEXT,
        article_fetched INTEGER DEFAULT 0,
        ai_summary INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        alert_id INTEGER,
        article_title TEXT,
        question TEXT,
        answer TEXT
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_articles_timestamp ON articles(timestamp)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_articles_sentiment ON articles(sentiment)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_articles_keyword ON articles(keyword)""")
    conn.commit()
    conn.close()
    log.info(f"Data store initialized: {DATA_STORE_PATH}")


def log_article(alert_data, article_text=""):
    """Log an article and its analysis to the data store."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("""INSERT OR IGNORE INTO articles
            (timestamp, source_id, source_name, title, url, keyword,
             article_text, summary, sentiment, sentiment_score,
             ai_sentiment, word_sentiment, confidence, severity,
             published_at, positive_signals, negative_signals,
             article_fetched, ai_summary)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (alert_data.get("timestamp", ""),
             alert_data.get("source", ""),
             alert_data.get("source_name", ""),
             alert_data.get("title", ""),
             alert_data.get("url", ""),
             alert_data.get("keyword", ""),
             article_text[:5000] if article_text else "",
             alert_data.get("summary", ""),
             alert_data.get("sentiment", ""),
             alert_data.get("sentiment_score", 0),
             alert_data.get("ai_sentiment", ""),
             alert_data.get("word_sentiment", ""),
             alert_data.get("confidence", 0),
             alert_data.get("severity", ""),
             alert_data.get("published_at", ""),
             json.dumps(alert_data.get("positive_signals", [])),
             json.dumps(alert_data.get("negative_signals", [])),
             1 if alert_data.get("article_fetched") else 0,
             1 if alert_data.get("ai_summary") else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Data store log error: {e}")


def log_chat(alert_id, article_title, question, answer):
    """Log a chat interaction to the data store."""
    try:
        conn = sqlite3.connect(str(DATA_STORE_PATH))
        c = conn.cursor()
        c.execute("INSERT INTO chat_logs (timestamp, alert_id, article_title, question, answer) VALUES (?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), alert_id, article_title, question, answer))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Chat log error: {e}")


# Initialize on import
init_data_store()


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
    "newsapi_key": "",
    "twitter_bearer_token": "",
    "truthsocial_enabled": True,
    "rss_enabled": True,
    "whitehouse_enabled": True,
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log.info(f"Created default config at {CONFIG_PATH} — edit it to add API keys.")
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


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
    """Thread-safe in-memory alert store with deduplication."""

    def __init__(self, max_alerts=500):
        self.alerts = []
        self.seen_hashes = set()
        self.max_alerts = max_alerts
        self.lock = threading.Lock()
        self.ollama_url = ""
        self.ollama_model = "llama3"

    def _hash(self, source_id, title, url):
        dedupe_basis = url.strip() if url else f"{source_id}:{title.strip().lower()}"
        return hashlib.md5(dedupe_basis.encode()).hexdigest()

    def add(self, source_id, source_name, title, url, snippet, keyword, severity="medium", published_at=None):
        if not title or not is_valid_source_url(url):
            return False
        h = self._hash(source_id, title, url)
        with self.lock:
            if h in self.seen_hashes:
                return False
            self.seen_hashes.add(h)

        # Fetch article text for deeper analysis (outside lock to avoid blocking)
        article_text, fetched = fetch_article_text(url)
        analysis_text = article_text if fetched else f"{title}. {snippet}"

        # Run sentiment analysis
        sentiment = analyze_sentiment(
            analysis_text,
            source_id=source_id,
            matched_keywords=[keyword],
            ollama_url=self.ollama_url,
            ollama_model=self.ollama_model,
        )

        # Format published_at for display
        pub_str = ""
        if published_at:
            try:
                pub_str = published_at.astimezone(timezone(timedelta(hours=-5))).strftime("%b %d, %I:%M %p EST")
            except Exception:
                pub_str = str(published_at)

        with self.lock:
            alert = {
                "id": int(time.time() * 1000),
                "source": source_id,
                "source_name": source_name,
                "title": title.strip(),
                "url": url.strip(),
                "snippet": (snippet or "").strip(),
                "keyword": keyword.strip().lower(),
                "severity": sentiment["severity"],
                "sentiment": sentiment["sentiment"],
                "sentiment_score": sentiment["score"],
                "confidence": sentiment["confidence"],
                "summary": sentiment["summary"],
                "ai_summary": sentiment.get("ai_summary", False),
                "ai_sentiment": sentiment.get("ai_sentiment"),
                "word_sentiment": sentiment.get("word_sentiment"),
                "positive_signals": sentiment["positive_signals"],
                "negative_signals": sentiment["negative_signals"],
                "article_fetched": fetched,
                "published_at": pub_str,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.alerts.insert(0, alert)
            if len(self.alerts) > self.max_alerts:
                self.alerts = self.alerts[: self.max_alerts]

            # Log to data store
            log_article(alert, article_text)

            emoji = {"positive": "📈", "negative": "📉", "neutral": "➡️"}.get(sentiment["sentiment"], "➡️")
            ai_tag = " 🤖" if sentiment.get("ai_summary") else ""
            disagree = " ⚡" if sentiment.get("ai_sentiment") and sentiment.get("word_sentiment") and sentiment["ai_sentiment"] != sentiment["word_sentiment"] else ""
            log.info(f"{emoji}{ai_tag}{disagree} [{source_id}] {sentiment['sentiment']}({sentiment['score']:+.1f}) keyword='{keyword}' → {title[:70]}")
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


# ─── Ollama Integration ───────────────────────────────────────────────────────

def ollama_analyze(text, ollama_url, model="llama3"):
    """
    Send article text to Ollama for AI-powered summary AND sentiment judgment.
    Returns (summary, ai_sentiment, success).
    ai_sentiment is "positive", "negative", or "neutral".
    """
    if not ollama_url:
        return "", "", False
    try:
        prompt = (
            "You are a financial market analyst. Analyze the following article.\n\n"
            "Respond in EXACTLY this format (no extra text):\n"
            "SENTIMENT: positive OR negative OR neutral\n"
            "SUMMARY: 2-3 sentence summary focusing on market impact, affected sectors/companies, and what traders should know.\n\n"
            f"Article:\n{text[:2500]}"
        )
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=45,
        )
        if resp.status_code == 200:
            result = resp.json().get("response", "").strip()
            if result:
                # Parse structured response
                ai_sentiment = ""
                summary = result
                lines = result.split("\n")
                for line in lines:
                    line_stripped = line.strip()
                    if line_stripped.upper().startswith("SENTIMENT:"):
                        val = line_stripped.split(":", 1)[1].strip().lower()
                        if val in ("positive", "negative", "neutral"):
                            ai_sentiment = val
                    elif line_stripped.upper().startswith("SUMMARY:"):
                        summary = line_stripped.split(":", 1)[1].strip()
                # If parsing failed, use full response as summary
                if not summary or summary == result:
                    summary = result[:500]
                return summary[:500], ai_sentiment, True
        return "", "", False
    except Exception as e:
        log.debug(f"Ollama analyze error: {e}")
        return "", "", False


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


def analyze_sentiment(text, source_id="", matched_keywords=None, ollama_url="", ollama_model="llama3"):
    """
    Hybrid sentiment analysis:
    1. Word-based scoring (instant, always runs)
    2. Ollama AI judgment (when available, overrides word-based if they disagree)
    Returns combined result with both scores visible.
    """
    text_lower = text.lower()
    matched_keywords = matched_keywords or []
    pos_signals_dict, neg_signals_dict = get_active_signals()

    pos_score = 0.0
    neg_score = 0.0
    pos_signals = []
    neg_signals = []

    for word, weight in pos_signals_dict.items():
        pattern = re.compile(rf"(?<!\w){re.escape(word)}", re.IGNORECASE)
        found = pattern.findall(text_lower)
        if found:
            pos_score += weight * len(found)
            pos_signals.append(word)

    for word, weight in neg_signals_dict.items():
        pattern = re.compile(rf"(?<!\w){re.escape(word)}", re.IGNORECASE)
        found = pattern.findall(text_lower)
        if found:
            neg_score += weight * len(found)
            neg_signals.append(word)

    # Check for negation patterns that flip sentiment
    negation_patterns = [
        (r"not\s+(rising|gaining|improving|growing)", "neg_flip"),
        (r"no\s+(deal|agreement|progress|recovery)", "neg_flip"),
        (r"fail(ed|s)?\s+to\s+(rally|recover|gain|rise)", "neg_flip"),
        (r"unlikely\s+to\s+(cut|ease|approve|pass)", "neg_flip"),
        (r"(avoid|prevent|avert)(ed|s|ing)?\s+(crash|crisis|recession|default)", "pos_flip"),
        (r"(ease|calm|cool)(ed|s|ing)?\s+(fears?|concerns?|tensions?|worries)", "pos_flip"),
        (r"despite\s+(concerns?|fears?|worries|criticism)", "pos_context"),
    ]
    for pat, flip_type in negation_patterns:
        if re.search(pat, text_lower):
            if flip_type == "neg_flip":
                neg_score += 2
            elif flip_type == "pos_flip":
                pos_score += 2

    amplifier = 1.0
    for word, mult in AMPLIFIERS.items():
        if word.lower() in text_lower:
            amplifier = max(amplifier, mult)

    kw_set = set(k.lower() for k in matched_keywords)
    combo_mult = 1.0
    for combo_words, mult in HIGH_IMPACT_COMBOS:
        if combo_words.issubset(kw_set) or all(w in text_lower for w in combo_words):
            combo_mult = max(combo_mult, mult)

    src_weight = SOURCE_WEIGHTS.get(source_id, 0.5)
    raw = (pos_score - neg_score) * amplifier * combo_mult * src_weight
    word_score = max(-10.0, min(10.0, raw))

    if word_score > 1.5:
        word_sentiment = "positive"
    elif word_score < -1.5:
        word_sentiment = "negative"
    else:
        word_sentiment = "neutral"

    # Confidence from word analysis
    total_signals = len(pos_signals) + len(neg_signals)
    text_len = max(len(text.split()), 1)
    word_confidence = min(1.0, (total_signals / text_len) * 10 + (abs(word_score) / 10) * 0.5)

    # Get Ollama analysis (summary + sentiment)
    ai_summary, ai_sentiment, used_ollama = ollama_analyze(text, ollama_url, ollama_model)

    # Fallback summary if Ollama didn't provide one
    if not ai_summary:
        sentences = re.split(r'(?<=[.!?])\s+', text[:600])
        ai_summary = " ".join(sentences[:2]).strip()
        if len(ai_summary) > 250:
            ai_summary = ai_summary[:247] + "..."

    # Hybrid decision: combine word-based and AI sentiment
    if used_ollama and ai_sentiment:
        if ai_sentiment == word_sentiment:
            # Both agree — high confidence
            final_sentiment = ai_sentiment
            final_confidence = min(1.0, word_confidence + 0.3)
        elif word_sentiment == "neutral":
            # Words inconclusive, trust AI
            final_sentiment = ai_sentiment
            final_confidence = 0.7
        elif ai_sentiment == "neutral":
            # AI unsure, use words
            final_sentiment = word_sentiment
            final_confidence = word_confidence
        else:
            # Disagreement — trust AI over words (AI understands context better)
            final_sentiment = ai_sentiment
            final_confidence = 0.5  # Lower confidence due to disagreement
    else:
        final_sentiment = word_sentiment
        final_confidence = round(word_confidence, 2)

    # Map final sentiment to a score for display
    if final_sentiment != word_sentiment:
        # Remap score direction to match AI judgment
        final_score = abs(word_score) if final_sentiment == "positive" else -abs(word_score) if final_sentiment == "negative" else 0
    else:
        final_score = word_score

    abs_score = abs(final_score)
    if abs_score >= 5:
        severity = "high"
    elif abs_score >= 2:
        severity = "medium"
    else:
        severity = "low"

    return {
        "sentiment": final_sentiment,
        "score": round(final_score, 2),
        "confidence": round(final_confidence, 2),
        "severity": severity,
        "summary": ai_summary,
        "ai_summary": used_ollama,
        "ai_sentiment": ai_sentiment if used_ollama else None,
        "word_sentiment": word_sentiment,
        "positive_signals": pos_signals[:5],
        "negative_signals": neg_signals[:5],
    }


def cleaned_entry_text(*parts):
    return normalize_whitespace(BeautifulSoup(" ".join([p for p in parts if p]), "html.parser").get_text(" "))


# ─── Article Freshness Checking ───────────────────────────────────────────────

# EST timezone offset (UTC-5), EDT (UTC-4)
EST = timezone(timedelta(hours=-5))


def now_est():
    """Current time in EST."""
    return datetime.now(EST)


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
    """Poll NewsAPI for keyword matches (requires free API key from newsapi.org)."""
    if not api_key:
        return
    try:
        priority_kw = keywords[:10]
        for kw in priority_kw:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": f'Trump AND "{kw}"',
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
    """Scrape White House presidential actions page for new items."""
    try:
        resp = requests.get(WHITEHOUSE_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KeywordMonitor/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")
        for link_tag in soup.find_all("a", href=True):
            text = normalize_whitespace(link_tag.get_text(" ", strip=True))
            href = normalize_whitespace(link_tag["href"])
            if not text or len(text) < 15:
                continue
            if not href.startswith("http"):
                href = f"https://www.whitehouse.gov{href}"
            if not is_valid_source_url(href):
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

            # Ollama config
            store.ollama_url = config.get("ollama_url", "")
            store.ollama_model = config.get("ollama_model", "llama3")
            ollama_status = f"Ollama: {store.ollama_model}@{store.ollama_url}" if store.ollama_url else "Ollama: off"

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
    Ask a follow-up question about an article using Ollama.
    Expects: {
        "alert_id": 123456,
        "question": "What sectors are affected?",
        "history": [{"role":"user","text":"..."}, {"role":"ai","text":"..."}]
    }
    """
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    alert_id = data.get("alert_id")
    history = data.get("history", [])

    config = load_config()
    ollama_url = config.get("ollama_url", "")
    ollama_model = config.get("ollama_model", "llama3")

    if not ollama_url:
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

    # Build conversation history for context
    conv_text = ""
    for msg in history[-10:]:  # Last 10 messages to keep prompt manageable
        role = "User" if msg.get("role") == "user" else "Assistant"
        conv_text += f"\n{role}: {msg.get('text', '')}"

    try:
        prompt = (
            f"You are a financial market analyst. Based on the following article, answer the user's question concisely.\n\n"
            f"Article Context:\n{context}\n"
        )
        if conv_text:
            prompt += f"\nPrevious conversation:{conv_text}\n"
        prompt += f"\nUser: {question}\n\nAssistant:"

        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            # Log chat to data store
            article_title = ""
            with store.lock:
                for a in store.alerts:
                    if a["id"] == alert_id:
                        article_title = a.get("title", "")
                        break
            log_chat(alert_id, article_title, question, answer)
            return jsonify({"answer": answer})
        return jsonify({"error": f"Ollama returned {resp.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": f"Ollama error: {str(e)}"}), 500


# ─── Market Data API ──────────────────────────────────────────────────────────

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

# Stored watchlist — items are {"symbol": "NVDA", "type": "stock"} or {"symbol": "bitcoin", "type": "crypto"}
WATCHLIST_PATH = Path("watchlist.json")
DEFAULT_WATCHLIST = [{"symbol": "NVDA", "type": "stock"}]
DEFAULT_INDICES = ["^GSPC", "^IXIC", "^DJI", "SPY"]

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
    """Fetch quote and chart data from Yahoo Finance."""
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
        volumes = indicators.get("volume", [])

        chart_data = []
        for i, ts in enumerate(timestamps):
            if i < len(closes) and closes[i] is not None:
                chart_data.append({
                    "time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
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
        log.warning(f"Yahoo Finance error [{symbol}]: {e}")
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
    """Get major index data."""
    range_str = request.args.get("range", "1d")
    interval = request.args.get("interval", "5m")
    results = {}
    for sym in DEFAULT_INDICES:
        data = fetch_yahoo_quote(sym, range_str, interval)
        if data:
            results[sym] = data
    return jsonify({"indices": results})


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
    """Add or remove symbols from watchlist with validation."""
    data = request.get_json(silent=True) or {}
    wl = load_watchlist()

    if data.get("add"):
        sym = data["add"].upper().strip()
        atype = data.get("type", "stock")
        # Check for duplicates
        for item in wl:
            if item["symbol"] == sym:
                return jsonify({"watchlist": wl, "status": "exists"})

        # Validate the symbol actually exists
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

    return jsonify({"watchlist": wl})


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
                        pub_str = entry_date.astimezone(timezone(timedelta(hours=-5))).strftime("%b %d, %I:%M %p EST")
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
        # Accuracy tracking: where AI and word-based disagreed
        c.execute("SELECT COUNT(*) FROM articles WHERE ai_sentiment != '' AND ai_sentiment IS NOT NULL AND word_sentiment != '' AND ai_sentiment != word_sentiment")
        disagreements = c.fetchone()[0]
        conn.close()
        return jsonify({
            "total_articles": total,
            "articles_fetched": fetched,
            "ai_analyzed": ai_analyzed,
            "sentiment_disagreements": disagreements,
            "by_sentiment": by_sentiment,
            "by_source": by_source,
            "by_keyword": by_keyword,
            "total_chats": chats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/recent")
def data_recent():
    """Get recent articles from the data store with full text for debugging."""
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
