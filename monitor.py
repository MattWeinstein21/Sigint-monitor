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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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

    def _hash(self, source_id, title, url):
        dedupe_basis = url.strip() if url else f"{source_id}:{title.strip().lower()}"
        return hashlib.md5(dedupe_basis.encode()).hexdigest()

    def add(self, source_id, source_name, title, url, snippet, keyword, severity="medium"):
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
        )

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
                "positive_signals": sentiment["positive_signals"],
                "negative_signals": sentiment["negative_signals"],
                "article_fetched": fetched,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.alerts.insert(0, alert)
            if len(self.alerts) > self.max_alerts:
                self.alerts = self.alerts[: self.max_alerts]
            emoji = {"positive": "📈", "negative": "📉", "neutral": "➡️"}.get(sentiment["sentiment"], "➡️")
            log.info(f"{emoji} [{source_id}] {sentiment['sentiment']}({sentiment['score']:+.1f}) keyword='{keyword}' → {title[:70]}")
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

# Sentiment word lists — weighted for financial/political context
POSITIVE_SIGNALS = {
    # Strong positive
    "surge": 3, "soar": 3, "rally": 3, "boom": 3, "skyrocket": 3,
    "breakthrough": 3, "historic deal": 4, "record high": 3,
    # Moderate positive
    "gain": 2, "rise": 2, "climb": 2, "jump": 2, "advance": 2,
    "recover": 2, "rebound": 2, "uptick": 2, "bullish": 2,
    "growth": 2, "expand": 2, "strong": 1.5, "boost": 2,
    "optimism": 2, "confidence": 1.5, "upgrade": 2, "approve": 2,
    "agreement": 2, "deal": 1.5, "cooperat": 1.5, "peace": 2,
    "cut taxes": 3, "tax cut": 3, "rate cut": 2.5, "stimulus": 2.5,
    "deregulat": 2, "ease restrictions": 2, "lift sanctions": 2,
    "beat expectations": 3, "exceed": 2, "outperform": 2,
    "profit": 1.5, "revenue growth": 2, "jobs added": 2,
    "unemployment fell": 3, "unemployment low": 2,
    "bipartisan": 1.5, "unanimously": 2, "signed into law": 2,
}

NEGATIVE_SIGNALS = {
    # Strong negative
    "crash": 4, "plunge": 4, "collapse": 4, "plummet": 4, "freefall": 4,
    "crisis": 3, "catastroph": 3, "recession": 3, "depression": 3,
    "default": 3, "bankrupt": 3, "insolven": 3,
    # Moderate negative
    "drop": 2, "fall": 1.5, "decline": 2, "slip": 1.5, "tumble": 2.5,
    "selloff": 2.5, "sell-off": 2.5, "bearish": 2, "downturn": 2,
    "loss": 1.5, "deficit": 1.5, "debt": 1, "inflation": 1.5,
    "warn": 2, "threat": 2, "fear": 2, "concern": 1.5, "uncertain": 1.5,
    "volatil": 1.5, "risk": 1, "panic": 3,
    "sanction": 2, "tariff": 1.5, "trade war": 2.5, "ban": 2,
    "restrict": 2, "penalt": 2, "fine": 1.5, "investigat": 1.5,
    "indict": 3, "lawsuit": 2, "subpoena": 2,
    "shutdown": 2.5, "layoff": 2.5, "cut jobs": 2.5, "downsiz": 2,
    "missed expectations": 3, "underperform": 2, "downgrade": 2.5,
    "impeach": 2, "veto": 2, "block": 1.5, "reject": 2, "oppose": 1.5,
    "escalat": 2, "retaliat": 2.5, "war": 2, "conflict": 2, "invasion": 3,
    "unemployment rose": 3, "unemployment high": 2,
}

AMPLIFIERS = {
    "breaking": 1.5, "just in": 1.5, "alert": 1.3, "urgent": 1.5,
    "exclusive": 1.3, "developing": 1.2, "major": 1.3,
    "immediately": 1.4, "effective immediately": 1.6,
    "executive order": 1.4, "signed": 1.2,
}

# Keyword combos that signal high market impact
HIGH_IMPACT_COMBOS = [
    ({"tariff", "china"}, 2.0),
    ({"tariff", "eu"}, 1.8),
    ({"tariff", "canada"}, 1.8),
    ({"tariff", "mexico"}, 1.8),
    ({"rate", "cut"}, 1.8),
    ({"rate", "hike"}, 1.8),
    ({"executive order"}, 1.5),
    ({"fed", "rate"}, 1.7),
    ({"sanctions", "russia"}, 1.6),
    ({"sanctions", "iran"}, 1.6),
    ({"sanctions", "china"}, 1.8),
    ({"ban", "import"}, 1.7),
    ({"trade", "deal"}, 1.6),
    ({"debt", "ceiling"}, 1.8),
    ({"government", "shutdown"}, 1.9),
    ({"nuclear"}, 1.5),
    ({"war"}, 1.5),
    ({"invasion"}, 1.8),
    ({"default"}, 2.0),
    ({"recession"}, 1.8),
]


def fetch_article_text(url, timeout=10):
    """
    Attempt to fetch and extract the main text from an article URL.
    Returns (text, success). Gracefully fails for paywalled/blocked content.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return "", False

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove scripts, styles, nav, footer, ads
        for tag in soup.find_all(["script", "style", "nav", "footer", "aside",
                                   "iframe", "noscript", "header"]):
            tag.decompose()

        # Try common article containers
        article = None
        for selector in ["article", '[role="main"]', ".article-body",
                         ".story-body", ".post-content", ".entry-content",
                         ".article-content", "#article-body", ".body-text"]:
            article = soup.select_one(selector)
            if article:
                break

        if article:
            text = article.get_text(" ", strip=True)
        else:
            # Fallback: grab all <p> tags
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(" ", strip=True) for p in paragraphs)

        text = normalize_whitespace(text)

        # If we got very little text, it's probably paywalled
        if len(text) < 100:
            return "", False

        # Cap at ~3000 chars to keep analysis fast
        return text[:3000], True

    except Exception as e:
        log.debug(f"Article fetch failed [{url[:60]}]: {e}")
        return "", False


def analyze_sentiment(text, source_id="", matched_keywords=None):
    """
    Analyze sentiment of text. Returns dict with:
      - sentiment: "positive" | "negative" | "neutral"
      - score: float from -10 to +10
      - confidence: float 0-1
      - summary: brief auto-generated summary
      - market_signals: list of detected signal words
    """
    text_lower = text.lower()
    matched_keywords = matched_keywords or []

    pos_score = 0.0
    neg_score = 0.0
    pos_signals = []
    neg_signals = []

    # Score positive signals
    for word, weight in POSITIVE_SIGNALS.items():
        pattern = re.compile(rf"(?<!\w){re.escape(word)}", re.IGNORECASE)
        matches = pattern.findall(text_lower)
        if matches:
            pos_score += weight * len(matches)
            pos_signals.append(word)

    # Score negative signals
    for word, weight in NEGATIVE_SIGNALS.items():
        pattern = re.compile(rf"(?<!\w){re.escape(word)}", re.IGNORECASE)
        matches = pattern.findall(text_lower)
        if matches:
            neg_score += weight * len(matches)
            neg_signals.append(word)

    # Apply amplifiers
    amplifier = 1.0
    for word, mult in AMPLIFIERS.items():
        if word.lower() in text_lower:
            amplifier = max(amplifier, mult)

    # Apply keyword combo bonuses
    kw_set = set(k.lower() for k in matched_keywords)
    combo_mult = 1.0
    for combo_words, mult in HIGH_IMPACT_COMBOS:
        if combo_words.issubset(kw_set) or all(w in text_lower for w in combo_words):
            combo_mult = max(combo_mult, mult)

    # Apply source weight
    src_weight = SOURCE_WEIGHTS.get(source_id, 0.5)

    # Calculate raw score (-10 to +10)
    raw = (pos_score - neg_score) * amplifier * combo_mult * src_weight
    score = max(-10.0, min(10.0, raw))

    # Determine sentiment
    if score > 1.5:
        sentiment = "positive"
    elif score < -1.5:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    # Confidence based on signal density
    total_signals = len(pos_signals) + len(neg_signals)
    text_len = max(len(text.split()), 1)
    confidence = min(1.0, (total_signals / text_len) * 10 + (abs(score) / 10) * 0.5)
    confidence = round(confidence, 2)

    # Auto-generate summary from first 2 sentences
    sentences = re.split(r'(?<=[.!?])\s+', text[:500])
    summary = " ".join(sentences[:2]).strip()
    if len(summary) > 200:
        summary = summary[:197] + "..."

    # Severity based on composite score
    abs_score = abs(score)
    if abs_score >= 5:
        severity = "high"
    elif abs_score >= 2:
        severity = "medium"
    else:
        severity = "low"

    return {
        "sentiment": sentiment,
        "score": round(score, 2),
        "confidence": confidence,
        "severity": severity,
        "summary": summary,
        "positive_signals": pos_signals[:5],
        "negative_signals": neg_signals[:5],
    }


def cleaned_entry_text(*parts):
    return normalize_whitespace(BeautifulSoup(" ".join([p for p in parts if p]), "html.parser").get_text(" "))


def poll_rss(keywords):
    """Poll all configured RSS feeds for keyword matches."""
    for source_id, source in RSS_SOURCES.items():
        for feed_url in source["feeds"]:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:
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
    while True:
        try:
            config = load_config()
            keywords = [normalize_whitespace(k).lower() for k in config.get("keywords", []) if normalize_whitespace(k)]
            interval = config.get("poll_interval_seconds", 120)

            # Split accounts by platform
            all_accounts = config.get("monitored_accounts", [])
            x_accounts = [a for a in all_accounts if a.get("platform") == "x"]
            ts_accounts = [a for a in all_accounts if a.get("platform") == "truthsocial"]

            log.info(f"Monitor cycle — {len(keywords)} keywords, {len(x_accounts)} X accounts, {len(ts_accounts)} Truth Social accounts")
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
