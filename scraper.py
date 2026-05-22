import hashlib
import json
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MAX_IMAGES = 60
DOWNLOAD_TIMEOUT = 8
DOWNLOAD_WORKERS = 8

_login_lock = threading.Lock()
_login_running = False


def is_serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def storage_root() -> Path:
    root = Path("/tmp/image-scraper") if is_serverless() else BASE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def cookies_file() -> Path:
    return storage_root() / "cookies.json"


def profile_dir() -> Path:
    path = storage_root() / ".browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    path = storage_root() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL must start with http:// or https://")
    return url


def is_logged_in() -> bool:
    return cookies_file().exists()


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    path = cookies_file()
    if not path.exists():
        return session
    for cookie in json.loads(path.read_text()):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return session


def setup_login() -> dict:
    global _login_running

    if is_serverless():
        return {
            "ok": False,
            "error": "Login only works when running locally (python app.py on your Mac).",
        }

    with _login_lock:
        if _login_running:
            return {"ok": False, "error": "Login is already in progress."}
        _login_running = True

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "Run: pip install playwright && playwright install chromium",
        }

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir()),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("about:blank")
            page.evaluate(
                """() => {
                document.body.innerHTML =
                    '<div style="font-family:sans-serif;padding:40px;max-width:560px;line-height:1.5">'
                    + '<h1>Log in to your news sites</h1>'
                    + '<p>Open new tabs and sign in to NYT, WSJ, WaPo, etc.</p>'
                    + '<p>When you are done, close this browser window.</p>'
                    + '</div>';
            }"""
            )
            while context.pages:
                time.sleep(0.5)
            cookies = context.cookies()
            context.close()

        cookies_file().write_text(json.dumps(cookies, indent=2))
        return {
            "ok": True,
            "message": f"Login saved ({len(cookies)} cookies). You can scrape subscriber articles now.",
        }
    finally:
        with _login_lock:
            _login_running = False


def _best_from_srcset(srcset: str):
    best_url = None
    best_score = -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        descriptor = bits[1] if len(bits) > 1 else ""
        if descriptor.endswith("w"):
            score = int(descriptor[:-1]) if descriptor[:-1].isdigit() else 0
        elif descriptor.endswith("x"):
            try:
                score = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                score = 500
        else:
            score = 500
        if score >= best_score:
            best_score = score
            best_url = url
    return best_url


def _normalize_image_key(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/\d+x\d+/", "/", parsed.path)
    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path,
        re.I,
    )
    if uuid_match:
        return uuid_match.group(0).lower()
    return f"{parsed.netloc}{path.split('?')[0].lower()}"


def _img_url(img):
    src = img.get("src") or ""
    if src and not src.startswith("data:"):
        return src
    srcset = img.get("srcset")
    if srcset:
        return _best_from_srcset(srcset)
    for attr in ("data-src", "data-lazy-src", "data-original"):
        value = img.get(attr)
        if value and not value.startswith("data:"):
            return value
    return None


def _extract_image_urls(html: str, base_url: str) -> list[str]:
    """Like Inspect Element: only <img> and <picture> tags."""
    soup = BeautifulSoup(html, "html.parser")
    raw_urls = []

    for img in soup.find_all("img"):
        url = _img_url(img)
        if url:
            raw_urls.append(url)

    for source in soup.find_all("picture source"):
        srcset = source.get("srcset")
        if srcset:
            best = _best_from_srcset(srcset)
            if best:
                raw_urls.append(best)

    absolute = []
    seen = set()
    for url in raw_urls:
        full = urljoin(base_url, url.strip())
        if full.startswith("data:"):
            continue
        key = _normalize_image_key(full)
        if key not in seen:
            seen.add(key)
            absolute.append(full)
    return absolute[:MAX_IMAGES]


def _fetch_page(url: str) -> tuple[str, str, requests.Session]:
    session = _build_session()
    response = session.get(url, timeout=20, allow_redirects=True)
    response.raise_for_status()
    return response.text, str(response.url), session


def _safe_filename(url: str, index: int) -> str:
    path = urlparse(url).path
    name = os.path.basename(path.split("?")[0]) or f"image_{index:03d}"
    name = re.sub(r"[^\w.\-]", "_", name)
    if not re.search(r"\.(jpe?g|png|gif|webp|avif|svg|bmp)$", name, re.I):
        name = f"{name}.jpg"
    return name


def _download_one(url: str, referer: str, cookie_jar) -> tuple:
    try:
        resp = requests.get(
            url,
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            cookies=cookie_jar,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if content_type and not content_type.startswith("image/"):
            return None
        if len(resp.content) < 1000:
            return None
        return url, resp.content
    except requests.RequestException:
        return None


def _download_images(image_urls: list[str], folder: Path, referer: str, session: requests.Session) -> int:
    used_names = set()
    seen_hashes = set()
    count = 0
    cookie_jar = session.cookies

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(_download_one, url, referer, cookie_jar) for url in image_urls]
        for i, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if not result:
                continue
            url, content = result
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
            (folder / filename).write_bytes(content)
            count += 1
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


def scrape_images(url: str) -> dict:
    url = validate_url(url)
    folder = _make_output_folder(url)

    try:
        html, final_url, session = _fetch_page(url)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to load page: {exc}") from exc

    image_urls = _extract_image_urls(html, final_url)
    if not image_urls:
        message = "No <img> tags found on the page."
        if not is_logged_in():
            message += " Click Log in first for subscriber articles."
        return {"ok": False, "error": message}

    count = _download_images(image_urls, folder, referer=url, session=session)
    if count == 0:
        return {"ok": False, "error": "Found image URLs but could not download any."}

    result = {
        "ok": True,
        "count": count,
        "folder": str(folder),
        "folder_id": folder.name,
        "serverless": is_serverless(),
    }
    if is_serverless():
        _create_zip(folder)
        result["zip_url"] = f"/download/{folder.name}.zip"
        result["folder"] = f"{count} images ready to download"
    return result


def get_zip_file(folder_id: str) -> Path:
    folder = _resolve_folder(folder_id)
    zip_path = folder.with_suffix(".zip")
    if not zip_path.exists():
        zip_path = _create_zip(folder)
    return zip_path


def reveal_folder(folder_id: str) -> dict:
    if is_serverless():
        return {"ok": False, "error": "Use Download zip on Vercel."}
    folder = _resolve_folder(folder_id)
    if sys.platform == "darwin":
        os.system(f'open "{folder}"')
    elif sys.platform == "win32":
        os.startfile(folder)  # noqa: S606
    else:
        os.system(f'xdg-open "{folder}"')
    return {"ok": True, "folder": str(folder)}
