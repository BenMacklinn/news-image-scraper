import hashlib
import json
import os
import re
import sys
import zipfile
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

SKIP_URL_PATTERN = re.compile(
    r"(favicon|pixel\.gif|1x1|spacer|tracking|doubleclick|facebook\.com/tr|"
    r"google-analytics|scorecardresearch|beacon|/ads/|/ad/|advert)",
    re.I,
)

IMAGE_URL_IN_TEXT = re.compile(
    r'https?://[^\s"\'<>\\]+?\.(?:jpe?g|png|gif|webp|avif|bmp|svg)(?:\?[^\s"\'<>\\]*)?',
    re.I,
)

CDN_IMAGE_IN_TEXT = re.compile(
    r'https?://[^\s"\'<>\\]*(?:images?|media|static|cdn|graphics|photos?|'
    r"img|dam\.|cloudfront)[^\s\"'<>\\]*",
    re.I,
)

BACKGROUND_IMAGE = re.compile(
    r"""background-image:\s*url\(\s*['"]?([^'")]+)['"]?\s*\)""",
    re.I,
)

LAZY_ATTRS = (
    "src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-image",
    "data-url",
    "data-hi-res-src",
    "data-full-src",
    "data-pin-media",
    "data-image-url",
    "data-srcset",
    "data-lazy-srcset",
    "data-original-src",
    "data-retina-src",
)

JSON_IMAGE_KEYS = (
    "url",
    "contentUrl",
    "thumbnailUrl",
    "image",
    "src",
    "uri",
    "href",
    "imageUrl",
    "mediaUrl",
    "previewImage",
    "fullImage",
    "defaultImage",
    "largeImage",
    "promoImage",
    "poster",
    "thumbnail",
    "photo",
)


def is_serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def storage_root() -> Path:
    if is_serverless():
        root = Path("/tmp/image-scraper")
    else:
        root = BASE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def cookies_file() -> Path:
    return storage_root() / "cookies.json"


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


def save_cookies(cookies_json: str) -> dict:
    try:
        data = json.loads(cookies_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}

    if not isinstance(data, list):
        return {"ok": False, "error": "Paste a JSON array of cookie objects."}

    for i, cookie in enumerate(data):
        if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
            return {"ok": False, "error": f"Cookie #{i + 1} must have name and value."}

    cookies_file().write_text(json.dumps(data, indent=2))
    return {"ok": True, "message": f"Saved {len(data)} cookies for subscriber sites."}


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


def _all_from_srcset(srcset: str) -> list[str]:
    urls = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if bits:
            urls.append(bits[0])
    return urls


def _looks_like_image_url(url: str) -> bool:
    if not url or url.startswith("data:"):
        return False
    if SKIP_URL_PATTERN.search(url):
        return False

    lower = url.lower()
    if re.search(r"\.(jpe?g|png|gif|webp|avif|svg|bmp)(\?|$)", url, re.I):
        return True

    cdn_hints = (
        "/image", "/images/", "/photo", "/photos/", "/media/", "/picture",
        "/graphics/", "/dam/", "images.", "media.", "static.", "cdn.",
        "img.", "cloudfront.net", "wp.com/wp-content/uploads",
    )
    return any(hint in lower for hint in cdn_hints)


def _normalize_image_key(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/\d+x\d+/", "/", parsed.path)
    path = re.sub(r"[-_]\d+w(?=\.)", "", path, flags=re.I)
    path = re.sub(r"[-_]\d+h(?=\.)", "", path, flags=re.I)

    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path,
        re.I,
    )
    if uuid_match:
        return uuid_match.group(0).lower()

    return f"{parsed.netloc}{path.split('?')[0].lower()}"


def _collect_json_images(obj, urls: list[str]) -> None:
    if isinstance(obj, dict):
        for key in JSON_IMAGE_KEYS:
            if key not in obj:
                continue
            value = obj[key]
            if isinstance(value, str) and _looks_like_image_url(value):
                urls.append(value)
            elif isinstance(value, dict):
                nested = value.get("url") or value.get("contentUrl") or value.get("uri")
                if isinstance(nested, str) and _looks_like_image_url(nested):
                    urls.append(nested)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and _looks_like_image_url(item):
                        urls.append(item)
                    elif isinstance(item, dict):
                        nested = item.get("url") or item.get("contentUrl") or item.get("uri")
                        if isinstance(nested, str) and _looks_like_image_url(nested):
                            urls.append(nested)
        for value in obj.values():
            _collect_json_images(value, urls)
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_images(item, urls)


def _extract_urls_from_text(text: str, urls: list[str]) -> None:
    for match in IMAGE_URL_IN_TEXT.findall(text):
        urls.append(match)
    for match in CDN_IMAGE_IN_TEXT.findall(text):
        if _looks_like_image_url(match):
            urls.append(match.rstrip("\\)]};,"))


def _dedupe_urls(raw_urls: list[str], base_url: str) -> list[str]:
    absolute: list[str] = []
    seen_keys: set[str] = set()
    for url in raw_urls:
        url = url.strip().strip("'\"")
        if not url or url.startswith("data:"):
            continue
        full = urljoin(base_url, url)
        if not _looks_like_image_url(full):
            continue
        key = _normalize_image_key(full)
        if key not in seen_keys:
            seen_keys.add(key)
            absolute.append(full)
    return absolute


def _extract_image_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    raw_urls: list[str] = []

    def add(url):
        if url:
            raw_urls.append(url.strip().strip("'\""))

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key.startswith("og:image") or key.startswith("twitter:image") or key == "thumbnail":
            add(meta.get("content"))

    for link in soup.find_all("link", rel=True):
        rel = " ".join(link.get("rel", [])).lower()
        if "preload" in rel and link.get("as") == "image":
            add(link.get("href"))
        if "image_src" in rel:
            add(link.get("href"))

    for tag in soup.find_all(["img", "amp-img", "amp-img-lightbox"]):
        for attr in LAZY_ATTRS:
            value = tag.get(attr)
            if not value:
                continue
            if "srcset" in attr:
                for src in _all_from_srcset(value):
                    add(src)
            else:
                add(value)
        for attr, value in tag.attrs.items():
            if not attr.startswith("data-") or not isinstance(value, str):
                continue
            if "srcset" in attr:
                for src in _all_from_srcset(value):
                    add(src)
            elif value.startswith("http") or value.startswith("//"):
                add(value)

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for src in _all_from_srcset(srcset):
                add(src)
        add(source.get("src"))

    for video in soup.find_all("video"):
        add(video.get("poster"))

    for tag in soup.find_all(style=True):
        for match in BACKGROUND_IMAGE.findall(tag.get("style", "")):
            add(match)

    for style in soup.find_all("style"):
        if style.string:
            for match in BACKGROUND_IMAGE.findall(style.string):
                add(match)

    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        if not content:
            continue
        if script.get("type") == "application/ld+json":
            try:
                _collect_json_images(json.loads(content), raw_urls)
            except json.JSONDecodeError:
                pass
            continue
        if script.get("id") in ("__NEXT_DATA__", "__PRELOADED_STATE__", "__NUXT__"):
            try:
                _collect_json_images(json.loads(content), raw_urls)
            except json.JSONDecodeError:
                pass
        _extract_urls_from_text(content, raw_urls)
        for json_match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content):
            snippet = json_match.group(0)
            if not any(ext in snippet.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", "image")):
                continue
            try:
                _collect_json_images(json.loads(snippet), raw_urls)
            except json.JSONDecodeError:
                pass

    _extract_urls_from_text(html, raw_urls)
    return _dedupe_urls(raw_urls, base_url)


def _fetch_page(url: str) -> tuple[str, str, requests.Session]:
    session = _build_session()
    response = session.get(url, timeout=30, allow_redirects=True)
    response.raise_for_status()
    return response.text, str(response.url), session


def _safe_filename(url: str, index: int) -> str:
    path = urlparse(url).path
    name = os.path.basename(path.split("?")[0]) or f"image_{index:03d}"
    name = re.sub(r"[^\w.\-]", "_", name)
    if not re.search(r"\.(jpe?g|png|gif|webp|avif|svg|bmp)$", name, re.I):
        name = f"{name}.jpg"
    return name


def _download_images(
    image_urls: list[str],
    folder: Path,
    referer: str,
    session: requests.Session,
) -> int:
    session.headers["Referer"] = referer
    used_names: set[str] = set()
    seen_hashes: set[str] = set()
    count = 0

    for i, url in enumerate(image_urls, start=1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if content_type and not content_type.startswith("image/"):
                continue

            content = resp.content
            if len(content) < 1500:
                continue

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


def scrape_images(url: str) -> dict:
    url = validate_url(url)
    folder = _make_output_folder(url)

    try:
        html, final_url, session = _fetch_page(url)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to load page: {exc}") from exc

    image_urls = _extract_image_urls(html, final_url)
    if not image_urls:
        has_cookies = cookies_file().exists()
        message = "No images found in page HTML."
        if not has_cookies:
            message += " Paste subscriber cookies below for NYT/WSJ/WaPo articles."
        return {"ok": False, "error": message}

    count = _download_images(image_urls, folder, referer=url, session=session)
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
