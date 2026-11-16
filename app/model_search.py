"""Find models to download: the recommended list, the whole Ollama library and Hugging Face.

The Ollama library has no API, so its listing page (ollama.com/library, every model on one page)
is read and cached for 6 hours. Hugging Face is searched live through its public API
(huggingface.co/api/models, GGUF models only, which Ollama can download as hf.co/USER/REPO).

Everything is turned into one shape so the page can filter and show them the same way:
  {id, source, name, title, about, use, uses[], tools, thinking, vision, embedding, audio, image,
   cloud, experimental, variants[{tag, label, params, size, ram, fits, installed}], popularity,
   popularity_label, likes, updated, url, installed}
Sizes and memory needs that the sites don't publish are estimates from the parameter count
(4-bit quantization), marked "estimated" for the page.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psutil

UA = "SysAdmin.IA (model search)"
LIBRARY_URL = "https://ollama.com/library"
HF_API = "https://huggingface.co/api/models"
LIBRARY_TTL = 6 * 3600
HF_TTL = 600
PAGE = 48

USES = [
    ("chat", "Chat & writing"), ("tools", "Runs commands (tools)"), ("reasoning", "Reasoning (thinks first)"),
    ("code", "Coding"), ("vision", "Reads images"), ("embedding", "Embeddings / search"),
    ("audio", "Audio & speech"), ("image", "Image generation"),
]
PARAM_BUCKETS = [  # id, label, min (B), max (B)
    ("lt1", "Under 1B (tiny)", 0, 1), ("1-4", "1–4B (small)", 1, 4), ("4-10", "4–10B (medium)", 4, 10),
    ("10-35", "10–35B (large)", 10, 35), ("35-100", "35–100B (very large)", 35, 100), ("gt100", "Over 100B (huge)", 100, 1e9),
]
SORTS = [("popular", "Most popular"), ("newest", "Recently updated"), ("smallest", "Smallest"),
         ("largest", "Largest"), ("name", "Name")]
SOURCES = [("recommended", "Recommended"), ("ollama", "Ollama library"), ("hf", "Hugging Face")]

# Mixture-of-experts sizes written as experts x size: real totals (the product overstates them).
_MOE = {"8x7b": 46.7, "8x22b": 141, "16x17b": 109, "128x17b": 400}
_CODE = re.compile(r"cod(e|er|ing)|starcoder|devstral|codestral|sql", re.I)
_REASON = re.compile(r"(^|[^a-z])(r1|qwq|reason\w*|think\w*|o1)([^a-z]|$)", re.I)


class SearchError(RuntimeError):
    pass


# ---------------------------------------------------------------- shared helpers

def _fetch(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # network down, DNS, HTTP error…
        host = urllib.parse.urlparse(url).hostname
        raise SearchError(f"Couldn't reach {host}: {getattr(e, 'reason', e)}")


def parse_params(label: str) -> float | None:
    """'8b' → 8, '270m' → 0.27, 'e4b' → 4 (effective size), '8x7b' → 46.7, '35B-A3B' → 35."""
    s = (label or "").strip().lower()
    if s in _MOE:
        return _MOE[s]
    m = re.fullmatch(r"(\d+)x(\d+(?:\.\d+)?)b", s)
    if m:
        return round(int(m.group(1)) * float(m.group(2)) * 0.8, 1)
    m = re.fullmatch(r"e?(\d+(?:\.\d+)?)([bmk])", s)
    if not m:
        return None
    n = float(m.group(1))
    n = n if m.group(2) == "b" else n / 1000 if m.group(2) == "m" else n / 1e6
    return n if n >= 0.01 else None   # e.g. internlm2's "1m" tag is a context length, not 1M parameters


def params_from_name(name: str) -> float | None:
    """Parameter count written in a model or repo name: 'Qwen3-30B-A3B-GGUF' → 30, 'gemma-3-270m' → 0.27."""
    # A number glued to a letter is skipped, so "-A3B" (active parameters of a mixture-of-experts
    # model) and "gemma3n-e4b" don't count; the largest remaining number is the total.
    best = None
    for m in re.finditer(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*([bm])(?![a-z])", name or "", re.I):
        v = float(m.group(1)) / (1000 if m.group(2).lower() == "m" else 1)
        best = v if best is None else max(best, v)
    return best


def estimate(params: float | None, ram_total_gb: float) -> dict:
    """Approximate 4-bit download size and memory needed for a model of this many billion parameters."""
    if params is None:
        return {"size": None, "ram": None, "fits": None, "estimated": True}
    size = round(params * 0.6 + 0.05, 1 if params >= 1 else 2)
    ram = max(1, round(size * 1.2 + 0.7))
    return {"size": size, "ram": ram, "fits": ram <= ram_total_gb * 0.9, "estimated": True}


def size_label(gb: float | None) -> str:
    if gb is None:
        return ""
    return f"{round(gb * 1000)} MB" if gb < 1 else f"{gb:g} GB"


def use_summary(m: dict) -> str:
    """One or two plain sentences on what the model is good for."""
    if m.get("embedding"):
        return "Best for: turning text into vectors for semantic search and RAG (finding related documents). Not a chat model."
    if m.get("image"):
        return "Best for: creating images from a text description. Experimental in Ollama; the chat can't show images yet."
    if m.get("audio") and not m.get("tools"):
        return "Best for: working with audio (speech recognition or text-to-speech). Ollama support for audio models is limited."
    good = []
    if m.get("code"):
        good.append("writing, explaining and fixing code, scripts and config files")
    if m.get("vision"):
        good.append("reading screenshots, photos and scanned documents")
    if not good:
        good.append("general questions, writing, summaries and translation")
    if m.get("thinking"):
        good.append("hard problems it reasons through step by step (slower, more accurate)")
    s = "Best for: " + "; ".join(good) + "."
    if m.get("tools") is True:
        s += " Can run commands on this server from the chat."
    elif m.get("tools") is False:
        s += " Can't run commands: answers questions only."
    else:
        s += " Whether it can run commands shows after downloading."
    ps = [v["params"] for v in m.get("variants", []) if v.get("params")]
    if ps and max(ps) <= 2:
        s += " Small enough for weak servers."
    elif ps and min(ps) >= 70:
        s += " Needs a very powerful server."
    return s


def _uses(m: dict) -> list[str]:
    u = [k for k in ("tools", "vision", "embedding", "audio", "image") if m.get(k)]
    if m.get("thinking"):
        u.append("reasoning")
    if m.get("code"):
        u.append("code")
    if not (m.get("embedding") or m.get("image")):
        u.append("chat")
    return u


def _finish(m: dict, have: set[str]) -> dict:
    for v in m["variants"]:
        tag = v["tag"]
        full = tag if ":" in tag.rsplit("/", 1)[-1] else f"{tag}:latest"
        v["installed"] = tag in have or full in have
    m["installed"] = any(v["installed"] for v in m["variants"])
    m["uses"] = _uses(m)
    m["use"] = use_summary(m)
    return m


def _ram_gb() -> float:
    return psutil.virtual_memory().total / 2**30


# ---------------------------------------------------------------- recommended (curated list in local_ai)

def recommended(have: set[str], ram: float) -> list[dict]:
    from .local_ai import CATALOG
    out = []
    for i, c in enumerate(CATALOG):
        tag = c["name"]
        params = params_from_name(tag.split(":", 1)[1]) if ":" in tag and not tag.startswith("hf.co/") else None
        params = params or params_from_name(c["title"])
        hf = tag.startswith("hf.co/")
        repo = tag[6:].split(":")[0] if hf else tag.split(":")[0]
        m = {
            "id": f"rec:{tag}", "source": "Hugging Face" if hf else "Ollama library", "name": tag,
            "title": c["title"], "about": c["about"], "cat": c["cat"], "order": i,
            "tools": c.get("tools"), "thinking": bool(c.get("thinking")), "vision": bool(c.get("vision")),
            "embedding": bool(c.get("embedding")), "image": bool(c.get("image")), "audio": False,
            "code": c["cat"] == "code", "cloud": False, "experimental": bool(c.get("experimental")),
            "variants": [{"tag": tag, "label": (tag.split(":", 1)[1] if ":" in tag else "latest").upper() if not hf else tag.rsplit(":", 1)[-1],
                          "params": params, "size": c["size"], "ram": c["ram"], "estimated": False,
                          "fits": None if c["ram"] is None else c["ram"] <= ram * 0.9}],
            "popularity": None, "popularity_label": "", "likes": None, "updated": None,
            "url": f"https://huggingface.co/{repo}" if hf else f"https://ollama.com/library/{repo}",
        }
        out.append(_finish(m, have))
    return out


# ---------------------------------------------------------------- Ollama library (scraped, cached)

_lib = {"at": 0.0, "models": [], "error": None}
_lib_lock = threading.Lock()
_LI = re.compile(r'<li\s+[^>]*class="flex items-baseline[^"]*"[^>]*>(.*?)</li>', re.S)


def _count(text: str) -> int | None:
    m = re.fullmatch(r"([\d.]+)\s*([KMB]?)", text.strip(), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def parse_library(page: str) -> list[dict]:
    out = []
    for block in _LI.findall(page):
        m = re.search(r'href="/library/([^"/?#]+)"', block)
        if not m:
            continue
        name = html.unescape(m.group(1))
        desc = re.search(r'<p class="max-w-lg[^"]*">(.*?)</p>', block, re.S)
        badges = [(html.unescape(b).strip().lower(), color) for color, b in
                  re.findall(r'<span[^>]*class="[^"]*rounded-md (bg-\S+)[^"]*"[^>]*>([^<]+)</span>', block)]
        caps = {b for b, color in badges if "ddf4ff" not in color}
        sizes = [b for b, color in badges if "ddf4ff" in color]
        pulls = re.search(r"<span\s*>([\d.]+[KMB]?)</span>\s*<span[^>]*>&nbsp;Pulls", block)
        upd = re.search(r'title="([A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M) UTC"', block)
        updated = None
        if upd:
            try:
                updated = datetime.strptime(upd.group(1), "%b %d, %Y %I:%M %p").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        about = html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else ""
        out.append({"name": name, "about": about, "caps": sorted(caps), "sizes": sizes,
                    "pulls": _count(pulls.group(1)) if pulls else None,
                    "pulls_label": pulls.group(1) if pulls else "", "updated": updated})
    return out


def library(force: bool = False) -> dict:
    """The whole Ollama library (cached). On a network error the last good copy is kept."""
    with _lib_lock:
        if force or not _lib["models"] or time.time() - _lib["at"] > LIBRARY_TTL:
            try:
                models = parse_library(_fetch(LIBRARY_URL).decode("utf-8", "replace"))
                if not models:
                    raise SearchError("ollama.com/library changed its layout; only the recommended list is available.")
                _lib.update(at=time.time(), models=models, error=None)
            except SearchError as e:
                _lib["error"] = str(e)
                if not _lib["models"]:
                    _lib["at"] = time.time() - LIBRARY_TTL + 120   # retry in 2 minutes, not on every request
        return dict(_lib)


def _from_library(r: dict, have: set[str], ram: float) -> dict:
    caps = set(r["caps"])
    variants = []
    for s in r["sizes"]:
        p = parse_params(s)
        variants.append({"tag": f"{r['name']}:{s}", "label": s.upper(), "params": p, **estimate(p, ram)})
    if not variants and "cloud" not in caps:
        p = params_from_name(r["name"])
        variants.append({"tag": r["name"], "label": "latest", "params": p, **estimate(p, ram)})
    m = {
        "id": f"ollama:{r['name']}", "source": "Ollama library", "name": r["name"], "title": r["name"],
        "about": r["about"], "tools": "tools" in caps, "thinking": "thinking" in caps, "vision": "vision" in caps,
        "embedding": "embedding" in caps, "audio": "audio" in caps, "image": False,
        "code": bool(_CODE.search(r["name"])) or bool(re.search(r"\bcod(e|ing)\b", r["about"], re.I)),
        "cloud": "cloud" in caps, "cloud_only": not variants, "experimental": False,
        "variants": variants, "popularity": r["pulls"],
        "popularity_label": f"{r['pulls_label']} pulls" if r["pulls_label"] else "", "likes": None,
        "updated": r["updated"], "url": f"https://ollama.com/library/{r['name']}",
    }
    if m["embedding"]:
        m["tools"] = False
    return _finish(m, have)


# ---------------------------------------------------------------- Hugging Face (live API, cached briefly)

_hf_cache: dict[str, tuple[float, list]] = {}
_HF_PIPE = {"chat": "text-generation", "vision": "image-text-to-text", "embedding": "feature-extraction",
            "image": "text-to-image", "audio": "text-to-speech", "tools": "text-generation"}
_RUNNABLE = {None, "", "text-generation", "image-text-to-text", "feature-extraction", "sentence-similarity",
             "text2text-generation", "image-to-text", "any-to-any"}
_AUDIO = {"text-to-speech", "automatic-speech-recognition", "audio-to-audio", "text-to-audio", "audio-text-to-text"}


def _hf_query(q: str, use: str, bucket: str, sort: str) -> list[dict]:
    params = [("filter", "gguf"), ("limit", "100"), ("full", "false"),
              ("sort", {"newest": "lastModified", "popular": "downloads"}.get(sort, "downloads")), ("direction", "-1")]
    for e in ("downloads", "likes", "pipeline_tag", "tags", "lastModified", "cardData"):
        params.append(("expand[]", e))
    search = q.strip()
    if use == "code" and not search:
        search = "coder"
    if search:
        params.append(("search", search[:80]))
    if use in _HF_PIPE:
        params.append(("pipeline_tag", _HF_PIPE[use]))
    b = next((b for b in PARAM_BUCKETS if b[0] == bucket), None)
    if b:
        hi = "" if b[3] >= 1e9 else f",max:{b[3]}B"
        params.append(("num_parameters", f"min:{b[2]}B{hi}"))
    url = HF_API + "?" + urllib.parse.urlencode(params)
    hit = _hf_cache.get(url)
    if hit and time.time() - hit[0] < HF_TTL:
        return hit[1]
    try:
        data = json.loads(_fetch(url))
    except json.JSONDecodeError:
        raise SearchError("Hugging Face returned an unexpected answer.")
    if not isinstance(data, list):
        raise SearchError(f"Hugging Face: {data.get('error') if isinstance(data, dict) else 'unexpected answer'}")
    if len(_hf_cache) > 200:
        _hf_cache.clear()
    _hf_cache[url] = (time.time(), data)
    return data


def _from_hf(r: dict, have: set[str], ram: float) -> dict:
    rid = r.get("id") or r.get("modelId") or ""
    repo = rid.split("/", 1)[-1]
    tags = {str(t).lower() for t in r.get("tags") or []}
    card = r.get("cardData") or {}
    pipe = r.get("pipeline_tag") or card.get("pipeline_tag")
    base = card.get("base_model")
    if isinstance(base, list):
        base = base[0] if base else None
    title = re.sub(r"[-_.]gguf$", "", repo, flags=re.I).replace("-", " ").replace("_", " ")
    p = params_from_name(repo) or (params_from_name(base) if base else None)
    author = rid.split("/", 1)[0]
    title = f"{title} · {author}"   # the same model is often uploaded by several people
    about = f"GGUF build of {base} by {author}." if base else f"GGUF model by {author}."
    if card.get("license"):
        about += f" License: {card['license']}."
    vision = pipe in ("image-text-to-text", "image-to-text") or "vision" in tags or "multimodal" in tags
    m = {
        "id": f"hf:{rid}", "source": "Hugging Face", "name": f"hf.co/{rid}", "title": title, "about": about,
        "tools": None, "vision": vision,
        "thinking": bool(_REASON.search(repo)) or "reasoning" in tags,
        "embedding": pipe in ("feature-extraction", "sentence-similarity") or "embeddings" in tags,
        "audio": pipe in _AUDIO, "image": pipe == "text-to-image",
        "code": bool(_CODE.search(repo)), "cloud": False, "experimental": pipe == "text-to-image",
        "runnable": pipe in _RUNNABLE,
        "variants": [{"tag": f"hf.co/{rid}", "label": "Q4_K_M (default)", "params": p, **estimate(p, ram)}],
        "popularity": r.get("downloads"), "popularity_label": f"{_short(r.get('downloads'))} downloads" if r.get("downloads") else "",
        "likes": r.get("likes"), "updated": r.get("lastModified"), "url": f"https://huggingface.co/{rid}",
    }
    if m["embedding"]:
        m["tools"] = False
    return _finish(m, have)


def _short(n) -> str:
    if not n:
        return "0"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(n)


# ---------------------------------------------------------------- search (filters are applied here)

def _matches_text(m: dict, q: str) -> bool:
    hay = " ".join([m["name"], m["title"], m["about"], " ".join(m["uses"])]).lower()
    return all(w in hay for w in q.lower().split())


def _in_bucket(v: dict, bucket: str) -> bool:
    b = next((b for b in PARAM_BUCKETS if b[0] == bucket), None)
    if not b:
        return True
    return v.get("params") is not None and b[2] <= v["params"] < b[3]


def _sort(models: list[dict], sort: str) -> None:
    def size(m, pick):
        ps = [v["params"] for v in m["variants"] if v.get("params") is not None]
        return pick(ps) if ps else None
    if sort == "name":
        models.sort(key=lambda m: m["title"].lower())
    elif sort == "newest":
        models.sort(key=lambda m: m.get("updated") or "", reverse=True)
    elif sort == "smallest":
        models.sort(key=lambda m: (size(m, min) is None, size(m, min) or 0))
    elif sort == "largest":
        models.sort(key=lambda m: (size(m, max) is None, -(size(m, max) or 0)))
    elif sort == "popular":
        models.sort(key=lambda m: -(m.get("popularity") or 0))


def search(source: str = "recommended", q: str = "", use: str = "", params: str = "", fits: bool = False,
           sort: str = "", offset: int = 0, have: list[str] | None = None, refresh: bool = False) -> dict:
    from .local_ai import installed_names
    have_set = set(installed_names() if have is None else have)
    ram = _ram_gb()
    notes: list[str] = []
    error = None
    fetched = None
    if source == "ollama":
        lib = library(force=refresh)
        error, fetched = lib["error"], (lib["at"] or None)
        models = [_from_library(r, have_set, ram) for r in lib["models"]]
        cloud_only = [m for m in models if m["cloud_only"]]
        models = [m for m in models if not m["cloud_only"]]
        if cloud_only:
            notes.append(f"{len(cloud_only)} cloud-only models are hidden: they run on ollama.com, not on this server.")
        sort = sort or "popular"
    elif source == "hf":
        try:
            raw = _hf_query(q, use, params, sort or "popular")
        except SearchError as e:
            raw, error = [], str(e)
        fetched = time.time()
        models = [_from_hf(r, have_set, ram) for r in raw]
        if use not in ("audio", "image"):
            skipped = [m for m in models if not m["runnable"]]
            models = [m for m in models if m["runnable"]]
            if skipped:
                notes.append(f"{len(skipped)} results hidden because Ollama can't run that kind of model "
                             "(speech, music, etc.). Choose Audio & speech to see them.")
        notes.append("Hugging Face shows its top 100 matches: type a name to narrow it down. Sizes are estimates for the "
                     "default 4-bit (Q4_K_M) file; to pick another, type hf.co/user/repo:QUANT in the box above.")
        sort = sort or "popular"
    else:
        source = "recommended"
        models = recommended(have_set, ram)
        notes.append("Video generation isn't supported by Ollama, so no video models are listed.")
    # filters
    if q and source != "hf":
        models = [m for m in models if _matches_text(m, q)]
    elif q:
        models = [m for m in models if _matches_text(m, q) or q.lower() in m["name"].lower()] or models
    if use == "tools" and source == "hf":
        notes.insert(0, "Hugging Face doesn't say which models can run commands (tools); it shows after downloading. "
                        "Showing chat models instead.")
        models = [m for m in models if "chat" in m["uses"]]
    elif use:
        models = [m for m in models if use in m["uses"]]
    if params or fits:
        kept = []
        for m in models:
            vs = [v for v in m["variants"] if _in_bucket(v, params) and (not fits or v.get("fits") is not False)]
            if vs:
                kept.append({**m, "variants": vs})
        models = kept
    if sort:
        _sort(models, sort)
    total = len(models)
    offset = max(0, int(offset or 0))
    return {
        "source": source, "total": total, "offset": offset, "models": models[offset:offset + PAGE],
        "more": offset + PAGE < total, "error": error, "notes": notes,
        "fetched": datetime.fromtimestamp(fetched, timezone.utc).isoformat() if fetched else None,
        "ram_total": round(ram, 1),
        "options": {"sources": SOURCES, "uses": USES, "params": [(b[0], b[1]) for b in PARAM_BUCKETS], "sorts": SORTS},
    }
