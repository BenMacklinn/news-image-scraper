import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent

_context_lock = threading.Lock()
_setup_running = False


def is_serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def storage_root() -> Path:
    if is_serverless():
        root = Path("/tmp/image-scraper")
    else:
        root = BASE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def profile_dir() -> Path:
    path = storage_root() / ".browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    path = storage_root() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_playwright() -> None:
    bundled = BASE_DIR / "pw-browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
        return

    if is_serverless():
        runtime_browsers = Path("/tmp/pw-browsers")
        runtime_browsers.mkdir(parents=True, exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_browsers)


def _ensure_playwright_browsers() -> None:
    _configure_playwright()
    browsers_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    if browsers_path.exists() and any(browsers_path.rglob("chrome*")):
        return

    if not is_serverless():
        return

    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        env=os.environ.copy(),
    )


def _ensure_dirs():
    profile_dir()
    downloads_dir()


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
    folder = downloads_dir() / f"{domain}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _resolve_folder(folder_id: str) -> Path:
    folder_id = folder_id.strip().removesuffix(".zip")
    if not folder_id or ".." in folder_id or "/" in folder_id:
        raise ValueError("Invalid folder id")

    folder = (downloads_dir() / folder_id).resolve()
    root = downloads_dir().resolve()
    if not str(folder).startswith(str(root)) or not folder.is_dir():
        raise ValueError("Folder not found")
    return folder


def _create_zip(folder: Path) -> Path:
    zip_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in folder.iterdir():
            if file_path.is_file():
                archive.write(file_path, arcname=file_path.name)
    return zip_path


def _launch_browser(playwright, headless: bool):
    _ensure_playwright_browsers()

    if is_serverless():
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        return browser, context

    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=headless,
        viewport={"width": 1280, "height": 900},
    )
    return None, context


def setup_login() -> dict:
    global _setup_running

    if is_serverless():
        return {
            "ok": False,
            "error": "Login setup only works locally. Run python app.py on your Mac, then use Set up logins.",
        }

    _ensure_dirs()

    with _context_lock:
        if _setup_running:
            return {"ok": False, "error": "Login setup is already running."}
        _setup_running = True

    try:
        with sync_playwright() as p:
            _, context = _launch_browser(p, headless=False)
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
    browser = None

    with _context_lock:
        with sync_playwright() as p:
            browser, context = _launch_browser(p, headless=True)
            page = context.new_page()
            try:
                page.goto(url, wait_until="load", timeout=45000)
            except Exception as exc:
                context.close()
                if browser:
                    browser.close()
                raise RuntimeError(f"Failed to load page: {exc}") from exc

            _scroll_page(page)
            page.wait_for_timeout(2000)
            image_urls = _extract_image_urls(page, page.url)
            context.close()
            if browser:
                browser.close()

    if not image_urls:
        message = "No images found."
        if is_serverless():
            message += " Subscriber articles require running the app locally with Set up logins."
        else:
            message += " Try Set up logins if the article is subscriber-only."
        return {"ok": False, "error": message}

    count = _download_images(image_urls, folder, referer=url)
    if count == 0:
        return {
            "ok": False,
            "error": "Found image URLs but failed to download any files.",
        }

    result = {
        "ok": True,
        "count": count,
        "folder": str(folder),
        "folder_id": folder.name,
        "serverless": is_serverless(),
    }

    if is_serverless():
        zip_path = _create_zip(folder)
        result["zip_url"] = f"/download/{folder.name}.zip"
        result["folder"] = f"{count} images ready to download"
        result["zip_path"] = str(zip_path)

    return result


def get_zip_file(folder_id: str) -> Path:
    folder = _resolve_folder(folder_id)
    zip_path = folder.with_suffix(".zip")
    if not zip_path.exists():
        zip_path = _create_zip(folder)
    return zip_path


def reveal_folder(folder_id: str) -> dict:
    if is_serverless():
        return {
            "ok": False,
            "error": "Reveal in Finder is not available on Vercel. Use Download zip instead.",
        }

    folder = _resolve_folder(folder_id)

    if sys.platform == "darwin":
        os.system(f'open "{folder}"')
    elif sys.platform == "win32":
        os.startfile(folder)  # noqa: S606
    else:
        os.system(f'xdg-open "{folder}"')

    return {"ok": True, "folder": str(folder)}
