# News Image Scraper

Paste a news article URL, scrape all images, and save them to a local folder.

Uses plain HTTP + HTML parsing (no browser). Fast on local and Vercel.

## Setup

```bash
cd "/Users/benmock/Downloads/Image Scraper"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run locally

```bash
python app.py
```

Open http://127.0.0.1:5001

## Subscriber sites (NYT, WSJ, WaPo)

1. Log into the site in your normal browser
2. Export cookies with a browser extension (EditThisCookie, Cookie-Editor, etc.)
3. Expand **Subscriber cookies** in the app, paste the JSON array, click **Save cookies**
4. Scrape article URLs — requests use your saved session

## Scrape

1. Paste an article URL
2. Click **Scrape images**
3. Images save to `downloads/{domain}_{timestamp}/`
4. On Vercel, click **Download zip** instead

## Deploy

Connected to Vercel at https://news-image-scraper.vercel.app

Note: cookies saved on Vercel are stored in `/tmp` and reset when the serverless function cold-starts. For subscriber sites, saving cookies locally is more reliable.
