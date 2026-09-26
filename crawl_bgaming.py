from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests


BASE_URL = "https://bgaming.com"
CATALOG_URL = f"{BASE_URL}/game-type/slots"
API_URL = f"{BASE_URL}/wp-json/bg/v1/games/search"
DEFAULT_OUTPUT = Path("data") / "providers" / "bgaming"
DEFAULT_TYPES = "slots"
DEMO_HOSTS = {"bgaming-network.com", "demo.bgaming-network.com"}


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


def _thumbnail_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
        return suffix
    return ".img"


class _CatalogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._depth = 0
        self._card: dict[str, str] | None = None
        self._capture_heading = False
        self._heading_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = self._attrs(attrs)

        if self._card is None:
            if tag == "div" and "data-catalog-card" in data:
                self._card = {
                    "name": "",
                    "page_url": "",
                    "thumbnail_url": html.unescape(data.get("data-image", "")).strip(),
                }
                self._depth = 1
            return

        if tag == "div":
            self._depth += 1
            classes = set(data.get("class", "").split())
            if "heading-35" in classes and "also_like" in classes:
                self._capture_heading = True
                self._heading_parts = []

        if tag == "a" and not self._card["page_url"]:
            href = html.unescape(data.get("href", "")).strip()
            if href.startswith(f"{BASE_URL}/games/"):
                self._card["page_url"] = href

        if tag == "img":
            alt = " ".join(html.unescape(data.get("alt", "")).split())
            if alt and not self._card["name"]:
                self._card["name"] = alt
            if not self._card["thumbnail_url"]:
                self._card["thumbnail_url"] = html.unescape(data.get("src", "")).strip()

    def handle_data(self, data: str) -> None:
        if self._capture_heading:
            self._heading_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return

        if tag == "div" and self._capture_heading:
            name = " ".join(html.unescape("".join(self._heading_parts)).split())
            if name and not self._card["name"]:
                self._card["name"] = name
            self._capture_heading = False
            self._heading_parts = []

        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                if self._card["page_url"] and self._card["name"]:
                    self.cards.append(self._card)
                self._card = None


def _parse_catalog_html(value: str) -> list[dict[str, str]]:
    parser = _CatalogParser()
    parser.feed(value)
    parser.close()
    return parser.cards


_COPY_RE = re.compile(
    r'<button\b[^>]*\bdata-copy=(?P<q>["\'])(?P<url>.*?)(?P=q)[^>]*\bcopy-btn\b'
    r'|<button\b[^>]*\bcopy-btn\b[^>]*\bdata-copy=(?P<q2>["\'])(?P<url2>.*?)(?P=q2)',
    re.IGNORECASE | re.DOTALL,
)
_IFRAME_RE = re.compile(
    r'\bdata-iframe-src=(?P<q>["\'])(?P<url>.*?)(?P=q)',
    re.IGNORECASE | re.DOTALL,
)


def _clean_demo_url(value: str) -> str:
    value = html.unescape(value).strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.netloc.lower() not in DEMO_HOSTS:
        return ""
    if not parsed.path.startswith("/play/"):
        return ""
    return value


def _extract_demo_url(page_html: str) -> str:
    # El botón "Copy demo link" es la fuente autoritativa del enlace limpio.
    for match in _COPY_RE.finditer(page_html):
        value = match.group("url") or match.group("url2") or ""
        clean = _clean_demo_url(value)
        if clean:
            return clean

    # Respaldo: Play Demo actualmente lleva el mismo enlace.
    for match in _IFRAME_RE.finditer(page_html):
        clean = _clean_demo_url(match.group("url"))
        if clean:
            return clean

    return ""


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136 Safari/537.36"
            ),
            "Accept": "application/json, text/html, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": CATALOG_URL,
            "Cache-Control": "no-cache",
        }
    )
    return session


def _fetch_catalog_page(
    session: requests.Session,
    page: int,
    *,
    page_size: int,
    game_types: str,
    timeout: float,
) -> tuple[list[dict[str, str]], int]:
    params = {
        "types": game_types,
        "sort": "release_date",
        "order": "DESC",
        "posts_per_page": page_size,
        "format": "html",
        "columns_style": 1,
        "game_type": 1,
        "game_label": 1,
        "most_popular": 0,
        "filter": "game",
        "page": page,
        "lang": "en",
    }
    response = session.get(API_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(f"BGaming: respuesta inválida en página {page}")

    html_value = payload.get("html")
    total = payload.get("total")
    if not isinstance(html_value, str):
        raise RuntimeError(f"BGaming: falta html en página {page}")
    try:
        total_pages = max(1, int(total))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"BGaming: total de páginas inválido en página {page}: {total!r}"
        ) from exc

    return _parse_catalog_html(html_value), total_pages


def _fetch_demo_url(page_url: str, *, timeout: float) -> str:
    session = _new_session()
    try:
        response = session.get(page_url, timeout=timeout)
        response.raise_for_status()
        return _extract_demo_url(response.text)
    finally:
        session.close()


def _download_thumbnail(
    session: requests.Session,
    url: str,
    target: Path,
    *,
    timeout: float,
) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return

    response = session.get(url, timeout=timeout)
    response.raise_for_status()

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(target)


def crawl(
    output: Path,
    *,
    page_size: int = 25,
    game_types: str = DEFAULT_TYPES,
    workers: int = 12,
    timeout: float = 30.0,
    download_thumbnails: bool = True,
) -> list[dict[str, str]]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    session = _new_session()
    cards_by_url: dict[str, dict[str, str]] = {}

    try:
        page = 1
        total_pages = 1
        while page <= total_pages:
            cards, observed_total = _fetch_catalog_page(
                session,
                page,
                page_size=page_size,
                game_types=game_types,
                timeout=timeout,
            )
            if page == 1:
                total_pages = observed_total
            elif observed_total != total_pages:
                raise RuntimeError(
                    "BGaming: total de páginas cambió durante el crawl "
                    f"({total_pages} -> {observed_total})"
                )

            for card in cards:
                cards_by_url[card["page_url"]] = card
            print(f"Página {page}/{total_pages}: acumulados={len(cards_by_url)}")
            page += 1

        cards = list(cards_by_url.values())
        demo_by_page: dict[str, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_card = {
                executor.submit(_fetch_demo_url, card["page_url"], timeout=timeout): card
                for card in cards
            }
            for future in concurrent.futures.as_completed(future_to_card):
                card = future_to_card[future]
                name = card["name"]
                try:
                    demo_url = future.result()
                except Exception as exc:
                    print(f"[error link] {name}: {exc}")
                    demo_url = ""

                if demo_url:
                    demo_by_page[card["page_url"]] = demo_url
                    print(f"[link] {name}")
                else:
                    print(f"[sin demo] {name}")

        records: list[dict[str, str]] = []
        used_folders: set[str] = set()

        for card in cards:
            name = card["name"]
            page_url = card["page_url"]
            thumbnail_url = card["thumbnail_url"]
            row = {
                "name": name,
                "page": page_url,
                "demo": demo_by_page.get(page_url, ""),
            }

            if download_thumbnails and thumbnail_url:
                folder = _safe_folder(name)
                folder_key = folder.casefold()
                if folder_key in used_folders:
                    slug = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1]
                    folder = _safe_folder(f"{name}__{slug}")
                    folder_key = folder.casefold()
                used_folders.add(folder_key)

                thumbnail_path = (
                    output
                    / folder
                    / f"thumbnail{_thumbnail_suffix(thumbnail_url)}"
                )
                try:
                    _download_thumbnail(
                        session,
                        thumbnail_url,
                        thumbnail_path,
                        timeout=timeout,
                    )
                    row["thumbnail"] = thumbnail_path.relative_to(output).as_posix()
                except Exception as exc:
                    print(f"[error miniatura] {name}: {exc}")
                    row["thumbnail"] = ""
            else:
                row["thumbnail"] = thumbnail_url

            records.append(row)
    finally:
        session.close()

    records = sorted(
        records,
        key=lambda row: (row["name"].casefold(), row["page"]),
    )

    catalog_path = output / "catalog.json"
    tmp = catalog_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(catalog_path)

    targets_path = Path("targets.txt").resolve()
    targets = list(
        dict.fromkeys(row["demo"] for row in records if row["demo"])
    )
    targets_path.write_text(
        "\n".join(targets) + ("\n" if targets else ""),
        encoding="utf-8",
    )

    print(f"Listo: {len(records)} juegos; {len(targets)} demos")
    print(f"Catálogo: {catalog_path}")
    print(f"Targets: {targets_path}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Crawler mínimo de BGaming: catálogo, miniaturas y enlaces limpios "
            "del botón 'Copy demo link'."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Directorio de salida (default: {DEFAULT_OUTPUT.as_posix()})",
    )
    parser.add_argument(
        "--types",
        default=DEFAULT_TYPES,
        help="Taxonomía del catálogo BGaming (default: slots)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
        help="Cantidad pedida por página (default: 25)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Páginas de juegos procesadas en paralelo (default: 12)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout HTTP en segundos (default: 30)",
    )
    parser.add_argument(
        "--no-thumbnails",
        action="store_true",
        help="No descargar imágenes; guardar sus URLs en catalog.json",
    )
    args = parser.parse_args()

    if args.page_size < 1:
        parser.error("--page-size debe ser >= 1")
    if args.workers < 1:
        parser.error("--workers debe ser >= 1")
    if args.timeout <= 0:
        parser.error("--timeout debe ser > 0")

    crawl(
        args.output,
        page_size=args.page_size,
        game_types=args.types,
        workers=args.workers,
        timeout=args.timeout,
        download_thumbnails=not args.no_thumbnails,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
