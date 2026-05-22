import hashlib
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / ".browser-profile"
DOWNLOADS_DIR = BASE_DIR / "downloads"

_context_lock = threading.Lock()
_setup_running = False


def _ensure_dirs():
    PROFILE_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)


def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL must start with http:// or https://")
    return url


def _normalize_image_key(url: str) -> str:
    """Group resize/crop variants of the same photo under one key."""
    parsed = urlparse(url)
    path = re.sub(r"/\d+x\d+/", "/", parsed.path)
    path = re.sub(r"[-_]\d+w(?=\.)", "", path, flags=re.I)
    path = re.sub(r"[-_]\d+h(?=\.)", "", path, flags=re.I)

    uuid_match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", path, re.I)
    if uuid_match:
        return uuid_match.group(0).lower()

    return f"{parsed.netloc}{path.split('?')[0].lower()}"


def _extract_image_urls(page, base_url: str) -> list[str]:
    urls = page.evaluate(
        """() => {
        const found = [];
        const seen = new Set();
        const add = (url) => {
            if (!url || url.startsWith('data:') || seen.has(url)) return;
            seen.add(url);
            found.push(url);
        };
        const bestFromSrcset = (srcset) => {
            if (!srcset) return null;
            let best = null;
            let bestScore = -1;
            for (const part of srcset.split(',')) {
                const bits = part.trim().split(/\\s+/);
                const url = bits[0];
                const descriptor = bits[1] || '';
                let score = 0;
                if (descriptor.endsWith('w')) score = parseInt(descriptor, 10) || 0;
                else if (descriptor.endsWith('x')) score = (parseFloat(descriptor) || 1) * 1000;
                else score = 500;
                if (score >= bestScore) {
                    bestScore = score;
                    best = url;
                }
            }
            return best;
        };

        document.querySelectorAll('img').forEach((img) => {
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            if (w > 0 && h > 0 && (w < 120 || h < 120)) return;

            if (img.currentSrc) {
                add(img.currentSrc);
                return;
            }
            const fromSrcset = bestFromSrcset(img.srcset);
            if (fromSrcset) add(fromSrcset);
            else if (img.src) add(img.src);
        });

        return found;
    }"""
    )

    absolute = []
    seen_keys = set()
    for url in urls:
        full = urljoin(base_url, url)
        key = _normalize_image_key(full)
        if key not in seen_keys:
            seen_keys.add(key)
            absolute.append(full)
    return absolute


def _scroll_page(page):
    page.evaluate(
        """async () => {
        const delay = (ms) => new Promise((r) => setTimeout(r, ms));
        const height = document.body.scrollHeight;
        const steps = 5;
        for (let i = 1; i <= steps; i++) {
            window.scrollTo(0, (height / steps) * i);
            await delay(400);
        }
        window.scrollTo(0, 0);
    }"""
    )


def _safe_filename(url: str, index: int) -> str:
    path = urlparse(url).path
    name = os.path.basename(path.split("?")[0]) or f"image_{index:03d}"
    name = re.sub(r"[^\w.\-]", "_", name)
    if not re.search(r"\.(jpe?g|png|gif|webp|avif|svg|bmp)$", name, re.I):
        name = f"{name}.jpg"
    return name


def _download_images(image_urls: list[str], folder: Path, referer: str) -> int:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
        }
    )

    used_names = set()
    seen_hashes = set()
    count = 0

    for i, url in enumerate(image_urls, start=1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if content_type and not content_type.startswith("image/"):
                continue

            content = resp.content
            content_hash = hashlib.sha256(content).hexdigest()
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            filename = _safe_filename(url, i)
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            while filename in used_names:
                filename = f"{stem}_{i:03d}{suffix}"
            used_names.add(filename)

            dest = folder / filename
            dest.write_bytes(content)
            count += 1
        except requests.RequestException:
            continue

    return count


def _make_output_folder(url: str) -> Path:
    domain = urlparse(url).netloc.replace("www.", "")
    domain = re.sub(r"[^\w.\-]", "_", domain)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = DOWNLOADS_DIR / f"{domain}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def setup_login() -> dict:
    global _setup_running

    _ensure_dirs()

    with _context_lock:
        if _setup_running:
            return {"ok": False, "error": "Login setup is already running."}
        _setup_running = True

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("about:blank")
            page.evaluate(
                """() => {
                document.body.innerHTML = '<div style="font-family:sans-serif;padding:40px;max-width:600px">'
                    + '<h1>Login setup</h1>'
                    + '<p>Log into NYT, WSJ, WaPo, and other sites in new tabs.</p>'
                    + '<p>When finished, close this browser window.</p>'
                    + '</div>';
            }"""
            )
            while context.pages:
                time.sleep(0.5)
            try:
                context.close()
            except Exception:
                pass

        return {"ok": True, "message": "Login setup complete. Sessions saved."}
    finally:
        with _context_lock:
            _setup_running = False


def scrape_images(url: str) -> dict:
    _ensure_dirs()
    url = validate_url(url)

    folder = _make_output_folder(url)

    with _context_lock:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="load", timeout=45000)
            except Exception as exc:
                context.close()
                raise RuntimeError(f"Failed to load page: {exc}") from exc

            _scroll_page(page)
            page.wait_for_timeout(2000)

            image_urls = _extract_image_urls(page, page.url)
            context.close()

    if not image_urls:
        return {
            "ok": False,
            "error": "No images found. Try Set up logins if the article is subscriber-only.",
        }

    count = _download_images(image_urls, folder, referer=url)
    if count == 0:
        return {
            "ok": False,
            "error": "Found image URLs but failed to download any files.",
        }

    folder_id = folder.name
    return {
        "ok": True,
        "count": count,
        "folder": str(folder),
        "folder_id": folder_id,
    }


def reveal_folder(folder_id: str) -> dict:
    folder_id = folder_id.strip()
    if not folder_id or ".." in folder_id or "/" in folder_id:
        raise ValueError("Invalid folder id")

    folder = (DOWNLOADS_DIR / folder_id).resolve()
    downloads_root = DOWNLOADS_DIR.resolve()

    if not str(folder).startswith(str(downloads_root)) or not folder.is_dir():
        raise ValueError("Folder not found")

    if sys.platform == "darwin":
        os.system(f'open "{folder}"')
    elif sys.platform == "win32":
        os.startfile(folder)  # noqa: S606
    else:
        os.system(f'xdg-open "{folder}"')

    return {"ok": True, "folder": str(folder)}
