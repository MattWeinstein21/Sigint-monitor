# SIGINT — Keyword Monitor

Real-time keyword monitoring across Trump's social media accounts, major news outlets, and government sources. Built for traders who need instant alerts when market-moving keywords appear.

## Sources

| Source | Method | API Key Required |
|--------|--------|-----------------|
| Reuters, AP, CNBC, WSJ, Bloomberg, CNN, Fox, NYT | RSS feeds | No |
| White House | Web scrape | No |
| Truth Social | RSSHub proxy | No |
| X / Twitter | RSSHub proxy (free) or API v2 (paid) | Optional |
| 80,000+ news sources | NewsAPI | Yes (free tier) |

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/sigint-monitor.git
cd sigint-monitor

# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p static && cp index.html static/

# Run
python monitor.py
# Open http://localhost:5000
```

## Deploy to Server

```bash
# First time — on your server
cd /opt/sigint
git clone https://github.com/YOUR_USERNAME/sigint-monitor.git .
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p static && cp index.html static/
python -c "import monitor"  # generates config.json
nano config.json             # add API keys

# Create systemd service (see DEPLOY_STEPS.md)
# After that, updates are one command:
./deploy.sh
```

## Update Workflow

```
Local machine                    GitHub                     Server
─────────────                    ──────                     ──────
Edit code          →  git push  →  repo updated
                                                   ←  ./deploy.sh
                                                      (git pull + restart)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Frontend dashboard |
| `/api/alerts?limit=N` | GET | Get alerts |
| `/api/alerts` | DELETE | Clear all alerts |
| `/api/keywords` | GET | List keywords |
| `/api/keywords` | POST | Add/remove keyword |
| `/api/accounts` | GET | List monitored accounts |
| `/api/accounts` | POST | Add/remove account by URL |
| `/api/refresh` | POST | Trigger immediate poll |
| `/api/status` | GET | Service health check |
| `/metrics` | GET | Prometheus metrics |
