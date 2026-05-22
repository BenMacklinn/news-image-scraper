# News Image Scraper

Paste a news article URL, scrape all images, and save them to a local folder.

## Setup

```bash
cd "/Users/benmock/Downloads/Image Scraper"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python app.py
```

Open http://127.0.0.1:5001

> **Note:** This app uses Playwright and local file storage. Run locally for full functionality (subscriber logins, image downloads). A Vercel deployment hosts the UI but scraping may not work in serverless.

## First-time login setup

1. Click **Set up logins** in the app (or visit http://127.0.0.1:5000/setup-login)
2. A browser window opens — log into NYT, WSJ, WaPo, etc.
3. Close the browser window when done

Your sessions are saved in `.browser-profile/` (local only, gitignored). Re-run setup if a session expires.

## Scrape

1. Paste an article URL
2. Click **Scrape images**
3. Images are saved to `downloads/{domain}_{timestamp}/`
4. Click **Reveal in Finder** to open the folder
