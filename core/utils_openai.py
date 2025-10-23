# core/utils_openai.py
from __future__ import annotations

import os
import re
import time
from collections import Counter
from typing import Iterable, List, Optional, Tuple

# --- optional OpenAI client (supports both v1+ "from openai import OpenAI" and legacy "import openai") ---
_CLIENT_KIND = None  # "v1", "legacy", or None
_OpenAIClient = None

try:
    # New SDK style (>=1.0)
    from openai import OpenAI  # type: ignore

    _OpenAIClient = OpenAI
    _CLIENT_KIND = "v1"
except Exception:
    try:
        import openai  # type: ignore

        _OpenAIClient = openai
        _CLIENT_KIND = "legacy"
    except Exception:
        _OpenAIClient = None
        _CLIENT_KIND = None


# ----------------------------
# Helpers
# ----------------------------
_STOPWORDS = {
    # very small English stoplist; expand if needed
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of", "on", "in", "to",
    "with", "at", "by", "from", "up", "down", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "you", "your", "i", "we", "they", "he",
    "she", "them", "our", "us", "me", "my", "mine", "yours",
}

_EMOJI_RE = re.compile(
    "[\U0001F1E0-\U0001F1FF]"  # flags
    "|[\U0001F300-\U0001F5FF]"  # symbols & pictographs
    "|[\U0001F600-\U0001F64F]"  # emoticons
    "|[\U0001F680-\U0001F6FF]"  # transport & map
    "|[\U0001F700-\U0001F77F]"
    "|[\U0001F780-\U0001F7FF]"
    "|[\U0001F800-\U0001F8FF]"
    "|[\U0001F900-\U0001F9FF]"
    "|[\U0001FA00-\U0001FA6F]"
    "|[\U0001FA70-\U0001FAFF]"
    "|[\U00002700-\U000027BF]"  # dingbats
    "|[\U00002600-\U000026FF]",  # misc symbols
    flags=re.UNICODE,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _chunk(seq: List[str], n: int) -> Iterable[List[str]]:
    if n <= 0:
        n = 1
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ----------------------------
# Public API: Keywords
# ----------------------------
def extract_global_keywords_from_titles(
    titles: List[str],
    max_unigrams: int = 80,
    max_bigrams: int = 80,
    min_len: int = 3,
) -> List[str]:
    """
    Naive keyword extractor from titles. Returns top unigrams + bigrams.
    - Filters stopwords and tokens shorter than min_len.
    - Case-insensitive; numbers allowed.
    """
    if not titles:
        return []

    # Unigrams
    words: List[str] = []
    for t in titles:
        toks = _TOKEN_RE.findall((t or "").lower())
        for w in toks:
            if len(w) >= min_len and w not in _STOPWORDS:
                words.append(w)

    top_uni = [w for w, _ in Counter(words).most_common(max_unigrams)]

    # Bigrams
    bigram_corpus: List[str] = []
    for t in titles:
        toks = [w for w in _TOKEN_RE.findall((t or "").lower()) if len(w) >= min_len and w not in _STOPWORDS]
        if len(toks) >= 2:
            bigram_corpus.extend(f"{a} {b}" for a, b in zip(toks, toks[1:]))

    top_bi = [b for b, _ in Counter(bigram_corpus).most_common(max_bigrams)]

    return top_uni + top_bi


# ----------------------------
# Public API: Descriptions
# ----------------------------
def generate_all_descriptions(
    openai_api_key: Optional[str],
    titles: List[str],
    global_keywords: List[str],
    desc_len: int = 200,
    strip_emojis: bool = True,
    model: str = "gpt-4o-mini",
    temperature: float = 0.7,
    max_tokens: int = 512,
    batch_size: int = 4,
    max_retries: int = 2,
) -> List[str]:
    """
    Returns a list of English descriptions (len == len(titles)).
    - If OpenAI SDK + API key are available, calls the model (one title at a time; batched loop).
    - Otherwise, falls back to a deterministic, emoji-free builder.
    """
    titles = titles or []
    kws = " ".join(global_keywords[:30]) if global_keywords else ""

    if _OpenAIClient and openai_api_key:
        try:
            if _CLIENT_KIND == "v1":
                client = _OpenAIClient(api_key=openai_api_key)  # type: ignore[call-arg]
            else:
                # legacy client
                _OpenAIClient.api_key = openai_api_key  # type: ignore[attr-defined]
                client = _OpenAIClient
            return _generate_via_openai(
                client=client,
                kind=_CLIENT_KIND or "legacy",
                titles=titles,
                kws=kws,
                desc_len=desc_len,
                temperature=temperature,
                max_tokens=max_tokens,
                batch_size=batch_size,
                max_retries=max_retries,
                strip_emoji=strip_emojis,
                model=model,
            )
        except Exception:
            # fall back silently to deterministic when API fails
            pass

    # Fallback path (no SDK / no key / API error)
    return [_fallback_description(t, kws, desc_len, strip_emojis) for t in titles]


def _fallback_description(title: str, kws: str, desc_len: int, do_strip_emojis: bool) -> str:
    base = f"{_normalize_ws(title)}. {_normalize_ws(kws)}".strip()
    if do_strip_emojis:
        base = _strip_emojis(base)
    if desc_len and len(base) > desc_len:
        base = base[:desc_len].rstrip()
    return base


def _prompt_for(title: str, kws: str, desc_len: int, strip_emoji: bool) -> str:
    rules = [
        "Write a concise YouTube Shorts description in natural English.",
        f"Target length: up to {max(60, desc_len)} characters.",
        "Use only words present in the title or the provided keywords.",
        "No emojis. No hashtags. No URLs.",
        "Keep it one paragraph; no bullet points.",
    ]
    joined_rules = " ".join(rules)
    body = f"Title: {title}\nKeywords: {kws}\nDescription:"
    return f"{joined_rules}\n\n{body}"


def _generate_via_openai(
    client,
    kind: str,
    titles: List[str],
    kws: str,
    desc_len: int,
    temperature: float,
    max_tokens: int,
    batch_size: int,
    max_retries: int,
    strip_emoji: bool,
    model: str,
) -> List[str]:
    out: List[str] = []
    for batch in _chunk(titles, batch_size):
        for title in batch:
            prompt = _prompt_for(title=_normalize_ws(title), kws=_normalize_ws(kws), desc_len=desc_len, strip_emoji=strip_emoji)
            desc = _call_openai_with_retries(
                client=client,
                kind=kind,
                model=model,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
            desc = _normalize_ws(desc)
            if strip_emoji:
                desc = _strip_emojis(desc)
            if desc_len and len(desc) > desc_len:
                desc = desc[:desc_len].rstrip()
            out.append(desc)
    # pad if anything failed
    while len(out) < len(titles):
        t = titles[len(out)]
        out.append(_fallback_description(t, kws, desc_len, strip_emoji))
    return out


def _call_openai_with_retries(
    client,
    kind: str,
    model: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    max_retries: int,
) -> str:
    attempt = 0
    last_err: Optional[Exception] = None
    while attempt <= max_retries:
        try:
            if kind == "v1":
                # New SDK
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a concise assistant that writes short, fluent English descriptions without emojis or URLs."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                text = (resp.choices[0].message.content or "").strip()
            else:
                # Legacy SDK
                # Some older environments only support ChatCompletion under openai
                if hasattr(client, "ChatCompletion"):
                    resp = client.ChatCompletion.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "You are a concise assistant that writes short, fluent English descriptions without emojis or URLs."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    text = (resp["choices"][0]["message"]["content"] or "").strip()
                else:
                    # Fallback to text completion if chat isn't available
                    resp = client.Completion.create(  # type: ignore[attr-defined]
                        model=model,
                        prompt=prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    text = (resp["choices"][0]["text"] or "").strip()
            if text:
                return text
            # empty? fall back to deterministic
            return ""
        except Exception as e:
            last_err = e
            # simple backoff
            time.sleep(0.8 * (attempt + 1))
            attempt += 1
    # give up; let caller use deterministic fallback
    return ""


__all__ = [
    "extract_global_keywords_from_titles",
    "generate_all_descriptions",
]
