# TradingWith-AI 🚀

India's AI-powered trading terminal — combining Zerodha, TradingView and Claude AI in one place.

## Files
- `index.html` — Main trading terminal
- `auth.html` — Login / Signup page
- `zerodha_bridge.py` — Local Zerodha bridge (run on your PC)

## Setup

### 1. Supabase (User Auth)
- Go to supabase.com → Create free project
- Copy Project URL + anon key
- Add to auth.html and index.html

### 2. Claude API
- Go to console.anthropic.com
- Create API key
- Enter in app settings

### 3. Zerodha Bridge (Local)
```bash
pip install flask flask-cors kiteconnect requests
python zerodha_bridge.py
```

## Deployment
Deployed on Vercel — auto-deploys on every GitHub push.

## Tech Stack
- Frontend: Vanilla HTML/CSS/JS
- Charts: TradingView Widget
- AI: Claude API (Anthropic)
- Auth: Supabase
- Broker: Zerodha Kite API
- Hosting: Vercel (free)

## Disclaimer
Educational platform only. Not SEBI registered.
