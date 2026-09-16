#!/usr/bin/env python3
"""
Genereer een podcast RSS 2.0-feed (met iTunes-tags) uit de mp3's in episodes/.

De feed wijst naar afleveringen via PUBLIC_BASE_URL. Zet deze env-variabele naar
de publieke basis-URL waar de episodes gehost worden (bijv. GitHub Pages):
  PUBLIC_BASE_URL=https://lorenzo07.github.io/nl-daily-podcast

Zonder PUBLIC_BASE_URL worden relatieve paden gebruikt (lokale test).

Gebruik:
  .venv/bin/python generate_feed.py
Output: feed.xml in de projectroot.
"""

import os
import re
import json
import subprocess
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EPISODES_DIR = ROOT / "episodes"
FEED_PATH = ROOT / "feed.xml"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
PODCAST_TITLE = os.environ.get("PODCAST_TITLE", "NL Daily News — B1")
PODCAST_DESC = (
    "Dagelijks nieuws in eenvoudig Nederlands (B1-niveau). "
    "Belangrijkste nieuws uit Nederland en de wereld, samengevat en voorgelezen."
)
PODCAST_LANG = "nl-nl"
PODCAST_AUTHOR = os.environ.get("PODCAST_AUTHOR", "Lorenzo")


def ffprobe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True,
        )
        return int(float(out.strip()))
    except Exception:
        return 0


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def episode_date(stem, meta):
    date_str = meta.get("date") or re.sub(r".*-(\d{4}-\d{2}-\d{2})$", r"\1", stem)
    try:
        return dt.date.fromisoformat(date_str)
    except Exception:
        return None


def cleanup_old_episodes():
    """Verwijder afleveringen ouder dan MAX_AGE_DAYS van schijf (en dus uit feed)."""
    today = dt.date.today()
    removed = 0
    for mp3 in EPISODES_DIR.glob("episode-*.mp3"):
        stem = mp3.stem
        meta_path = EPISODES_DIR / f"{stem}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        d = episode_date(stem, meta)
        if d is None:
            continue
        age = (today - d).days
        if age > MAX_AGE_DAYS:
            for ext in (".mp3", ".txt", ".meta.json", ".wav"):
                p = EPISODES_DIR / f"{stem}{ext}"
                if p.exists():
                    p.unlink()
            removed += 1
            print(f"  opruimen: {stem} ({age} dagen oud)")
    if removed:
        print(f"  {removed} oude aflevering(en) verwijderd (> {MAX_AGE_DAYS} dagen).")


def main():
    cleanup_old_episodes()
    items = []
    ep_files = sorted(EPISODES_DIR.glob("episode-*.mp3"), reverse=True)
    for mp3 in ep_files:
        stem = mp3.stem
        meta_path = EPISODES_DIR / f"{stem}.meta.json"
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        date_str = meta.get("date") or re.sub(r".*-(\d{4}-\d{2}-\d{2})$", r"\1", stem)
        try:
            d = dt.date.fromisoformat(date_str)
        except Exception:
            d = dt.date.today() or dt.date(2000, 1, 1)
        secs = ffprobe_duration(mp3)
        size = mp3.stat().st_size
        if PUBLIC_BASE_URL:
            url = f"{PUBLIC_BASE_URL}/episodes/{mp3.name}"
        else:
            url = f"episodes/{mp3.name}"
        title = f"Aflevering {d.isoformat()}"
        if stem.endswith("-14b") or stem.endswith("-32b"):
            title += f" ({stem.split('-')[-1]})"
        items.append({
            "title": title,
            "date": d,
            "url": url,
            "size": size,
            "secs": secs,
            "guid": f"nl-daily-{d.isoformat()}-{stem}",
        })

    last_build = dt.datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0200")
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">')
    lines.append("  <channel>")
    lines.append(f"    <title>{esc(PODCAST_TITLE)}</title>")
    lines.append(f"    <description>{esc(PODCAST_DESC)}</description>")
    lines.append(f"    <language>{PODCAST_LANG}</language>")
    lines.append(f"    <itunes:author>{esc(PODCAST_AUTHOR)}</itunes:author>")
    lines.append('    <itunes:category text="News"/>')
    lines.append("    <itunes:explicit>false</itunes:explicit>")
    # cover image (podcast artwork) — als cover.png/jpg/jpeg in de repo-root staat
    cover_path = None
    for name in ("cover.png", "cover.jpg", "cover.jpeg"):
        if (ROOT / name).exists():
            cover_path = ROOT / name
            break
    if cover_path and PUBLIC_BASE_URL:
        lines.append(f'    <itunes:image href="{PUBLIC_BASE_URL}/{cover_path.name}"/>')
    lines.append(f"    <lastBuildDate>{last_build}</lastBuildDate>")

    for it in items:
        pub = it["date"].strftime("%a, %d %b %Y 06:00:00 +0200")
        lines.append("    <item>")
        lines.append(f"      <title>{esc(it['title'])}</title>")
        lines.append(f"      <pubDate>{pub}</pubDate>")
        lines.append(f"      <guid isPermaLink=\"false\">{it['guid']}</guid>")
        lines.append(f"      <enclosure url=\"{esc(it['url'])}\" length=\"{it['size']}\" type=\"audio/mpeg\"/>")
        if it["secs"]:
            lines.append(f"      <itunes:duration>{it['secs']}</itunes:duration>")
        lines.append("    </item>")

    lines.append("  </channel>")
    lines.append("</rss>")

    FEED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"feed.xml geschreven: {FEED_PATH} ({len(items)} afleveringen)")
    if not PUBLIC_BASE_URL:
        print("WAARSCHUWING: PUBLIC_BASE_URL niet gezet — feed gebruikt relatieve paden (lokale test).")


if __name__ == "__main__":
    main()
