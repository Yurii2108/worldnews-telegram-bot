#!/usr/bin/env python3
"""
RSS -> Telegram channel publisher.

Security:
- TELEGRAM_BOT_TOKEN must be supplied through an environment secret.
- Never hard-code the token in this repository.
"""

from __future__ import annotations

import calendar
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "seen.json"
CONFIG_PATH = ROOT / "config.yaml"
USER_AGENT = "WorldNewsUSUA-RSS-Bot/1.0"
TELEGRAM_TIMEOUT = 25
FEED_TIMEOUT = 20

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("worldnews")


@dataclass(frozen=True)
class NewsItem:
    key: str
    source: str
    source_weight: int
    title: str
    summary: str
    link: str
    image_url: str | None
    published_at: datetime | None
    score: float
    hashtags: tuple[str, ...]


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing configuration: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a YAML object")
    return config


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"initialized": False, "seen": {}}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("initialized", False)
        state.setdefault("seen", {})
        return state
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("Could not read state file; starting fresh: %s", exc)
        return {"initialized": False, "seen": {}}


def save_state(state: dict[str, Any], retention_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    retained: dict[str, str] = {}
    for key, iso_value in state.get("seen", {}).items():
        try:
            dt = date_parser.isoparse(iso_value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                retained[key] = iso_value
        except (TypeError, ValueError):
            continue

    payload = {
        "initialized": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seen": retained,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_PATH)


def canonicalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        clean = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        return clean
    except ValueError:
        return url


def make_key(link: str, title: str) -> str:
    basis = canonicalize_url(link) or normalize_title(title)
    return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()[:32]


def normalize_title(title: str) -> str:
    title = html.unescape(title or "").lower()
    title = re.sub(r"https?://\S+", " ", title)
    title = re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def clean_text(raw: str, max_chars: int) -> str:
    soup = BeautifulSoup(raw or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(Read more|Continue reading|Mehr|Подробнее)\b.*$", "", text, flags=re.I)
    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return shortened + "…"


def parse_entry_datetime(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (OverflowError, TypeError, ValueError):
                pass

    for attr in ("published", "updated", "created"):
        value = getattr(entry, attr, None)
        if value:
            try:
                dt = date_parser.parse(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
    return None


def extract_image(entry: Any) -> str | None:
    for attr in ("media_content", "media_thumbnail"):
        values = getattr(entry, attr, None) or []
        for value in values:
            if isinstance(value, dict):
                url = value.get("url")
                medium = value.get("medium", "")
                mime = value.get("type", "")
                if url and (medium in ("", "image") or str(mime).startswith("image/")):
                    return str(url)

    for enclosure in getattr(entry, "enclosures", None) or []:
        if isinstance(enclosure, dict):
            url = enclosure.get("href") or enclosure.get("url")
            mime = enclosure.get("type", "")
            if url and (not mime or str(mime).startswith("image/")):
                return str(url)
    return None


def contains_any(text: str, words: list[str]) -> bool:
    lower = text.casefold()
    return any(word.casefold() in lower for word in words if word)


def infer_hashtags(text: str, hashtag_rules: dict[str, list[str]], limit: int) -> tuple[str, ...]:
    tags: list[str] = []
    lower = text.casefold()
    for tag, keywords in hashtag_rules.items():
        if any(keyword.casefold() in lower for keyword in keywords):
            cleaned = re.sub(r"[^\wА-Яа-яЁёІіЇїЄєҐґ]", "", str(tag), flags=re.UNICODE)
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
        if len(tags) >= limit:
            break
    return tuple(tags)


def score_item(
    title: str,
    summary: str,
    published_at: datetime | None,
    source_weight: int,
    priority_keywords: dict[str, int],
) -> float:
    text = f"{title} {summary}".casefold()
    score = float(source_weight * 10)

    for keyword, points in priority_keywords.items():
        if keyword.casefold() in text:
            score += float(points)

    if published_at:
        age_hours = max(
            0.0,
            (datetime.now(timezone.utc) - published_at.astimezone(timezone.utc)).total_seconds() / 3600,
        )
        score += max(0.0, 30.0 - age_hours)
    else:
        score += 5.0

    if len(title) >= 25:
        score += 2.0
    if len(summary) >= 80:
        score += 2.0
    return score


def fetch_source(source: dict[str, Any], config: dict[str, Any]) -> list[NewsItem]:
    if not source.get("enabled", True):
        return []

    url = str(source.get("url", "")).strip()
    source_name = str(source.get("name", "Unknown source")).strip()
    if not url:
        return []

    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"}
    try:
        response = requests.get(url, timeout=FEED_TIMEOUT, headers=headers)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Feed failed: %s (%s)", source_name, exc)
        return []

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False):
        log.info("Feed parser warning for %s: %s", source_name, getattr(parsed, "bozo_exception", ""))

    settings = config.get("settings", {})
    max_age_hours = int(settings.get("max_age_hours", 36))
    max_summary_chars = int(settings.get("max_summary_chars", 430))
    exclude_keywords = list(config.get("exclude_keywords", []))
    priority_keywords = dict(config.get("priority_keywords", {}))
    hashtag_rules = dict(config.get("hashtag_rules", {}))
    hashtag_limit = int(settings.get("hashtag_limit", 4))
    source_weight = int(source.get("weight", 1))
    now = datetime.now(timezone.utc)

    items: list[NewsItem] = []
    for entry in parsed.entries:
        title = clean_text(str(getattr(entry, "title", "")), 220)
        link = canonicalize_url(str(getattr(entry, "link", "")))
        raw_summary = str(
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or getattr(entry, "subtitle", "")
        )
        summary = clean_text(raw_summary, max_summary_chars)
        if not title or not link:
            continue

        searchable = f"{title} {summary}"
        if contains_any(searchable, exclude_keywords):
            continue

        published_at = parse_entry_datetime(entry)
        if published_at:
            try:
                age = now - published_at.astimezone(timezone.utc)
                if age > timedelta(hours=max_age_hours) or age < timedelta(hours=-2):
                    continue
            except (OverflowError, ValueError):
                pass

        tags = infer_hashtags(searchable, hashtag_rules, hashtag_limit)
        score = score_item(title, summary, published_at, source_weight, priority_keywords)
        key = make_key(link, title)

        items.append(
            NewsItem(
                key=key,
                source=source_name,
                source_weight=source_weight,
                title=title,
                summary=summary,
                link=link,
                image_url=extract_image(entry),
                published_at=published_at,
                score=score,
                hashtags=tags,
            )
        )
    return items


def remove_near_duplicates(items: list[NewsItem], similarity_threshold: float) -> list[NewsItem]:
    selected: list[NewsItem] = []
    normalized_titles: list[str] = []

    for item in sorted(items, key=lambda x: x.score, reverse=True):
        current = normalize_title(item.title)
        is_duplicate = False
        for existing in normalized_titles:
            ratio = difflib.SequenceMatcher(None, current, existing).ratio()
            if ratio >= similarity_threshold:
                is_duplicate = True
                break
        if not is_duplicate:
            selected.append(item)
            normalized_titles.append(current)
    return selected


def build_post(item: NewsItem, config: dict[str, Any]) -> str:
    settings = config.get("settings", {})
    show_summary = bool(settings.get("show_summary", True))
    show_time = bool(settings.get("show_publication_time", True))

    lines = [f"<b>{html.escape(item.title)}</b>"]
    if show_summary and item.summary:
        lines.extend(["", html.escape(item.summary)])

    meta: list[str] = [f"Источник: {html.escape(item.source)}"]
    if show_time and item.published_at:
        local = item.published_at.astimezone(timezone.utc)
        meta.append(local.strftime("%d.%m.%Y %H:%M UTC"))

    lines.extend(["", " · ".join(meta), f'<a href="{html.escape(item.link, quote=True)}">Открыть оригинал</a>'])
    if item.hashtags:
        lines.extend(["", " ".join(f"#{html.escape(tag)}" for tag in item.hashtags)])

    text = "\n".join(lines).strip()
    return text[:4090]


class TelegramClient:
    def __init__(self, token: str, channel_id: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.channel_id = channel_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _post(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/{method}",
            data=data,
            timeout=TELEGRAM_TIMEOUT,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"ok": False, "description": response.text[:500]}
        if not response.ok or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram {method} failed: HTTP {response.status_code}; "
                f"{payload.get('description', 'unknown error')}"
            )
        return payload

    def send_test(self) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": self.channel_id,
                "text": (
                    "<b>✅ WorldNewsUSUA: тест успешен</b>\n\n"
                    "Бот подключён и имеет право публиковать сообщения."
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def publish(self, item: NewsItem, text: str) -> None:
        if item.image_url:
            try:
                caption = text if len(text) <= 1010 else (
                    f"<b>{html.escape(item.title)}</b>\n\n"
                    f'<a href="{html.escape(item.link, quote=True)}">Открыть оригинал</a>'
                )
                self._post(
                    "sendPhoto",
                    {
                        "chat_id": self.channel_id,
                        "photo": item.image_url,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                )
                return
            except Exception as exc:
                log.info("Image post failed; falling back to text: %s", exc)

        self._post(
            "sendMessage",
            {
                "chat_id": self.channel_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
        )


def require_environment() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "@WorldnewsUSUA").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not channel_id:
        raise RuntimeError("TELEGRAM_CHANNEL_ID is missing")
    return token, channel_id


def main() -> int:
    config = load_config()
    settings = config.get("settings", {})
    token, channel_id = require_environment()
    mode = os.getenv("MODE", "run").strip().lower()
    telegram = TelegramClient(token, channel_id)

    if mode == "test":
        telegram.send_test()
        log.info("Test message sent to %s", channel_id)
        return 0

    state = load_state()
    seen: dict[str, str] = state.get("seen", {})
    initialized = bool(state.get("initialized", False))

    all_items: list[NewsItem] = []
    for source in config.get("sources", []):
        try:
            all_items.extend(fetch_source(source, config))
        except Exception:
            log.exception("Unexpected source-processing error: %s", source.get("name", "unknown"))

    similarity = float(settings.get("duplicate_similarity", 0.86))
    unique_items = remove_near_duplicates(all_items, similarity)
    unseen = [item for item in unique_items if item.key not in seen]
    unseen.sort(key=lambda x: x.score, reverse=True)

    max_posts = int(settings.get("max_posts_per_run", 3))
    bootstrap_posts = int(settings.get("bootstrap_posts", 3))
    selected = unseen[: bootstrap_posts if not initialized else max_posts]

    now_iso = datetime.now(timezone.utc).isoformat()
    published_count = 0

    for item in selected:
        try:
            telegram.publish(item, build_post(item, config))
            seen[item.key] = now_iso
            published_count += 1
            log.info("Published: [%s] %s", item.source, item.title)
            time.sleep(float(settings.get("delay_between_posts_seconds", 2)))
        except Exception:
            log.exception("Could not publish: %s", item.title)

    # On the first run, mark all currently fetched stories as seen to avoid flooding
    # the channel with a backlog on subsequent runs.
    if not initialized:
        for item in unique_items:
            seen.setdefault(item.key, now_iso)

    # Optionally discard overflow so each cycle publishes only the best stories,
    # rather than slowly draining a large backlog.
    if initialized and bool(settings.get("drop_overflow", True)):
        for item in unseen[max_posts:]:
            seen.setdefault(item.key, now_iso)

    state["seen"] = seen
    state["initialized"] = True
    save_state(state, int(settings.get("state_retention_days", 30)))
    log.info(
        "Done. Sources=%d, collected=%d, unique=%d, unseen=%d, published=%d",
        len(config.get("sources", [])),
        len(all_items),
        len(unique_items),
        len(unseen),
        published_count,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.error("%s", exc)
        raise
