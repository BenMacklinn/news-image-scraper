# News Image Scraper

Pull `<img>` tags from a page (like Inspect Element) and save them to a folder.

## Setup

```bash
cd "/Users/benmock/Downloads/Image Scraper"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install playwright && playwright install chromium   # for Log in only
```

## Run locally

```bash
python app.py
```

Open http://127.0.0.1:5001

## Usage

1. Click **Log in** — browser opens, sign into NYT / WSJ / WaPo, close the window
2. Paste an article URL and click **Scrape images**
3. Images save to `downloads/{domain}_{timestamp}/`

Login uses a real browser once. Scraping is fast HTTP — no Chromium per scrape.

## Vercel

https://news-image-scraper.vercel.app — public pages only (no login on serverless).
