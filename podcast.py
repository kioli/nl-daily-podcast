#!/usr/bin/env python3
"""
NL Daily News Podcast — prototype (volledig lokaal, 0 euro).

Pijplijn:
  1. RSS-feeds ophalen (feedparser)
  2. Volledige artikeltekst extraheren waar gratis mogelijk (trafilatura)
  3. Per-artikel samenvatten in Nederlands B1, strikt zonder verzinning (Ollama)
  4. Gebalanceerde selectie per categorie
  5. Eén samenhangende, letterlijk voor te lezen tekst in B1-Nederlands (Ollama)
  6. Audio synthese met edge-tts (nl-NL stem, vertraagd tempo)

Geen API-sleutels, geen cloud. Vereist: Ollama draait lokaal met mistral:latest.

Gebruik:
  .venv/bin/python podcast.py                  # volledige pijplijn
  .venv/bin/python podcast.py --no-tts         # alleen tekst, geen audio
  .venv/bin/python podcast.py --max-articles 12  # minder artikelen (sneller)

Omgeving (optioneel):
  OLLAMA_MODEL     mistral:latest
  TTS_VOICE        nl-NL-ColetteNeural
  TTS_RATE         -15%            (negatief = langzamer)
  TARGET_WORDS     1700            (~12-14 min uitlezing)
"""

import os
import sys
import re
import json
import time
import shutil
import argparse
import subprocess
import datetime as dt
from pathlib import Path

import yaml
import feedparser
import trafilatura
import requests

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
EPISODES_DIR = ROOT / "episodes"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_CALL_DELAY = float(os.environ.get("GROQ_CALL_DELAY", "20"))
TTS_VOICE = os.environ.get("TTS_VOICE", "nl-NL-ColetteNeural")
TTS_RATE = os.environ.get("TTS_RATE", "-15%")
TARGET_WORDS = int(os.environ.get("TARGET_WORDS", "2400"))
PER_FEED = int(os.environ.get("MAX_ARTICLES_PER_FEED", "4"))

CATEGORY_ORDER = ["buitenland", "binnenland", "economie", "algemeen", "wetenschap", "sport", "cultuur"]
SECTION_INTRO = {
    "buitenland": "Eerst de nieuws uit de wereld.",
    "binnenland": "Nu de nieuws uit Nederland.",
    "economie": "Daarna het economische nieuws.",
    "algemeen": "Verder het algemene nieuws.",
    "wetenschap": "Nu nieuws uit de wetenschap.",
    "sport": "Tot slot het sportnieuws.",
    "cultuur": "Tot slot het cultuurnieuws.",
}
WPM = 120  # gesproken woorden/min, vertraagd B1-tempo


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- Ollama ----------

def llm_chat(messages, temperature=0.2, num_predict=None, num_ctx=8192):
    """Eén LLM-aanroep. Gebruikt Groq (cloud) als GROQ_API_KEY gezet is, anders
    lokale Ollama. Groq is OpenAI-compatible. Handelt 429 rate-limit af met
    backoff (Groq free tier: ~7000 input tokens/min)."""
    if GROQ_API_KEY:
        payload = {
            "model": GROQ_MODEL,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if num_predict:
            payload["max_tokens"] = num_predict
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        for attempt in range(8):
            if attempt == 0 and GROQ_CALL_DELAY:
                time.sleep(GROQ_CALL_DELAY)
            resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=600)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(60, 2 ** attempt * 3)
                log(f"  Groq 429 rate-limit, wacht {wait:.0f}s (poging {attempt+1}/8)")
                time.sleep(wait)
                continue
            if resp.status_code in (502, 503, 504):
                time.sleep(min(30, 2 ** attempt * 2))
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:300]}")
            return resp.json()["choices"][0]["message"]["content"].strip()
        raise RuntimeError("Groq rate-limit: opgegeven na 8 pogingen")
    # lokale Ollama
    options = {"temperature": temperature, "num_ctx": num_ctx}
    if num_predict:
        options["num_predict"] = num_predict
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "options": options,
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


# ---------- Stap 1+2: feeds ophalen + tekst extraheren ----------

def scrape_nos_section(src):
    """Scrape NOS-sectiepagina voor /artikel/-links (NOS heeft geen RSS)."""
    try:
        r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=15)
    except Exception as e:
        log(f"  kon NOS-pagina niet ophalen: {e}")
        return []
    if r.status_code != 200:
        log(f"  NOS HTTP {r.status_code}")
        return []
    raw_links = re.findall(r'href="(/artikel/\d+[^"]*)"', r.text)
    seen = set()
    articles = []
    for path in raw_links:
        if path in seen:
            continue
        seen.add(path)
        # titel uit slug (strip leading article-ID digits)
        slug = path.split("/", 2)[-1]
        slug = slug.split("?")[0]
        slug = re.sub(r"^\d+", "", slug)
        title = slug.replace("-", " ").strip()
        articles.append({
            "source": src["name"],
            "region": src["region"],
            "category": src["category"],
            "title": title,
            "link": "https://nos.nl" + path,
            "rss_summary": "",
            "fulltext": True,
        })
        if len(articles) >= PER_FEED:
            break
    return articles


def fetch_feeds(sources):
    articles = []
    for src in sources["feeds"]:
        log(f"Feed: {src['name']}")
        if src.get("scraper"):
            items = scrape_nos_section(src)
            log(f"  {len(items)} items gescrapet")
            articles.extend(items)
            continue
        try:
            parsed = feedparser.parse(src["url"])
        except Exception as e:
            log(f"  kon feed niet ophalen: {e}")
            continue
        if not parsed.entries:
            log("  geen items (feed onbereikbaar?)")
            continue
        taken = 0
        for entry in parsed.entries:
            if taken >= PER_FEED:
                break
            link = entry.get("link", "")
            if not link:
                continue
            title = entry.get("title", "").strip()
            rss_summary = entry.get("summary", "") or entry.get("description", "")
            rss_summary = re.sub(r"<[^>]+>", " ", rss_summary).strip()
            published = None
            pt = entry.get("published_parsed") or entry.get("updated_parsed")
            if pt:
                try:
                    published = dt.datetime(*pt[:6], tzinfo=dt.timezone.utc)
                except Exception:
                    published = None
            articles.append({
                "source": src["name"],
                "region": src["region"],
                "category": src["category"],
                "title": title,
                "link": link,
                "rss_summary": rss_summary,
                "fulltext": src.get("fulltext", True),
                "published": published,
            })
            taken += 1
        log(f"  {taken} items genomen")
    return articles


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def extract_text(article):
    """Volledige tekst waar mogelijk; anders RSS-samenvatting; anders titel."""
    if article["fulltext"]:
        try:
            r = requests.get(article["link"], headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200 and r.text:
                text = trafilatura.extract(
                    r.text,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                )
                if text and len(text) > 120:
                    return text[:4000]  # begrens token-gebruik (geheugenbesparing)
                else:
                    log(f"  geen body gevonden ({article['source']}) — terugval op RSS")
            else:
                log(f"  HTTP {r.status_code} ({article['source']}) — terugval op RSS")
        except Exception as e:
            log(f"  extractie faalde voor {article['source']}: {e}")
    if article["rss_summary"] and len(article["rss_summary"]) > 40:
        return article["rss_summary"]
    return article["title"]


# ---------- Stap 3: per-artikel samenvatten ----------

SUMMARY_PROMPT = """\
Je bent een nauwkeurige nieuwsredacteur. Resumeer het volgende artikel in het \
Nederlands op B1-niveau: eenvoudige, veelvoorkomende woorden en korte, duidelijke \
zinnen. Vermijd jargon en ingewikkelde bijzinnen.

STRIKTE REGEL — GEEN VERZINNING (allerbelangrijkste):
Gebruik ALLEEN informatie die letterlijk in de tekst hieronder staat. Je mag GEEN \
enkeel feit, naam, datum, getal, oorzaak of gevolg noemen dat niet expliciet in de \
tekst staat. Gebruik nooit je eigen voorkennis of wereldkennis om de tekst aan te \
vullen. Liever een korte, correcte samenvatting dan een lange met erbij verzonnen \
details.

VOORBEELD van wat NIET mag:
- Als de tekst zegt "lichaam gevonden in water" maar geen naam noemt, schrijf dan \
NIET "mogelijk de zanger X". Noem alleen wat er letterlijk staat.
- Als de tekst over een verkiezing gaat maar geen kandidaat noemt, schrijf dan \
NIET de namen van kandidaten die je zelf weet.

Schrijf 3 tot 5 zinnen. Begin NIET met "Dit artikel" of "Het artikel". \
Schrijf alsof je de feiten vertelt. Vermeld de bronnaam niet in de samenvatting.

Bron: {source}

Tekst:
\"\"\"
{text}
\"\"\"
"""


def summarize_article(article, text):
    prompt = SUMMARY_PROMPT.format(source=article["source"], text=text)
    msg = llm_chat(
        [{"role": "user", "content": prompt}],
        temperature=0.1,
        num_predict=300,
    )
    return msg


# ---------- Stap 4: gebalanceerde selectie ----------

MAX_NL = int(os.environ.get("MAX_NL", "8"))
MAX_WORLD = int(os.environ.get("MAX_WORLD", "14"))


def _title_tokens(title):
    return set(re.findall(r"[a-z0-9]+", title.lower()))


def _content_signature(article):
    """Handtekening op basis van titel + eerste deel van de tekst (voor cross-categorie dedup)."""
    raw = article["title"] + " " + (article.get("text", "") or "")[:400]
    return set(re.findall(r"[a-z0-9]{4,}", raw.lower()))


def deduplicate(summaries):
    """Verwijder exacte duplicaten (zelfde link) en bijna-duplicaten.
    Gebruikt zowel titel-overlap als content-overlap om hetzelfde verhaal
    dat in meerdere feeds/categorieën verschijnt te herkennen."""
    seen_links = set()
    seen_title_sigs = []
    seen_content_sigs = []
    out = []
    for s in summaries:
        if s["link"] in seen_links:
            continue
        title_tok = _title_tokens(s["title"])
        content_sig = _content_signature(s)
        dup = False
        for prev_title, prev_content in zip(seen_title_sigs, seen_content_sigs):
            t_inter = len(title_tok & prev_title)
            t_union = len(title_tok | prev_title) or 1
            c_inter = len(content_sig & prev_content)
            c_union = len(content_sig | prev_content) or 1
            if t_inter / t_union > 0.4 or c_inter / c_union > 0.3:
                dup = True
                break
        if dup:
            continue
        seen_links.add(s["link"])
        seen_title_sigs.append(title_tok)
        seen_content_sigs.append(content_sig)
        out.append(s)
    return out


def score_articles(articles):
    """Score elk artikel op belangrijkheid:
    1. cross-fonte frequency (meerdere bronnen over hetzelfde feit = belangrijk)
    2. recency (verser = iets hoger)
    3. tekstlengte (meer inhoud = meer substantie)
    4. Amsterdam-bonus (Lorenzo woont daar)
    """
    now = dt.datetime.now(dt.timezone.utc)
    for i, a in enumerate(articles):
        a_tok = _title_tokens(a["title"])
        freq = 0
        for j, b in enumerate(articles):
            if i == j or a["source"] == b["source"]:
                continue
            b_tok = _title_tokens(b["title"])
            inter = len(a_tok & b_tok)
            union = len(a_tok | b_tok) or 1
            if inter / union > 0.4:
                freq += 1
        score = freq * 3
        pub = a.get("published")
        if pub:
            try:
                hours_old = (now - pub).total_seconds() / 3600
                score += max(0, 2 - hours_old / 6)
            except Exception:
                pass
        text_len = len(a.get("text", ""))
        if text_len > 800:
            score += 1
        if text_len > 2000:
            score += 1
        if a["region"] != "world":
            combined = (a["title"] + " " + a.get("text", "")).lower()
            if "amsterdam" in combined:
                score += 2
        a["score"] = score


def balance(articles, max_total=22):
    """Selecteer max_total artikelen: ~MAX_WORLD wereld + ~MAX_NL nederland.
    Binnen elkquotum: hoogste score eerst. NL ook gebalanceerd over categorieën."""
    score_articles(articles)
    world = [a for a in articles if a["region"] == "world"]
    nl = [a for a in articles if a["region"] != "world"]
    world.sort(key=lambda a: a["score"], reverse=True)
    nl.sort(key=lambda a: a["score"], reverse=True)

    n_world = min(MAX_WORLD, len(world))
    n_nl = min(max_total - n_world, len(nl), MAX_NL)
    # als er te weinig world is, vul aan met NL
    if n_world + n_nl < max_total:
        n_nl = min(max_total - n_world, len(nl))

    selected = world[:n_world]
    selected_links = {s["link"] for s in selected}

    # NL: round-robin per categorie (diversiteit), binnen categorie op score
    by_cat = {}
    for a in nl:
        if a["link"] in selected_links:
            continue
        by_cat.setdefault(a["category"], []).append(a)
    for c in by_cat:
        by_cat[c].sort(key=lambda a: a["score"], reverse=True)
    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat]
    ordered_cats += [c for c in by_cat if c not in CATEGORY_ORDER]
    queue = [by_cat[c] for c in ordered_cats]
    while queue and len(selected) < n_world + n_nl:
        progressed = False
        for i in range(len(queue) - 1, -1, -1):
            if not queue[i]:
                queue.pop(i)
                continue
            selected.append(queue[i].pop(0))
            progressed = True
            if len(selected) >= n_world + n_nl:
                break
        if not progressed:
            break
    return selected


# ---------- Stap 5: eindscript ----------

CAT_PROMPT = """\
Je bent een ervaren nieuwsjournalist die een rustige, dagelijkse podcast \
voorleest in eenvoudig Nederlands (B1-niveau) — in de stijl van BBC News: \
geen lijst van titels, maar een verhaal met diepgang.

Hieronder staan samenvattingen van nieuwsartikelen uit de categorie "{cat}", \
elk met de bronnaam erbij.

Schrijf de tekst voor ALLEEN deze categorie, als vloeiende alinea's. Geen \
opsommingstekens, geen koppen, geen inleiding of afsluiting — alleen de \
nieuwsinhoud voor deze categorie.

STIJL — geef SPESHOOR, niet alleen titels:
- Schrijf precies ÉÉN item per bronnensamenvatting hieronder. Splits één artikel NOOIT in meerdere items of "angles". Als er 3 samenvattingen staan, schrijf precies 3 items (De eerste, De tweede, De derde) — niet meer.
- Elk nieuwsitem begint met een AANKONDIGINGSZIN die het nummer en het onderwerp \
noemt, gevolgd door de inhoud. Nummer de items binnen de categorie: "De eerste: \
de staking van schoonmakers in Nederland." Daarna de uitwerking. "De tweede: ..." \
voor het volgende item, enzovoort. Deze aankondiging werkt als een hoorbare \
scheiding tussen items, zodat de luisteraar weet waar het over gaat.
- Werk elk item uit tot een stukje van 3-5 zinnen: vertel wat er gebeurt, \
het belang, en de context die in de samenvatting staat. Behandel het niet als \
een koppenlijst. Liever kort en correct dan lang en opgevuld.
- Een korte verbindende zin tussen items is goed ("Daarna nieuws uit het \
buitenland."), zodat het geen losse opsomming lijkt — maar geen lange overgangen.
- Als MEERDERE bronnen dezelfde gebeurtenis belichten, zet ze NAAST ELKAAR in \
één passage: laat zien wat ze benadrukken, waar ze overeenkomen of verschillen. \
Bijvoorbeeld: "BBC benadrukt ... terwijl The Guardian vooral meldt dat ...".
- Als een samenvatting een uitspraak of citaat bevat, geef die dan direct weer \
("De minister zei: '...'").

STRIKTE REGEL — GEEN VERZINNING (allerbelangrijkste):
- Gebruik ALLEEN de feiten, context en citaten die in de samenvattingen hieronder \
staan. Je mag GEEN naam, datum, getal, oorzaak, gevolg of achtergrond noemen die \
niet letterlijk in de samenvattingen staat. Gebruik nooit je eigen voorkennis.
- "Diepgang" betekent: meer ruimte voor wat WEL in de tekst staat, niet erbij \
verzinnen. Liever kort en correct dan lang en verzonnen.
- Vermeld bij elk item de bron ("Volgens NOS ...", "The Guardian meldt ...").

Taal: B1-Nederlands — korte, duidelijke zinnen, gewone woorden. Rustige, \
feitelijke toon, niet sensatiegericht.

LENGTE: Doel voor deze categorie: ongeveer {target} woorden. Dat is ruim \
{target // max(1, n_items_hint)} woorden per item. Schrijf elk item volledig \
uit (3-5 zinnen) maar voeg GEEN extra items toe om de lengte te halen — \
liever korter dan doel dan verzinnen of herhalen. Blijf uitsluitend bij de \
feiten uit de samenvattingen.

Samenvattingen (categorie: {cat}):

{items}
"""


_DUTCH_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
_DUTCH_MONTHS = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


def date_pretty(iso):
    d = dt.date.fromisoformat(iso)
    return f"{_DUTCH_DAYS[d.weekday()]} {d.day} {_DUTCH_MONTHS[d.month - 1]} {d.year}"


def group_by_category(selected):
    by_cat = {}
    for s in selected:
        by_cat.setdefault(s["category"], []).append(s)
    ordered = [c for c in CATEGORY_ORDER if c in by_cat]
    ordered += [c for c in by_cat if c not in CATEGORY_ORDER]
    return [(c, by_cat[c]) for c in ordered]


EXPAND_PROMPT = """\
Hieronder staat een concept-tekst voor een dagelijkse nieuwtspodcast, gevolgd door \
de bron-samenvattingen waarop het gebaseerd is. Het concept is TE KORT.

Jouw taak: breid de tekst UIT tot ongeveer {target} woorden, zodat de uitlezing \
ongeveer 20 minuten duurt. Werk elk nieuwsitem uitvoeriger uit, met meer CONCRETE \
context en detail UIT DE SAMENVATTINGEN — specifieke feiten, getallen, namen, \
plaatsen, tijdstippen, oorzaken en gevolgen die in de samenvattingen staan maar \
nog niet in het concept.

STRIKTE REGELS — geen verzinning en geen vulling:
1. Voeg GEEN nieuwe feiten, namen, datums, getallen, oorzaken of gevolgen toe die \
niet in de samenvattingen hieronder staan. "Uitbreiden" = meer ruimte voor wat WEL \
staat, niet erbij verzinnen.
2. VERBODEN vullingszinnen — gebruik deze NIET en schrijf ook geen vergelijkbare \
vage commentary:
   - "Dit toont aan hoe belangrijk/gevoelig ..."
   - "X blijft actief om ..."
   - "Dit heeft veel aandacht getrokken en de discussie versterkt"
   - "Dit heeft invloed op de toekomst van ..."
   - Herhaling van dezelfde zin in andere woorden.
3. Breid uit met ALLEEN extra concrete details uit de samenvattingen. Als een \
samenvatting geen extra detail meer bevat, laat dat item dan zoals het was — \
verzin niets eromheen.
4. Als meerdere bronnen dezelfde gebeurtenis belichten, zet hun perspectieven \
naast elkaar ("BBC benadrukt ..., terwijl The Guardian meldt ...").
5. Behoud bij elk item de bronnaam. Behoud B1-Nederlands, rustige toon, structuur \
per categorie, inleiding en afsluiting. Geen opsommingstekens, geen koppen.

Concept-tekst:

\"\"\"
{draft}
\"\"\"

Bron-samenvattingen:

{sources}

Schrijf nu de VOLLEDIGE uitgebreide tekst (de hele podcast, niet alleen de \
gewijzigde delen):
"""


def _is_clean_dutch(text):
    """True als tekst alleen Latijnse tekens, cijfers en gangige leestekens bevat.
    Vangt degeneratie op (bijv. CJK-tekens die het model soms uitbraakt)."""
    # tel niet-Latijnse, niet-witte-ruimte tekens
    bad = sum(1 for ch in text if ord(ch) > 0x024F and ch not in "“”‘’…–—")
    return bad == 0


def _is_degenerate(text):
    """True als tekst herhalings-degeneratie bevat: dezelfde zin of phrase
    te vaak herhaald (LLM raakt in een loop bij het opvullen van lengte)."""
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip().lower() for s in sentences if len(s.strip()) > 15]
    if len(sentences) < 6:
        return False
    from collections import Counter
    counts = Counter(sentences)
    # een zin die 3+ keer letterlijk herhaald = degeneratie
    if any(c >= 3 for c in counts.values()):
        return True
    # ook: korte phrases (6+ woorden) die 4+ keer herhalen
    phrases = []
    for s in sentences:
        words = s.split()
        for i in range(0, max(1, len(words) - 5)):
            phrases.append(" ".join(words[i:i+6]))
    pcounts = Counter(phrases)
    return any(c >= 4 for c in pcounts.values())


def write_script(selected, date_str):
    """Genereer het script per categorie (korte aanroepen) en voeg samen; breid \
    daarna globaal uit als het te kort is."""
    cats = group_by_category(selected)
    n_cats = max(1, len(cats))

    parts = []
    parts.append(
        f"Dit is de dagelijkse nieuwtspodcast voor {date_pretty(date_str)}. "
        f"Hier is het belangrijkste nieuws van vandaag."
    )
    parts.append("")

    for cat, items in cats:
        n_items = len(items)
        per_cat_target = max(180, 120 * n_items)  # ~120 woorden per item, min 180
        # sectie-aankondiging als hoorbare structuur
        section_line = SECTION_INTRO.get(cat)
        if section_line:
            parts.append(section_line)
            parts.append("")
        items_text = "\n".join(f"[{s['source']}] {s['summary']}" for s in items)
        prompt = CAT_PROMPT.format(cat=cat, target=per_cat_target, items=items_text, n_items_hint=n_items)
        log(f"  script voor categorie '{cat}' ({n_items} items, doel {per_cat_target} woorden)")
        try:
            chunk = llm_chat([{"role": "user", "content": prompt}], temperature=0.2, num_predict=1800)
        except Exception as e:
            log(f"    categorie '{cat}' faalde: {e} — overslaan")
            continue
        if _is_degenerate(chunk):
            log(f"    categorie '{cat}' gedegenereerd (herhalings-loop) — overslaan")
            continue
        parts.append(chunk)
        parts.append("")

    parts.append("Dat was het nieuws van vandaag. Bedankt voor het luisteren.")
    draft = "\n".join(parts)
    draft_words = len(draft.split())
    log(f"  concept na stap 1: {draft_words} woorden")

    # Stap 2: globale uitbreiding als het concept te kort is
    if draft_words < int(TARGET_WORDS * 0.85):
        log(f"  concept onder doel ({TARGET_WORDS}) — globale uitbreiding (stap 2)")
        sources_text = "\n".join(
            f"[{s['source']}] ({s['category']}) {s['summary']}" for s in selected
        )
        prompt = EXPAND_PROMPT.format(target=TARGET_WORDS, draft=draft, sources=sources_text)
        try:
            expanded = llm_chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                num_predict=3000,
                num_ctx=16384,
            )
            exp_words = len(expanded.split())
            if not _is_clean_dutch(expanded):
                log(f"  uitbreiding gedegenereerd (niet-Latijnse tekens) — houd concept")
            elif _is_degenerate(expanded):
                log(f"  uitbreiding gedegenereerd (herhalings-loop) — houd concept")
            elif exp_words > draft_words:
                log(f"  uitgebreid: {exp_words} woorden (was {draft_words})")
                return expanded
            else:
                log(f"  uitbreiding gaf geen meer ({exp_words}) — houd concept")
        except Exception as e:
            log(f"  uitbreiding faalde: {e} — houd concept")

    return draft


# ---------- Stap 6: audio ----------

PIPER_VOICE = os.environ.get("PIPER_VOICE", "nl_NL-alex-medium.onnx")
PIPER_LENGTH_SCALE = os.environ.get("PIPER_LENGTH_SCALE", "1.15")  # >1 = langzamer


def synthesize(text_path, mp3_path):
    """Piper (offline, nl-NL) -> wav -> ffmpeg -> mp3."""
    voice_path = ROOT / "voices" / PIPER_VOICE
    if not voice_path.exists():
        log(f"piper-stem niet gevonden: {voice_path}")
        return False
    wav_path = mp3_path.with_suffix(".wav")
    piper_bin = shutil.which("piper") or str(ROOT / ".venv" / "bin" / "piper")
    piper_cmd = [
        piper_bin,
        "-m", str(voice_path),
        "-c", str(voice_path.with_suffix(".json")),
        "-i", str(text_path),
        "-f", str(wav_path),
        "--length-scale", PIPER_LENGTH_SCALE,
    ]
    log(f"piper: {PIPER_VOICE} @ length-scale {PIPER_LENGTH_SCALE}")
    res = subprocess.run(piper_cmd, capture_output=True, text=True)
    if res.returncode != 0 or not wav_path.exists():
        log(f"piper fout: {res.stderr[:500]}")
        return False
    # wav -> mp3
    ff_cmd = [
        "ffmpeg", "-y", "-i", str(wav_path),
        "-c:a", "libmp3lame", "-b:a", "96k",
        str(mp3_path),
    ]
    res2 = subprocess.run(ff_cmd, capture_output=True, text=True)
    if res2.returncode != 0:
        log(f"ffmpeg fout: {res2.stderr[:300]}")
        return False
    wav_path.unlink(missing_ok=True)
    return True


# ---------- hoofd ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--max-articles", type=int, default=16)
    ap.add_argument("--out", default=None, help="bestandsnaam zonder extensie")
    args = ap.parse_args()

    global PER_FEED
    if args.max_articles:
        # verklein per-feed als we in totaal minder willen
        PER_FEED = max(2, min(PER_FEED, args.max_articles // 3))

    date_str = dt.date.today().isoformat()
    EPISODES_DIR.mkdir(exist_ok=True)
    base = args.out or f"episode-{date_str}"
    txt_path = EPISODES_DIR / f"{base}.txt"
    mp3_path = EPISODES_DIR / f"{base}.mp3"
    meta_path = EPISODES_DIR / f"{base}.meta.json"
    cache_path = EPISODES_DIR / f"summaries-{date_str}.json"

    with open(SOURCES_FILE) as f:
        sources = yaml.safe_load(f)

    # --- cache van samenvattingen (key = artikel-link) ---
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            log(f"Cache geladen: {len(cache)} samenvattingen vanaf {cache_path.name}")
        except Exception:
            cache = {}

    log("=== Stap 1+2: feeds ophalen ===")
    articles = fetch_feeds(sources)
    log(f"Totaal {len(articles)} artikelen verzameld.")

    log("=== Stap 2b: tekst extraheren ===")
    for a in articles:
        a["text"] = extract_text(a)
        log(f"  [{a['source']}] {len(a['text'])} tekens — {a['title'][:60]}")

    log(f"=== Stap 3: gebalanceerde selectie (max {args.max_articles}) ===")
    articles = deduplicate(articles)
    log(f"  {len(articles)} over na deduplicatie.")
    selected = balance(articles, max_total=args.max_articles)
    log(f"  {len(selected)} geselecteerd.")
    n_world = sum(1 for s in selected if s["region"] == "world")
    log(f"  waarvan {n_world} wereldbronnen, {len(selected) - n_world} NL-bronnen.")

    log("=== Stap 4: per-artikel samenvatten (alleen geselecteerde) ===")
    SHORT_TEXT_LIMIT = 260  # onder deze grens: tekst letterlijk, geen LLM (geen ruimte voor verzinning)
    summaries = []
    n_short = n_llm = n_cache = 0
    for i, a in enumerate(selected, 1):
        text = a["text"]
        # kort tekstje? -> letterlijk als samenvatting, geen LLM, negeer oude cache
        if len(text) < SHORT_TEXT_LIMIT:
            a["summary"] = text
            summaries.append(a)
            if cache.get(a["link"]) != text:
                cache[a["link"]] = text
                try:
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
            n_short += 1
            log(f"  ({i}/{len(selected)}) {a['source']} — kort ({len(text)} tek), letterlijk")
            continue
        if a["link"] in cache:
            a["summary"] = cache[a["link"]]
            summaries.append(a)
            n_cache += 1
            log(f"  ({i}/{len(selected)}) {a['source']} — cache hit")
            continue
        log(f"  ({i}/{len(selected)}) {a['source']} — {a['title'][:50]}")
        try:
            summ = summarize_article(a, a["text"])
        except Exception as e:
            log(f"    samenvatten faalde: {e} — overslaan")
            continue
        a["summary"] = summ
        cache[a["link"]] = summ
        summaries.append(a)
        n_llm += 1
        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    log(f"  samenvattingen: {n_llm} via LLM, {n_cache} uit cache, {n_short} letterlijk (kort).")

    log("=== Stap 5: eindscript schrijven (Ollama) ===")
    script = write_script(summaries, date_str)
    words = len(script.split())
    log(f"  script: {words} woorden (~{words // WPM} min uitlezing @ {WPM} wpm)")

    txt_path.write_text(script, encoding="utf-8")
    log(f"  tekst opgeslagen: {txt_path}")

    meta = {
        "date": date_str,
        "model": OLLAMA_MODEL,
        "tts": "piper",
        "voice": PIPER_VOICE,
        "length_scale": PIPER_LENGTH_SCALE,
        "words": words,
        "est_minutes": words // WPM,
        "n_articles": len(selected),
        "n_world_sources": sum(1 for s in selected if s["region"] == "world"),
        "sources_used": sorted({s["source"] for s in selected}),
        "articles": [
            {"source": s["source"], "category": s["category"], "title": s["title"], "link": s["link"]}
            for s in selected
        ],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.no_tts:
        log("--no-tts gezet: klaar zonder audio.")
        return

    log("=== Stap 6: audio synthese (edge-tts) ===")
    ok = synthesize(txt_path, mp3_path)
    if ok:
        size_mb = mp3_path.stat().st_size / (1024 * 1024)
        log(f"  mp3 klaar: {mp3_path} ({size_mb:.1f} MB)")
        log("KLAAR.")
    else:
        log("Audio synthese faalde — tekst staat wel in " + str(txt_path))


if __name__ == "__main__":
    main()
