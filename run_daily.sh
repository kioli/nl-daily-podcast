#!/bin/bash
# Dagelijkse run: podcast genereren + feed bijwerken.
# Gepland via launchd (nl.daily-podcast.plist) om 05:00.
# Log naar cron.log. Ollama moet draaien (losse daemon).

set -e
cd /Users/lorenzo.marchiori/Projects/nl-daily-podcast

# launchd heeft een minimaal PATH — voeg homebrew toe voor ffmpeg/ollama
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

export MAX_ARTICLES_PER_FEED=4
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:14b}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://kioli.github.io/nl-daily-podcast}"
export AUTO_PUBLISH="${AUTO_PUBLISH:-1}"

echo "=== $(date) — start daily run (model $OLLAMA_MODEL) ==="

.venv/bin/python3 podcast.py --max-articles 22 --out "episode-14b-$(date +%F)"
.venv/bin/python3 generate_feed.py

# Als GitHub Pages-hosting actief is, push de nieuwe aflevering + feed.
if [ -d .git ] && [ "$AUTO_PUBLISH" = "1" ]; then
  git add "episodes/episode-14b-$(date +%F)".* feed.xml 2>/dev/null || true
  git commit -m "daily: episode $(date +%F)" || true
  git push || echo "git push faalde — handmatig pushen"
fi

echo "=== $(date) — klaar ==="
