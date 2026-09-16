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
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TTS_VOICE = os.environ.get("TTS_VOICE", "nl-NL-ColetteNeural")
TTS_RATE = os.environ.get("TTS_RATE", "-15%")
TARGET_WORDS = int(os.environ.get("TARGET_WORDS", "2400"))
PER_FEED = int(os.environ.get("MAX_ARTICLES_PER_FEED", "4"))

CATEGORY_ORDER = ["binnenland", "buitenland", "economie", "algemeen", "wetenschap", "sport", "cultuur"]
WPM = 120  # gesproken woorden/min, vertraagd B1-tempo


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- Ollama ----------

def llm_chat(messages, temperature=0.2, num_predict=None, num_ctx=8192):
    """Eén LLM-aanroep. Gebruikt Groq (cloud) als GROQ_API_KEY gezet is, anders
    lokale Ollama. Groq is OpenAI-compatible."""
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
        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=600)
        if resp.status_code != 200:
            raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"].strip()
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
        # titel uit slug
        slug = path.split("/", 2)[-1]
        slug = slug.split("?")[0]
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
            articles.append({
                "source": src["name"],
                "region": src["region"],
                "category": src["category"],
                "title": title,
                "link": link,
                "rss_summary": rss_summary,
                "fulltext": src.get("fulltext", True),
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

def _title_tokens(title):
    return set(re.findall(r"[a-z0-9]+", title.lower()))


def deduplicate(summaries):
    """Verwijder exacte duplicaten (zelfde link) en bijna-duplicaten (titel-overlap)."""
    seen_links = set()
    seen_sigs = []
    out = []
    for s in summaries:
        if s["link"] in seen_links:
            continue
        tok = _title_tokens(s["title"])
        dup = False
        for prev_tok in seen_sigs:
            inter = len(tok & prev_tok)
            union = len(tok | prev_tok) or 1
            if inter / union > 0.6:
                dup = True
                break
        if dup:
            continue
        seen_links.add(s["link"])
        seen_sigs.append(tok)
        out.append(s)
    return out


def balance(summaries, max_total=18):
    """Selecteer max_total artikelen, gebalanceerd over categorieën EN over nl/world."""
    by_cat = {}
    for s in summaries:
        by_cat.setdefault(s["category"], []).append(s)

    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat]
    ordered_cats += [c for c in by_cat if c not in CATEGORY_ORDER]

    # garandeer een minimale quota voor wereld-nieuws (niet-NU.nl / world region)
    world = [s for s in summaries if s["region"] == "world"]
    nl = [s for s in summaries if s["region"] != "world"]
    min_world = min(len(world), max(4, max_total // 3))  # ~1/3 uit wereldbronnen

    selected = []
    # eerste ronde:优先 world-bronnen om zeker zichtbaar te zijn
    for s in world:
        if len(selected) >= min_world:
            break
        selected.append(s)
    selected_links = {s["link"] for s in selected}

    # vul aan per categorie (round-robin), vermijd reeds geselecteerde
    queue = [[s for s in by_cat[c] if s["link"] not in selected_links] for c in ordered_cats]
    while queue and len(selected) < max_total:
        progressed = False
        for i in range(len(queue) - 1, -1, -1):
            if not queue[i]:
                queue.pop(i)
                continue
            selected.append(queue[i].pop(0))
            progressed = True
            if len(selected) >= max_total:
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
- Elk nieuwsitem begint met een korte AANKONDIGINGSZIN die het onderwerp benoemt, \
gevolgd door de inhoud. Voorbeeld: "Eerst: de staking van schoonmakers in \
Nederland." Daarna komt de uitwerking. Deze aankondiging werkt als een hoorbare \
scheiding tussen items, zodat de luisteraar weet waar het over gaat.
- Werk elk item uit tot een stukje van enkele zinnen: vertel wat er gebeurt, \
het belang, en de context die in de samenvatting staat. Behandel het niet als \
een koppenlijst.
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

LENGTE — kritiek: schrijf ruim. Doel voor deze categorie: ongeveer {target} \
woorden. Werk elk item uit tot minstens 4-6 zinnen. Als je onder het doel \
blijft, schrijf dan meer uit per item — maar blijf uitsluitend bij de feiten \
uit de samenvattingen.

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


def write_script(selected, date_str):
    """Genereer het script per categorie (korte aanroepen) en voeg samen; breid \
    daarna globaal uit als het te kort is."""
    cats = group_by_category(selected)
    n_cats = max(1, len(cats))
    per_cat_target = max(420, (TARGET_WORDS * 13) // (n_cats * 10))  # ruim doel per categorie

    parts = []
    parts.append(
        f"Dit is de dagelijkse nieuwtspodcast voor {date_pretty(date_str)}. "
        f"Hier is het belangrijkste nieuws van vandaag, in eenvoudig Nederlands."
    )
    parts.append("")

    for cat, items in cats:
        items_text = "\n".join(f"[{s['source']}] {s['summary']}" for s in items)
        prompt = CAT_PROMPT.format(cat=cat, target=per_cat_target, items=items_text)
        log(f"  script voor categorie '{cat}' ({len(items)} items, doel {per_cat_target} woorden)")
        try:
            chunk = llm_chat([{"role": "user", "content": prompt}], temperature=0.2, num_predict=1800)
        except Exception as e:
            log(f"    categorie '{cat}' faalde: {e} — overslaan")
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

    log("=== Stap 3: per-artikel samenvatten (Ollama) ===")
    SHORT_TEXT_LIMIT = 260  # onder deze grens: tekst letterlijk, geen LLM (geen ruimte voor verzinning)
    summaries = []
    n_short = n_llm = n_cache = 0
    for i, a in enumerate(articles, 1):
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
            log(f"  ({i}/{len(articles)}) {a['source']} — kort ({len(text)} tek), letterlijk")
            continue
        if a["link"] in cache:
            a["summary"] = cache[a["link"]]
            summaries.append(a)
            n_cache += 1
            log(f"  ({i}/{len(articles)}) {a['source']} — cache hit")
            continue
        log(f"  ({i}/{len(articles)}) {a['source']} — {a['title'][:50]}")
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

    log(f"=== Stap 4: gebalanceerde selectie (max {args.max_articles}) ===")
    summaries = deduplicate(summaries)
    log(f"  {len(summaries)} over na deduplicatie.")
    selected = balance(summaries, max_total=args.max_articles)
    log(f"  {len(selected)} geselecteerd.")
    n_world = sum(1 for s in selected if s["region"] == "world")
    log(f"  waarvan {n_world} wereldbronnen, {len(selected) - n_world} NL-bronnen.")

    log("=== Stap 5: eindscript schrijven (Ollama) ===")
    script = write_script(selected, date_str)
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
