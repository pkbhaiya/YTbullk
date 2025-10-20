# core/utils_youtube.py
import time
import re
import requests
from typing import List, Dict, Optional

YOUTUBE_API = "https://www.googleapis.com/youtube/v3/videos"
SEARCH_API  = "https://www.googleapis.com/youtube/v3/search"

_ISO8601_ANY = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

def _parse_iso8601_duration(dur: str) -> int:
    s = dur or ""
    m = _ISO8601_ANY.match(s)
    if not m:
        return 10**9
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds

def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen, out = set(), []
    for t in items:
        k = " ".join((t or "").split()).strip().casefold()
        if k and k not in seen:
            seen.add(k)
            out.append((t or "").strip())
    return out

def fetch_video_stats_batch(video_ids: List[str], api_key: str, throttle_ms: int = 250) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    if not video_ids:
        return out

    def chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    for chunk in chunks(video_ids, 50):
        params = {"part": "statistics", "id": ",".join(chunk), "key": api_key}
        r = requests.get(YOUTUBE_API, params=params, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
        for item in (data.get("items") or []):
            vid = item.get("id")
            stats = item.get("statistics", {}) or {}
            out[vid] = {
                "views": int(stats.get("viewCount", 0) or 0),
                "likes": int(stats.get("likeCount", 0) or 0),
            }
        time.sleep(throttle_ms / 1000.0)
    return out

# ---------- titles: Top results (no Shorts filter) ----------
def fetch_youtube_titles(
    keyword: str,
    count: int = 10,
    api_key: Optional[str] = None,
    region: str = "IN",
    relevance_language: Optional[str] = None,   # e.g. "hi" to bias Hindi; None for neutral
    include_shorts_token: bool = False,         # ignored by default; set True if you want to bias to #shorts
) -> List[str]:
    """
    Return up to `count` video titles for `keyword`, regardless of duration (Shorts or long).
    - Uses YouTube Search API with pagination until `count` titles collected.
    - Biases to `region` (default IN). No Shorts filter. No duration calls.
    - Tries multiple orders to fill quota: relevance -> viewCount -> date.
    """
    if not api_key:
        try:
            from .models import SiteSettings
            s = SiteSettings.objects.first()
            api_key = s.youtube_api_key if s else None
        except Exception:
            api_key = None
    if not api_key:
        raise RuntimeError("YouTube API key missing for fetch_youtube_titles")

    collected: List[str] = []

    def _collect(order: str, need: int):
        nonlocal collected
        page_token = None
        q = keyword.strip()
        if include_shorts_token:
            q = f"{q} #shorts"  # optional bias, but not required

        # paginate until we hit the need or run out
        for _ in range(10):  # up to ~500 results per order (API cap is 50/page)
            if len(collected) >= need:
                return
            params = {
                "key": api_key,
                "part": "snippet",
                "q": q,
                "type": "video",
                "order": order,
                "regionCode": region,
                "maxResults": min(50, need - len(collected)),
                "safeSearch": "none",
            }
            if relevance_language:
                params["relevanceLanguage"] = relevance_language
            if page_token:
                params["pageToken"] = page_token

            r = requests.get(SEARCH_API, params=params, timeout=20)
            r.raise_for_status()
            data = r.json() or {}
            items = data.get("items") or []
            if not items:
                break

            for it in items:
                snippet = it.get("snippet") or {}
                title = (snippet.get("title") or "").strip()
                if title:
                    collected.append(title)
                    if len(collected) >= need:
                        break

            collected[:] = _dedupe_keep_order(collected)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    # Try multiple sort orders to fill the quota
    _collect("relevance", count)
    if len(collected) < count:
        _collect("viewCount", count)
    if len(collected) < count:
        _collect("date", count)

    return collected[:count]
