"""
homegate_scraper.py
===================

A small, self-contained Homegate (https://www.homegate.ch) **rental** scraper
built on top of Scrapling's ``StealthyFetcher``. Drop it next to your bot and
import it:

    from homegate_scraper import fetch_rentals

    listings = fetch_rentals("zurich", pages=3)        # without proxy (may be blocked)
    listings = fetch_rentals("geneva", pages=2,
                             proxy="http://user:pass@host:port")  # recommended

Each listing is a plain ``dict`` (JSON-serializable), so you can hand it straight
to a Telegram/Discord bot, a database, or ``json.dump``.

Why a stealth browser?
----------------------
Homegate blocks plain HTTP requests (403 / empty HTML shell) and requires real
JavaScript rendering. The data is embedded as JSON in a ``<script>`` tag:

* search/result pages -> ``window.__INITIAL_STATE__``
* property detail pages -> ``window.__PINIA_INITIAL_STATE__``

``StealthyFetcher`` renders the page in a patched stealth Chromium, so that JSON
is present and we extract the listings from it.

Important limits
----------------
* Homegate is geo/IP sensitive. For reliable results you almost certainly need a
  **Swiss residential proxy** (pass ``proxy=...`` / ``--proxy``). Without one the
  site may block the request or return empty data, even though the browser is
  stealthy.
* Scrapling natively solves Cloudflare only. If Homegate uses DataDome/Akamai,
  the stealth browser + a Swiss residential proxy is what carries you through;
  there is no native token solver for those.
* The JSON field paths below are resolved defensively (several candidate paths +
  a recursive search), because Homegate changes its front-end state shape from
  time to time. Use ``--debug-dump`` to inspect the raw state if a field comes
  back empty.

This script is for legitimate use only. Respect Homegate's Terms of Service and
robots.txt, and scrape responsibly (low concurrency, sensible delays).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable

from scrapling.fetchers import StealthyFetcher

# Homegate URL templates -----------------------------------------------------
BASE_URL = "https://www.homegate.ch"
SEARCH_URL = BASE_URL + "/rent/real-estate/{location}/matching-list?ep={page}"
DETAIL_URL = BASE_URL + "/rent/{listing_id}"

# Swiss-consistent browser settings.
_LOCALE = "de-CH"
_TIMEZONE = "Europe/Zurich"


# --------------------------------------------------------------------------- #
# JSON state extraction
# --------------------------------------------------------------------------- #
def _extract_state(response, var: str = "__INITIAL_STATE__") -> dict | None:
    """Pull the first complete JSON object assigned to ``window.<var>``.

    Robust against semicolons/nested braces inside the JSON by using
    ``json.JSONDecoder().raw_decode`` from the first ``{`` after ``var`` instead
    of a greedy regex.
    """
    try:
        scripts = response.css("script::text").getall()
    except Exception:
        scripts = []

    decoder = json.JSONDecoder()
    for script in scripts:
        if not script or var not in script:
            continue
        marker = script.index(var)
        brace = script.find("{", marker)
        if brace == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(script[brace:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# --------------------------------------------------------------------------- #
# Defensive helpers for digging values out of the (variable) state shape
# --------------------------------------------------------------------------- #
def _dig(obj: Any, path: str) -> Any:
    """Follow a dotted ``path`` through nested dicts/lists. Returns None on a miss.

    Numeric segments index into lists, e.g. ``"attachments.0.url"``.
    """
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and key.lstrip("-").isdigit():
            idx = int(key)
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            return None
    return cur


def _first(obj: Any, *paths: str) -> Any:
    """Return the first non-empty value among several candidate dotted paths."""
    for path in paths:
        val = _dig(obj, path)
        if val not in (None, "", [], {}):
            return val
    return None


_LISTING_KEYS = ("address", "prices", "characteristics")


def _looks_like_listing(d: Any) -> bool:
    """Heuristic: does this dict look like a Homegate listing record?

    Search result items have the shape ``{"id": ..., "listing": {...}}`` with the
    real data under ``listing``; detail records carry the data directly. Accept
    either shape.
    """
    if not isinstance(d, dict):
        return False
    inner = d.get("listing")
    if isinstance(inner, dict) and any(k in inner for k in _LISTING_KEYS):
        return True
    return "id" in d and any(k in d for k in _LISTING_KEYS)


def _find_listings(state: Any) -> list[dict]:
    """Recursively locate the listings array in the search state.

    Homegate wraps each result like ``{"listingType": ..., "listing": {...}}``.
    We walk the whole state and pick the largest list whose items look like
    listings, so we survive moderate restructuring of the state tree.
    """
    best: list[dict] = []

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            if node and sum(_looks_like_listing(x) for x in node) >= max(1, len(node) // 2):
                candidates = [x for x in node if _looks_like_listing(x)]
                if len(candidates) > len(best):
                    best = candidates
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(state)
    return best


def _localization_value(listing: dict, *keys: str) -> Any:
    """Pick a value from Homegate's ``localization`` block (language-keyed).

    The block looks like ``{"primary": "de", "de": {...}, "en": {...}}``. We try
    the primary language first, then any available language.
    """
    loc = listing.get("localization")
    if not isinstance(loc, dict):
        return None
    langs: list[str] = []
    primary = loc.get("primary")
    if isinstance(primary, str):
        langs.append(primary)
    langs += [k for k in loc.keys() if k != "primary" and isinstance(loc.get(k), dict)]
    for lang in langs:
        block = loc.get(lang)
        if not isinstance(block, dict):
            continue
        for key in keys:
            val = _dig(block, key)
            if val not in (None, "", [], {}):
                return val
    return None


def _normalize(record: dict) -> dict:
    """Map a raw Homegate listing record to a flat, bot-friendly dict."""
    listing = record.get("listing", record)
    if not isinstance(listing, dict):
        listing = record

    # On search pages the result item carries `id` at the top level, while
    # address/characteristics/prices live under the nested `listing` object.
    listing_id = _first(record, "id") or _first(listing, "id")
    rooms = _first(listing, "characteristics.numberOfRooms")
    living_space = _first(
        listing, "characteristics.livingSpace", "characteristics.totalFloorSpace"
    )
    locality = _first(listing, "address.locality", "address.region")

    title = _localization_value(listing, "text.title", "text.description") or _first(
        listing, "title"
    )
    if not title:
        # Result items often have no explicit title; build a readable one.
        parts = []
        if rooms is not None:
            parts.append(f"{rooms} Zi.")
        if living_space is not None:
            parts.append(f"{living_space} m²")
        if locality:
            parts.append(str(locality))
        title = ", ".join(parts) or None

    image = _localization_value(
        listing, "attachments.0.url", "attachments.0.file"
    )

    url = None
    if listing_id is not None:
        url = DETAIL_URL.format(listing_id=listing_id)

    return {
        "id": listing_id,
        "title": title,
        "price": _first(
            listing,
            "prices.rent.gross",
            "prices.rent.net",
            "prices.rent.interval.price",
            "prices.buy.price",
        ),
        "currency": _first(listing, "prices.currency", "prices.rent.currency"),
        "rooms": rooms,
        "living_space": living_space,
        "locality": locality,
        "postal_code": _first(listing, "address.postalCode"),
        "street": _first(listing, "address.street"),
        "url": url,
        "image": image,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def fetch_rentals(
    location: str = "zurich",
    pages: int = 1,
    proxy: str | dict | None = None,
    headless: bool = True,
    solve_cloudflare: bool = True,
    timeout: int = 60000,
    debug_dump: str | None = None,
) -> list[dict]:
    """Scrape Homegate rental search results.

    :param location: Location slug as used in Homegate URLs, e.g. ``"zurich"``,
        ``"geneva"``, ``"canton-zurich"``.
    :param pages: How many result pages to fetch (``ep=1..pages``).
    :param proxy: Optional proxy. A string (``"http://user:pass@host:port"``) or a
        dict with ``server``/``username``/``password``. **Strongly recommended**
        to be a Swiss residential proxy.
    :param headless: Run the browser hidden (default) or visible.
    :param solve_cloudflare: Auto-solve Cloudflare challenges if encountered.
    :param timeout: Per-operation timeout in milliseconds.
    :param debug_dump: If set, write the raw ``__INITIAL_STATE__`` of the first
        page to this file path for inspecting/mapping fields.
    :return: A list of flat listing dicts (JSON-serializable).
    """
    results: list[dict] = []

    for page in range(1, pages + 1):
        url = SEARCH_URL.format(location=location, page=page)
        try:
            response = StealthyFetcher.fetch(
                url,
                headless=headless,
                network_idle=True,
                solve_cloudflare=solve_cloudflare,
                proxy=proxy,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
                google_search=True,
                timeout=timeout,
            )
        except Exception as exc:  # network / browser failure -> skip this page
            print(f"[homegate] page {page}: fetch failed: {exc}", file=sys.stderr)
            continue

        status = getattr(response, "status", None)
        if status and status >= 400:
            print(
                f"[homegate] page {page}: HTTP {status} — likely blocked. "
                f"A Swiss residential proxy is usually required.",
                file=sys.stderr,
            )

        state = _extract_state(response, "__INITIAL_STATE__")
        if debug_dump and page == 1:
            with open(debug_dump, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            print(f"[homegate] raw state written to {debug_dump}", file=sys.stderr)

        if not state:
            print(
                f"[homegate] page {page}: no __INITIAL_STATE__ found "
                f"(blocked, empty, or front-end changed).",
                file=sys.stderr,
            )
            continue

        listings = _find_listings(state)
        if not listings:
            print(
                f"[homegate] page {page}: state found but no listings matched. "
                f"Use --debug-dump to inspect the JSON shape.",
                file=sys.stderr,
            )
            continue

        results.extend(_normalize(item) for item in listings)
        print(
            f"[homegate] page {page}: {len(listings)} listings.", file=sys.stderr
        )

    return results


def fetch_detail(
    listing_url_or_id: str,
    proxy: str | dict | None = None,
    headless: bool = True,
    solve_cloudflare: bool = True,
    timeout: int = 60000,
) -> dict:
    """Fetch a single property detail page and extract richer fields.

    Optional bonus over :func:`fetch_rentals` (which only returns list fields).
    Detail pages store their data in ``window.__PINIA_INITIAL_STATE__``.

    :param listing_url_or_id: A full detail URL or a bare listing id.
    """
    if str(listing_url_or_id).startswith("http"):
        url = str(listing_url_or_id)
    else:
        url = DETAIL_URL.format(listing_id=listing_url_or_id)

    response = StealthyFetcher.fetch(
        url,
        headless=headless,
        network_idle=True,
        solve_cloudflare=solve_cloudflare,
        proxy=proxy,
        locale=_LOCALE,
        timezone_id=_TIMEZONE,
        google_search=True,
        timeout=timeout,
    )

    state = _extract_state(response, "__PINIA_INITIAL_STATE__") or _extract_state(
        response, "__INITIAL_STATE__"
    )
    if not state:
        return {"url": url, "error": "no state found (blocked, empty, or changed)"}

    # Pinia detail state nests the record at state["listing"]["listing"]; fall
    # back to the recursive finder if the shape differs.
    listing = _dig(state, "listing.listing")
    if not isinstance(listing, dict):
        found = _find_listings(state)
        listing = found[0].get("listing", found[0]) if found else state

    return {
        "url": url,
        "title": _localization_value(listing, "text.title") if isinstance(listing, dict) else None,
        "description": _localization_value(listing, "text.description")
        if isinstance(listing, dict)
        else None,
        "features": _first(listing, "characteristics") if isinstance(listing, dict) else None,
        "address": _first(listing, "address") if isinstance(listing, dict) else None,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Homegate.ch rental listings via Scrapling's StealthyFetcher."
    )
    parser.add_argument(
        "--location", default="zurich", help="Location slug (default: zurich)"
    )
    parser.add_argument(
        "--pages", type=int, default=1, help="Number of result pages to fetch"
    )
    parser.add_argument(
        "--out", default="rentals.json", help="Output JSON file (default: rentals.json)"
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL, e.g. http://user:pass@host:port (Swiss residential recommended)",
    )
    parser.add_argument(
        "--headful", action="store_true", help="Show the browser window (debugging)"
    )
    parser.add_argument(
        "--no-cloudflare",
        action="store_true",
        help="Disable Cloudflare auto-solving (faster if not needed)",
    )
    parser.add_argument(
        "--debug-dump",
        nargs="?",
        const="homegate_state.json",
        default=None,
        help="Write raw __INITIAL_STATE__ of page 1 to a file for inspection",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)

    if not args.proxy:
        print(
            "[homegate] No proxy set. Homegate is geo/IP sensitive — without a "
            "Swiss residential proxy you may get blocked or empty results.",
            file=sys.stderr,
        )

    listings = fetch_rentals(
        location=args.location,
        pages=args.pages,
        proxy=args.proxy,
        headless=not args.headful,
        solve_cloudflare=not args.no_cloudflare,
        debug_dump=args.debug_dump,
    )

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(listings, fh, ensure_ascii=False, indent=2)

    print(f"[homegate] {len(listings)} listings written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
