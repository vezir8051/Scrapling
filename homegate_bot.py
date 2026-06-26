"""
homegate_bot.py
===============

A small alert bot around :mod:`homegate_scraper`. It periodically scrapes
Homegate rental listings, applies your price/room filters, and pushes **new**
listings to Telegram and/or Discord. Designed to run as a long-lived service
(e.g. on Railway).

Configuration is read from environment variables, so on Railway you just set
them in the service's "Variables" tab — no code changes needed.

Required (at least one notifier):
  TELEGRAM_BOT_TOKEN     Telegram bot token (from @BotFather)
  TELEGRAM_CHAT_ID       Chat/channel id to send to
  DISCORD_WEBHOOK_URL    Discord channel webhook URL (alternative to Telegram)

Scraping:
  HOMEGATE_LOCATION      Location slug (default: zurich)
  HOMEGATE_PAGES         Pages per check (default: 1)
  HOMEGATE_PROXY         Swiss residential proxy, http://user:pass@host:port
                         (strongly recommended; Homegate geo-blocks otherwise)

Filters (all optional):
  FILTER_MAX_PRICE       e.g. 2500
  FILTER_MIN_PRICE       e.g. 1000
  FILTER_MIN_ROOMS       e.g. 2.5
  FILTER_MAX_ROOMS       e.g. 4
  FILTER_MIN_SPACE       e.g. 50   (m²)
  FILTER_MAX_SPACE       e.g. 120  (m²)
  FILTER_LOCALITY        substring match on the city, e.g. "Zürich"

Runtime:
  CHECK_INTERVAL         seconds between checks in loop mode (default: 900)
  SEEN_STORE             path to the JSON file remembering already-sent ids
                         (default: seen_listings.json)

Run:
  python homegate_bot.py            # loop forever, checking every CHECK_INTERVAL
  python homegate_bot.py --once     # run a single check and exit (good for cron)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable

from homegate_scraper import fetch_rentals, filter_listings


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env_float(name: str) -> float | None:
    val = os.environ.get(name)
    if val in (None, ""):
        return None
    try:
        return float(val)
    except ValueError:
        print(f"[bot] ignoring non-numeric {name}={val!r}", file=sys.stderr)
        return None


def _load_config() -> dict:
    return {
        "location": os.environ.get("HOMEGATE_LOCATION", "zurich"),
        "pages": int(os.environ.get("HOMEGATE_PAGES", "1") or "1"),
        "proxy": os.environ.get("HOMEGATE_PROXY") or None,
        "filters": {
            "max_price": _env_float("FILTER_MAX_PRICE"),
            "min_price": _env_float("FILTER_MIN_PRICE"),
            "min_rooms": _env_float("FILTER_MIN_ROOMS"),
            "max_rooms": _env_float("FILTER_MAX_ROOMS"),
            "min_space": _env_float("FILTER_MIN_SPACE"),
            "max_space": _env_float("FILTER_MAX_SPACE"),
            "locality": os.environ.get("FILTER_LOCALITY") or None,
        },
        "interval": int(os.environ.get("CHECK_INTERVAL", "900") or "900"),
        "seen_store": os.environ.get("SEEN_STORE", "seen_listings.json"),
    }


# --------------------------------------------------------------------------- #
# Seen-id persistence (so we only alert on NEW listings)
# --------------------------------------------------------------------------- #
def _load_seen(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def _save_seen(path: str, seen: set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(seen), fh)
    except OSError as exc:
        print(f"[bot] could not persist seen ids: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Notifiers
# --------------------------------------------------------------------------- #
def _format_message(listing: dict) -> str:
    price = listing.get("price")
    currency = listing.get("currency") or "CHF"
    price_str = f"{price} {currency}" if price is not None else "Preis k. A."
    bits = [b for b in (
        f"{listing.get('rooms')} Zi." if listing.get("rooms") is not None else None,
        f"{listing.get('living_space')} m²" if listing.get("living_space") is not None else None,
        listing.get("locality"),
    ) if b]
    return (
        f"🏠 {listing.get('title') or 'Neues Inserat'}\n"
        f"💰 {price_str}\n"
        f"📐 {' · '.join(bits)}\n"
        f"🔗 {listing.get('url') or ''}"
    ).strip()


def _post(url: str, data: dict) -> bool:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 - notifier must never crash the loop
        print(f"[bot] notify failed: {exc}", file=sys.stderr)
        return False


def _make_notifier() -> Callable[[str], bool]:
    """Build a notifier from env vars. Falls back to stdout if none configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    senders: list[Callable[[str], bool]] = []

    if token and chat_id:
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        senders.append(
            lambda text: _post(api, {"chat_id": chat_id, "text": text, "disable_web_page_preview": "false"})
        )
    if webhook:
        senders.append(lambda text: _post(webhook, {"content": text}))

    if not senders:
        print(
            "[bot] No TELEGRAM_* or DISCORD_WEBHOOK_URL set — printing to stdout instead.",
            file=sys.stderr,
        )
        return lambda text: (print(text + "\n---"), True)[1]

    def notify(text: str) -> bool:
        return any(send(text) for send in senders)

    return notify


# --------------------------------------------------------------------------- #
# One check cycle
# --------------------------------------------------------------------------- #
def run_once(config: dict, notify: Callable[[str], bool]) -> int:
    """Scrape, filter, diff against seen ids, notify new ones. Returns # of new."""
    seen = _load_seen(config["seen_store"])
    first_run = not seen  # avoid blasting the whole first page as "new"

    listings = fetch_rentals(
        location=config["location"], pages=config["pages"], proxy=config["proxy"]
    )
    listings = filter_listings(listings, **config["filters"])

    new = [l for l in listings if l.get("id") and str(l["id"]) not in seen]
    print(
        f"[bot] {len(listings)} after filters, {len(new)} new "
        f"{'(seeding store, not notifying)' if first_run else ''}",
        file=sys.stderr,
    )

    for listing in new:
        if not first_run:
            notify(_format_message(listing))
        seen.add(str(listing["id"]))

    _save_seen(config["seen_store"], seen)
    return 0 if first_run else len(new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Homegate rental alert bot.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    args = parser.parse_args(argv)

    config = _load_config()
    notify = _make_notifier()

    if not config["proxy"]:
        print(
            "[bot] HOMEGATE_PROXY not set — Homegate will likely block requests. "
            "Set a Swiss residential proxy.",
            file=sys.stderr,
        )

    if args.once:
        run_once(config, notify)
        return 0

    print(f"[bot] starting loop, every {config['interval']}s", file=sys.stderr)
    while True:
        try:
            run_once(config, notify)
        except Exception as exc:  # noqa: BLE001 - keep the service alive
            print(f"[bot] check failed: {exc}", file=sys.stderr)
        time.sleep(config["interval"])


if __name__ == "__main__":
    raise SystemExit(main())
