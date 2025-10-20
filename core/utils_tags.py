# core/utils_tags.py
import logging
import re
import random
import requests
from typing import List

log = logging.getLogger(__name__)

# =========================
# Emoji stripping / cleaning
# =========================

_EMOJI_RE = re.compile(
    "["  # common emoji ranges
    "\U0001F300-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)

def _clean_tag_phrase(p: str) -> str:
    """
    Clean a suggestion phrase for tag use:
    - lowercase, strip emojis
    - remove brackets, numbering, bullets/hashtags
    - collapse spaces
    """
    p = (p or "").lower()
    p = _EMOJI_RE.sub("", p)
    p = re.sub(r"[\[\]\(\)\{\}]", "", p)
    p = re.sub(r"^\s*\d+[\.\)]\s*", "", p)  # 1. tag / 1) tag
    p = p.lstrip("#•*- ").strip()
    p = re.sub(r"\s+", " ", p)
    return p


# =========================
# Autocomplete (YouTube + Google)
# =========================

def fetch_yt_suggestions(seed: str, max_items: int = 20) -> List[str]:
    """YouTube autocomplete via suggest endpoint using the YouTube client."""
    try:
        r = requests.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "youtube", "ds": "yt", "q": seed},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
            out = [s for s in data[1][:max_items] if isinstance(s, str)]
            return out
    except Exception as e:
        log.warning("YT suggest failed for seed=%r: %s", seed, e)
    return []

def fetch_web_suggestions(seed: str, max_items: int = 20) -> List[str]:
    """Google web autocomplete (general)."""
    try:
        r = requests.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": seed},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
            out = [s for s in data[1][:max_items] if isinstance(s, str)]
            return out
    except Exception as e:
        log.warning("Web suggest failed for seed=%r: %s", seed, e)
    return []

def fetch_suggestions(seed: str, suggest_count: int) -> List[str]:
    """
    Merge YT + Google suggestions for the SEED (deduped, trimmed to suggest_count).
    Useful for snapshotting in FileBatch and as a stable source for tags.
    """
    yt = fetch_yt_suggestions(seed, max_items=suggest_count)
    web = fetch_web_suggestions(seed, max_items=suggest_count)
    seen, out = set(), []
    for s in yt + web:
        k = (s or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append((s or "").strip())
        if len(out) >= suggest_count:
            break
    return out


# =========================================
# Phrase-preserving tag line (CHAR LIMIT)
# =========================================

def _build_tag_line_from_full_phrases_char_limit(
    phrases: List[str], char_limit: int, rng: random.Random
) -> str:
    """
    Use FULL cleaned suggestion phrases (no word splitting).
    Build a comma-separated line <= char_limit without cutting any phrase.
    Strategy:
      - clean, dedupe
      - shuffle for randomness
      - greedy fill
      - then try shortest leftovers to pack tighter
    """
    base, seen = [], set()
    for s in phrases:
        c = _clean_tag_phrase(s)
        if c and c not in seen:
            seen.add(c)
            base.append(c)

    if not base:
        return ""

    bag = base[:]
    rng.shuffle(bag)

    picked, total = [], 0
    for phrase in bag:
        add = (", " if picked else "") + phrase
        if total + len(add) <= char_limit:
            picked.append(phrase)
            total += len(add)

    # Try to squeeze shortest remaining phrases if they fit
    remaining = [p for p in base if p not in picked]
    remaining.sort(key=len)  # shortest first
    for phrase in remaining:
        add = (", " if picked else "") + phrase
        if total + len(add) <= char_limit:
            picked.append(phrase)
            total += len(add)

    return ", ".join(picked)


# ===========================================================
# Snapshot-based tags for ALL items (primary, no per-item network)
# ===========================================================

def generate_tags_from_snapshot_char_limit(
    suggestions_snapshot: List[str],
    n_items: int,
    char_limit: int = 400,
    global_seed: int | None = None,
) -> List[str]:
    """
    Build tags for n_items using ONLY the global suggestions_snapshot (full phrases).
    For each item:
      - clean + dedupe phrases
      - shuffle with item-specific seed
      - greedily pack phrases up to <= char_limit (no truncation)
      - try shortest leftovers to snugly fill
    """
    base, seen = [], set()
    for s in suggestions_snapshot or []:
        c = _clean_tag_phrase(s)
        if c and c not in seen:
            seen.add(c)
            base.append(c)

    if not base:
        return [""] * n_items

    results: List[str] = []
    for i in range(n_items):
        rng = random.Random((global_seed or 0) + i + 1337)
        bag = base[:]
        rng.shuffle(bag)

        picked, total = [], 0
        for phrase in bag:
            add = (", " if picked else "") + phrase
            if total + len(add) <= char_limit:
                picked.append(phrase)
                total += len(add)

        leftovers = [p for p in base if p not in picked]
        leftovers.sort(key=len)
        for phrase in leftovers:
            add = (", " if picked else "") + phrase
            if total + len(add) <= char_limit:
                picked.append(phrase)
                total += len(add)

        results.append(", ".join(picked))
    return results


# ===========================================================
# Per-title tags using RANDOM TITLE SEEDS (fallback network path)
# ===========================================================

def generate_tags_per_title_using_random_title_seeds_with_char_limit(
    titles: List[str],
    suggest_count: int,
    char_limit: int = 400,
    global_seed: int | None = None,
) -> List[str]:
    """
    For EACH title:
      - randomly choose a seed title from the FULL title list,
      - fetch YT + Google suggestions for that seed (limit suggest_count),
      - keep FULL phrases (cleaned), shuffled,
      - build a comma-separated line <= char_limit (no phrase truncation).
    Ensures diversified tag lines across items.
    """
    results: List[str] = []
    title_seeds = [t.strip() for t in titles if t and t.strip()]
    if not title_seeds:
        return [""] * len(titles)

    for i, _ in enumerate(titles):
        rng = random.Random((global_seed or 0) + i + 97)  # stable but varied per index
        seed_title = rng.choice(title_seeds)

        yt = fetch_yt_suggestions(seed_title, max_items=suggest_count)
        web = fetch_web_suggestions(seed_title, max_items=suggest_count)

        # merge + dedupe suggestions for this item (as phrases)
        merged, seen = [], set()
        for s in yt + web:
            k = (s or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                merged.append((s or "").strip())
            if len(merged) >= suggest_count:
                break

        line = _build_tag_line_from_full_phrases_char_limit(merged, char_limit, rng)
        results.append(line)

    return results
