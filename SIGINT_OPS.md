# SIGINT Operations & Analytics Reference

## Server Access

```bash
# Enter the SIGINT container
pct enter 300

# Check service status
systemctl status sigint

# Restart service
systemctl restart sigint

# View live logs (Ctrl+C to exit)
journalctl -u sigint -f

# View last 50 log lines
journalctl -u sigint -n 50

# View logs from last hour
journalctl -u sigint --since "1 hour ago"

# View logs from today only
journalctl -u sigint --since today
```

## Deploy Changes

```bash
# From Mac:
cd ~/sigint-monitor/sigint-monitor
cp ~/Downloads/monitor.py ~/Downloads/index.html .
git add . && git commit -m "description" && git push

# On server:
pct enter 300
cd /opt/sigint && git pull origin main && cp index.html static/index.html && systemctl restart sigint
```

## API Endpoints — Diagnostics

All endpoints accessible at `http://10.0.0.230:5000` or via Tailscale.

### Health & Status
```bash
# Basic health check
curl http://localhost:5000/api/status

# View current config (redacted keys)
curl http://localhost:5000/api/config
```

### Source Polling Analytics
```bash
# Per-source polling stats (polls, alerts, errors, rates)
curl http://localhost:5000/api/data/source-stats | python3 -m json.tool

# What this shows:
# - total_polls: how many times source was checked
# - total_alerts: how many alerts it generated
# - total_errors: how many times it failed
# - error_rate: % of polls that errored
# - alerts_per_poll: average alerts per check
# - alerts_last_6h: recent activity
# - last_poll: when it was last checked
# - last_error_msg: most recent error text
```

### Alert Data
```bash
# Get all alerts (JSON)
curl http://localhost:5000/api/alerts?limit=300

# Count alerts by source
curl -s http://localhost:5000/api/alerts?limit=300 | python3 -c "
import json,sys,collections
data=json.load(sys.stdin)
c=collections.Counter(a['source'] for a in data.get('alerts',[]))
for src,cnt in c.most_common(): print(f'{cnt:4d} {src}')
"

# Count alerts by keyword
curl -s http://localhost:5000/api/alerts?limit=300 | python3 -c "
import json,sys,collections
data=json.load(sys.stdin)
c=collections.Counter(a.get('keyword','?') for a in data.get('alerts',[]))
for kw,cnt in c.most_common(): print(f'{cnt:4d} {kw}')
"
```

### Accuracy & Corrections
```bash
# View correction accuracy stats
curl http://localhost:5000/api/data/accuracy | python3 -m json.tool

# Apply recalibration (adjusts word weights based on corrections)
curl -X POST http://localhost:5000/api/data/apply-recalibration | python3 -m json.tool
```

### Market Data
```bash
# Watchlist with quotes
curl http://localhost:5000/api/market/watchlist | python3 -m json.tool

# Index data
curl "http://localhost:5000/api/market/indices?range=1d&interval=5m" | python3 -m json.tool

# SEC recent filings
curl http://localhost:5000/api/sec/recent | python3 -m json.tool

# Search SEC filings
curl "http://localhost:5000/api/sec/search?q=AAPL" | python3 -m json.tool
```

### Economy
```bash
# FRED dashboard (requires fred_api_key in config)
curl http://localhost:5000/api/economy/fred-dashboard | python3 -m json.tool

# Crypto trending
curl http://localhost:5000/api/crypto/trending | python3 -m json.tool

# Global crypto market
curl http://localhost:5000/api/crypto/global | python3 -m json.tool

# Fear & Greed
curl http://localhost:5000/api/market/fear-greed | python3 -m json.tool
```

### Wikipedia Context
```bash
# Test Wikipedia lookup for any entity
curl http://localhost:5000/api/wiki/summary/OPEC | python3 -m json.tool
curl http://localhost:5000/api/wiki/summary/Federal%20Reserve | python3 -m json.tool
```

### Ollama AI Status
```bash
# Check if Ollama is reachable
curl http://10.0.0.191:11434/api/tags | python3 -m json.tool

# Check which models are loaded
curl http://10.0.0.191:11434/api/ps | python3 -m json.tool
```

## SQLite Database

```bash
# Open the database
cd /opt/sigint
sqlite3 sigint_data.db

# Count articles by source
SELECT source_id, COUNT(*) as cnt FROM articles GROUP BY source_id ORDER BY cnt DESC;

# Recent corrections
SELECT url, user_corrected_sentiment, user_corrected_impact, corrected_at 
FROM articles WHERE user_corrected_sentiment IS NOT NULL 
ORDER BY corrected_at DESC LIMIT 20;

# AI analysis success rate
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN ai_analysis_raw IS NOT NULL THEN 1 ELSE 0 END) as ai_done,
  ROUND(SUM(CASE WHEN ai_analysis_raw IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as pct
FROM articles WHERE created_at > datetime('now', '-24 hours');

# Sentiment distribution (last 24h)
SELECT market_impact, COUNT(*) FROM articles 
WHERE created_at > datetime('now', '-24 hours')
GROUP BY market_impact;

# Exit sqlite
.quit
```

## Log Analysis One-Liners

```bash
# Count polls per cycle
journalctl -u sigint --since "1 hour ago" | grep "Polling cycle start" | wc -l

# Find errors
journalctl -u sigint --since "1 hour ago" | grep -i "error\|warning\|fail"

# Count AI enrichment completions
journalctl -u sigint --since "1 hour ago" | grep "AI enrichment" | wc -l

# Check if both AI workers are running
journalctl -u sigint --since "5 minutes ago" | grep "analysis\|chat_assist"

# Watch alert creation in real-time
journalctl -u sigint -f | grep "📈\|📉\|➡️"
```

## Source Stats File

The file `/opt/sigint/source_stats.json` persists polling data across restarts.

```bash
# Pretty-print source stats
cat /opt/sigint/source_stats.json | python3 -m json.tool

# Quick summary: which sources are active
python3 -c "
import json
d=json.load(open('/opt/sigint/source_stats.json'))
for s,v in sorted(d.items(), key=lambda x: x[1].get('total_alerts',0), reverse=True):
    print(f\"{v.get('total_alerts',0):5d} alerts | {v.get('total_polls',0):5d} polls | {v.get('error_rate',0):5.1f}% err | {s}\")
"
```

## GPU / Ollama Server (CT100)

```bash
# Enter GPU container
pct enter 100

# Check GPU status
nvidia-smi

# Check Ollama service
systemctl status ollama

# List loaded models
curl http://localhost:11434/api/ps

# Pull a new model
ollama pull llama3.1:8b

# Test a model directly
curl http://localhost:11434/api/generate -d '{"model":"llama3.1:latest","prompt":"Hello","stream":false}'
```

## Config File Reference

Location: `/opt/sigint/config.json`

Key fields:
- `fred_api_key`: FRED API key (free from fred.stlouisfed.org)
- `newsapi_key`: NewsAPI key
- `ollama_url`: Base Ollama URL (e.g. http://10.0.0.191:11434)
- `ollama_model`: Default model
- `ollama_chat_model`: Chat lane model (llama3.1)
- `ollama_analysis_model`: Analysis lane model (qwen3.5:9b)
- `poll_interval_seconds`: Polling frequency (default 120)
- `max_article_age_hours`: Max article age to consider (default 4)
- `keywords`: List of monitoring keywords
- `monitored_accounts`: Social media accounts to track
