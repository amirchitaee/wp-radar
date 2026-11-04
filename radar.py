#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radar engine: feeds -> queue -> Persian post -> Telegram.

Commands:
  python radar.py security   # advisories, no model, immediate
  python radar.py news       # core news, model
  python radar.py tools      # ai tools and free offers, model
  python radar.py showcase   # 3d / ai built sites, model
  python radar.py digest     # nightly roundup of leftovers
  python radar.py report     # weekly coverage report
  python radar.py watchdog   # external health check
Add --dry to print instead of posting.
"""

import os
import re
import sys
import json
import html
import time
import gzip
import socket
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()
STATE = ROOT / "state"
ARCH = ROOT / "archive"
SEEN = STATE / "seen.json"
QUEUE = STATE / "queue.json"
STATS = STATE / "sources.json"
DAILY = STATE / "daily.json"
LAST = STATE / "last_run.json"
LAST_START = STATE / "last_start.json"
STORIES = STATE / "stories.json"
SCHED = STATE / "schedule.json"
READY = STATE / "ready.json"
PLUGIN_STATE = STATE / "plugins.json"
ARCHIVE = ARCH / "items.jsonl"

def env_clean(name):
    return "".join(os.environ.get(name, "").split()).strip()


API_BASE = env_clean("API_BASE").rstrip("/")
API_KEY = env_clean("API_KEY")
TG_TOKEN = env_clean("TG_TOKEN")
TG_CHAT = env_clean("TG_CHAT")
TG_ADMIN = env_clean("TG_ADMIN")

DRY = "--dry" in sys.argv
TZ = timezone(timedelta(minutes=210))


def log(*args):
    print(*args, flush=True)


def now():
    return datetime.now(timezone.utc)


def today():
    return now().astimezone(TZ).date().isoformat()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def settings():
    return read_json(
        ROOT / "settings.json",
        {
            "daily_cap": 10,
            "queue_days": 7,
            "max_items_per_call": 30,
            "max_posts_per_run": 10,
            "models": ["Gemma-4-31B-IT"],
        },
    )


# ---------------------------------------------------------------- http

def http_get(url, timeout=20, headers=None):
    hdrs = {
        "User-Agent": "Mozilla/5.0 (compatible; RadarBot/2.0)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, identity",
    }
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def http_post_json(url, payload, headers=None, timeout=90, retry_5xx=True):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    # بدون این خط، کلادفلر بعضی سرویس‌ها را با خطای ۱۰۱۰ رد می‌کند.
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; RadarBot/2.0)")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # خطای ۵۰۰ این سرویس تصادفی است: همان درخواست دقیقاً با همان
        # پارامترها یک بار ۵۰۰ داد و بار بعد جواب درست. دو تلاش دوباره
        # ارزان‌تر از خواباندن کل لایه برای ده دقیقه است.
        if not (retry_5xx and 500 <= exc.code < 600):
            raise
        for wait in (2, 5):
            time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as again:
                if not (500 <= again.code < 600):
                    raise
        raise


def link_alive(url):
    if not url.startswith("http"):
        return False
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405, 429):
                return True
            if exc.code == 404:
                return False
        except Exception:
            continue
    return True


# ---------------------------------------------------------------- feeds

def clean_text(value):
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*(Read More|Continue reading|ادامه مطلب).*$", "", value, flags=re.I)
    value = re.sub(r"\s*The post .*? appeared first on .*$", "", value, flags=re.I)
    return value.strip()


def norm_link(url):
    url = (url or "").split("#")[0].strip()
    if "?" in url:
        base, query = url.split("?", 1)
        keep = [p for p in query.split("&") if not p.lower().startswith(("utm_", "fbclid", "gclid"))]
        url = base + ("?" + "&".join(keep) if keep else "")
    return url.rstrip("/")


def parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        d = parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def child_text(node, names):
    for child in node:
        if child.tag.split("}")[-1] in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def parse_feed(raw):
    root = ET.fromstring(raw)
    out = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = clean_text(child_text(node, ("title",)))
        desc = clean_text(child_text(node, ("description", "summary", "content")))
        pub = child_text(node, ("pubDate", "published", "updated", "date"))
        link = ""
        for child in node:
            if child.tag.split("}")[-1] == "link":
                link = (child.get("href") or child.text or "").strip() or link
        if title and link:
            out.append({"title": title, "link": norm_link(link), "desc": desc[:600], "published": parse_date(pub)})
    return out


def load_sources(kind):
    rows = []
    path = ROOT / "sources.txt"
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        if len(p) < 3:
            continue
        name, url, k = p[0], p[1], p[2]
        lang = p[3] if len(p) > 3 else "en"
        tier = int(p[4]) if len(p) > 4 and p[4].isdigit() else 2
        group = p[5].lower() if len(p) > 5 else ""
        # فیلتر واژه: فید عمومی فقط آیتمی را می‌دهد که این واژه در تیتر یا
        # خلاصه‌اش باشد. برای فیدهای امنیتی عمومی لازم است، وگرنه خبر
        # آسیب‌پذیری یک فریم‌ورک دیگر هم وارد صف می‌شود.
        must = p[6].strip().lower() if len(p) > 6 else ""
        if k == kind:
            rows.append(
                {
                    "name": name,
                    "url": url,
                    "kind": k,
                    "lang": lang,
                    "tier": tier,
                    "group": group,
                    "must": must,
                }
            )
    return rows


# رقیب، خبر نیست. کانال برای مخاطبِ ماست، نه تابلوی اعلانات رقیب. هر آیتمی
# که نام یا دامنهٔ رقیب در آن باشد، همان لحظهٔ جمع‌آوری کنار می‌رود و به مدل
# هم نمی‌رسد. فهرست در blocked.txt است تا بدون دست زدن به کد عوض شود.
BLOCKED = ROOT / "blocked.txt"
_BLOCK_CACHE = {"mtime": None, "words": []}


def _norm_fa(text):
    """نیم‌فاصله و فاصله را یکی می‌کند تا «میهن‌وردپرس» و «میهن وردپرس» یکی شوند."""
    return str(text or "").replace("\u200c", " ").replace("\u200f", "").lower()


def load_blocked():
    path = BLOCKED
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if _BLOCK_CACHE["mtime"] == mtime:
        return _BLOCK_CACHE["words"]
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.append(_norm_fa(line).strip())
    _BLOCK_CACHE["mtime"] = mtime
    _BLOCK_CACHE["words"] = words
    return words


def blocked_hit(item, blocked=None):
    """اگر نام یا دامنهٔ رقیب جایی در آیتم باشد، همان واژه را برمی‌گرداند."""
    words = load_blocked() if blocked is None else blocked
    if not words:
        return ""
    hay = _norm_fa(
        " ".join(str(item.get(k) or "") for k in ("title", "desc", "link", "source"))
    )
    for w in words:
        if w and w in hay:
            return w
    return ""


# کالای فارسی، رقیب است. نه فقط میهن وردپرس؛ هر افزونه و قالب فارسی.
# کانال جای معرفی فروشگاه دیگر نیست. این سد اسم فروشگاه را نمی‌شناسد و
# به هیچ فهرستی وابسته نیست: یا منبع فارسی است یا دامنه ایرانی، به‌اضافهٔ
# حرف کالا. عمداً فقط خبر و ابزار را می‌گیرد؛ هشدار امنیتی مستثنی است.
IR_DOMAIN_RE = re.compile(r"\.ir(?::\d+)?(?:/|$)", re.I)
PRODUCT_WORD_RE = re.compile(
    r"(قالب|افزونه|پلاگین|\btheme\b|\bplugin\b|\btemplate\b)", re.I
)


def persian_product(item):
    """کالای فارسی: نه فقط یک فروشگاه، هر افزونه و قالب فارسی.

    یا منبع فارسی است، یا دامنه ایرانی، و حرف کالا در تیتر یا توضیح.
    هشدار امنیتی هرگز از این سد نمی‌گذرد چون اصلاً صدایش نمی‌زنیم؛
    خبر، ابزار و فرصت گرفته می‌شوند.
    """
    link = str(item.get("link") or "")
    fa = str(item.get("lang") or "") == "fa"
    if not fa and not IR_DOMAIN_RE.search(link):
        return False
    return bool(PRODUCT_WORD_RE.search(
        str(item.get("title") or "") + " " + str(item.get("desc") or "")
    ))


def fetch_json_items(url):
    data = json.loads(http_get(url, headers={"Accept": "application/json"}).decode("utf-8", "replace"))
    out = []
    if "hits" in data:
        for h in data["hits"]:
            link = h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID", "")))
            out.append(
                {
                    "title": clean_text(h.get("title") or h.get("story_title") or ""),
                    "link": norm_link(link),
                    "desc": clean_text(h.get("story_text") or "")[:600],
                    "published": parse_date(h.get("created_at")),
                }
            )
    elif "items" in data:
        for r in data["items"]:
            desc = clean_text(r.get("description") or "")
            out.append(
                {
                    "title": clean_text((r.get("full_name") or "") + (" — " + desc if desc else "")),
                    "link": r.get("html_url", ""),
                    "desc": desc,
                    "published": parse_date(r.get("pushed_at") or r.get("created_at")),
                }
            )
    return [x for x in out if x["title"] and x["link"]]


def fetch_perq_offers(limit=26):
    """فرصت‌های رایگان پرک: فقط برای همه، بدون نیاز به درخواست رسمی، زنده."""
    url = (
        "https://perq.sh/api/search?q=&audience=anyone&no_application=true"
        "&status=live&category=llm-api,voice,dev-tools,agents"
        "&sort=newest&limit={}".format(int(limit))
    )
    data = json.loads(http_get(url, headers={"Accept": "application/json"}).decode("utf-8", "replace"))
    out = []
    for r in data.get("results") or []:
        vendor = (r.get("vendor") or "").strip()
        value = clean_text(r.get("credit_value") or "")
        link = (r.get("source_url") or r.get("page_url") or "").strip()
        if not vendor or not link:
            continue
        bits = [value]
        if r.get("how_to_claim"):
            bits.append(clean_text(r["how_to_claim"]))
        if r.get("eligibility"):
            bits.append(clean_text(r["eligibility"]))
        title = "{} — {}".format(vendor, value[:120]) if value else vendor
        out.append(
            {
                "title": clean_text(title),
                "link": norm_link(link),
                "desc": clean_text(" · ".join([b for b in bits if b]))[:600],
                "published": parse_date(r.get("last_verified") or r.get("first_seen")),
            }
        )
    return out


def fetch_source(src, stats):
    key = src["url"]
    st = stats.setdefault(key, {"name": src["name"], "fails": 0, "last_count": 0, "last_ok": ""})
    last = None
    for _ in range(2):
        try:
            if src["url"].startswith("perq:"):
                items = fetch_perq_offers()[:40]
            elif "hn.algolia.com" in src["url"] or "api.github.com" in src["url"]:
                items = fetch_json_items(src["url"])[:40]
            else:
                items = parse_feed(http_get(src["url"]))[:40]
            st["last_count"] = len(items)
            st["last_ok"] = now().isoformat(timespec="seconds")
            st["fails"] = 0
            return items
        except Exception as exc:
            last = exc
            time.sleep(2)
    st["fails"] = int(st.get("fails", 0)) + 1
    log("  err  {}  {}".format(src["name"], last))
    return []


# ---------------------------------------------------------------- dedupe

def item_id(link):
    return hashlib.sha1(norm_link(link).encode("utf-8")).hexdigest()[:16]


def title_key(title):
    t = re.sub(r"[^\w\u0600-\u06FF ]+", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


def cluster_key(title):
    words = [w for w in re.findall(r"[\w\u0600-\u06FF]+", (title or "").lower()) if len(w) > 3]
    return hashlib.sha1(" ".join(sorted(words[:6])).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- dedupe

DIGITS_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

FA_STOP = {
    "این", "آن", "برای", "از", "که", "با", "را", "در", "به", "است", "شد", "شده",
    "می", "نمی", "های", "هایی", "یک", "دو", "تا", "هم", "یا", "و", "بر", "کرد",
    "کرده", "دارد", "دارند", "بود", "خود", "همه", "چه", "چی", "کدام", "اما",
    "بیشتر", "جدید", "تازه", "منتشر", "عرضه", "نسخه", "افزونه", "قالب", "خبر",
    "امنیتی", "آسیب", "پذیری", "کاربران", "سایت", "سایت‌ها", "روز", "هفته", "ماه",
    "سال", "دهد", "دهند", "کند", "کنند", "شود", "شوند", "نیز", "فقط", "الان",
}

STOPWORDS = {
    "release", "released", "releases", "update", "updated", "updates", "version",
    "security", "news", "new", "patch", "patches", "patched", "available", "fixes",
    "fixed", "critical", "high", "vulnerability", "vulnerabilities", "exploit",
    "report", "plugin", "theme", "site", "sites", "users", "user", "today", "week",
    "month", "year", "what", "with", "from", "this", "that", "have", "will", "your",
    "more", "than", "after", "before", "into", "launch", "launches", "launched",
    "now", "just", "first", "also", "over", "under", "about", "these", "those",
    "their", "there", "then", "when", "where", "which", "while", "would", "could",
    "should", "said", "says", "one", "two", "three", "most", "best", "top", "get",
    "gets", "adds", "added", "brings", "bring", "here", "why", "how", "you", "are",
    "show", "ask", "tell", "launch", "introducing", "announcing", "hn", "app",
    "free", "review", "guide", "tutorial", "tips", "vs", "using", "build", "built",
    "we", "our", "us", "my", "me", "they", "them", "its", "love", "feedback",
    "now", "here", "new", "just", "still", "very", "much", "many", "some", "any",
}

# نام‌هایی که در این فید همه‌جا هستند و اشتراکشان چیزی ثابت نمی‌کند.
GENERIC_ENTS = {
    "mcp", "claude", "code", "ai", "api", "server", "model", "agent", "agents",
    "app", "apps", "tool", "tools", "plugin", "gpt", "llm", "openai", "google",
    "meta", "apple", "microsoft", "browser", "agentic", "cursor", "codex",
    "anthropic", "apache", "github", "hugging", "python", "node", "web",
    "dashboard", "sdk", "runtime", "inference", "llama", "mistral", "open",
    "windows", "linux", "macos",
}

VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
# شمارهٔ نسخهٔ مجوز («Apache 2.0»، «GPL 3.0») نسخهٔ محصول نیست و نباید
# دو محصول کاملاً بی‌ربط را هم‌نسخه حساب کند.
LICENSE_VER_RE = re.compile(
    r"\b(?:apache|gpl|agpl|lgpl|bsd|mit|mpl|isc|cc|creative\s+commons|licen[cs]e)"
    r"\s*(?:v|ver|version)?\s*\d+(?:\.\d+)*",
    re.IGNORECASE,
)
PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[\-\.][A-Za-z0-9]+)*|[A-Z]{2,})\b")
# فقط وقتی پسوند را می‌بریم که واقعاً نام سایت باشد: کوتاه و بعد از خط عمودی یا خط تیره.
SITE_SUFFIX_RE = re.compile(r"\s+(?:\|\s*\S.{1,28}|-\s+[A-Z][\w .&'’]{1,24})$")
# پیشوندهای تیترهای هکرنیوز که در همه تکرار می‌شوند و مقایسه را خراب می‌کنند.
HN_PREFIX_RE = re.compile(r"^(?:show|ask|launch|tell)\s+hn\s*[:：]\s*", re.IGNORECASE)


def tokens(text):
    return {w for w in re.findall(r"[\w\u0600-\u06FF]+", (text or "").lower()) if len(w) > 3}


def norm_text(text):
    """ارقام را یکی می‌کند و پسوند نام سایت را از انتهای تیتر برمی‌دارد."""
    t = str(text or "").translate(DIGITS_TO_LATIN)
    t = HN_PREFIX_RE.sub("", t.strip())
    t = SITE_SUFFIX_RE.sub("", t)
    return t.strip()


def story_sig(title):
    """امضای داستان: واژه‌های معنادار، نام‌های خاص لاتین، شمارهٔ نسخه."""
    raw = norm_text(title)
    low = raw.lower()
    words = {
        w
        for w in re.findall(r"[\w\u0600-\u06FF]+", low)
        if len(w) > 2 and w not in STOPWORDS and w not in FA_STOP
    }
    ents = set()
    for m in PROPER_RE.findall(raw):
        e = m.lower().strip(".-")
        if len(e) > 1 and e not in STOPWORDS:
            ents.add(e)
    vers = set(VERSION_RE.findall(LICENSE_VER_RE.sub(" ", low)))
    return words, ents, vers


ADVISORY_REFS = ("cisa", "nvd", "kev")
CVE_RE = re.compile(r"cve-\d{4}-\d{4,}", re.I)


def _advisory_refs(title):
    """مرجع‌هایی که دو تیتر را به یک اطلاعیه وصل می‌کند."""
    low = str(title or "").lower()
    refs = {w for w in ADVISORY_REFS if re.search(r"\b" + w + r"\b", low)}
    refs |= {m.group(0) for m in CVE_RE.finditer(low)}
    return refs


def same_story(a, b):
    """دو آیتم یک داستان‌اند؟ واژه + نام خاص + شمارهٔ نسخه.

    ترتیب قاعده‌ها مهم است: اول نسخهٔ متفاوت، بعد تشابه واژگانی، بعد نسخهٔ یکسان،
    بعد نام‌های خاص متمایز. هر قاعده‌ای که زودتر جواب بدهد تصمیم را می‌بندد.
    """
    wa, ea, va = story_sig(a.get("title", ""))
    wb, eb, vb = story_sig(b.get("title", ""))
    if not wa or not wb:
        return False

    jac = len(wa & wb) / len(wa | wb)
    same_ver = bool(va and vb and va == vb)
    diff_ver = bool(va and vb and va != vb)

    # ۱. نسخهٔ متفاوت با نام‌های خاص هم‌خانواده یعنی دو رویداد جدا.
    if diff_ver and (ea == eb or (ea & eb and len(ea | eb) <= 3)):
        return False

    # ۲. نسخهٔ یکسان، قوی‌ترین شاهد است و بر کوتاهی تیتر مقدم می‌شود.
    if same_ver:
        if ea or eb:
            shared = ea & eb
            # اشتراک فقط در نام‌های همه‌جاحاضر (apache، openai، llm…) شاهد نیست.
            if ea == eb or (len(shared) >= 2 and (shared - GENERIC_ENTS)):
                return True
        elif wa & wb:
            return True

    # ۳. تیتر خیلی کوتاه فقط با تشابه تقریباً کامل یکی حساب می‌شود.
    short = min(len(wa), len(wb))
    if short <= 1 and jac < 0.8:
        return False
    if short == 2 and jac < 0.6:
        return False

    # ۴. تشابه واژگانی بالا.
    if jac >= 0.5:
        return True

    # ۵. دو نام خاص متمایز مشترک، به‌علاوهٔ تشابه معقول.
    if len((ea & eb) - GENERIC_ENTS) >= 2 and jac >= 0.45:
        return True

    # ۶. دو نام خاص مشترک و چهار واژهٔ محتوایی مشترک. یک داستان که دو
    # نشریه بازنویسی می‌کنند، واژه‌هایش یکی نیست ولی نام‌ها و موضوعش
    # یکی است. شکایت نیویورک‌تایمز از بریو دو بار پست شد چون تشابه
    # واژگانی‌اش چهل و یک درصد بود و از آستانهٔ قبلی جا می‌ماند.
    if len((ea & eb) - GENERIC_ENTS) >= 2 and len(wa & wb) >= 4:
        return True

    # ۷. مرجع امنیتی مشترک. «سیسا» یا یک شمارهٔ CVE در هر دو تیتر یعنی
    # همان اطلاعیه و دو نشریه فقط بازنویسی‌اش کرده‌اند. سیزدهم آبان یک
    # خبر CISA دربارهٔ وردپرس دو بار پست شد، سه ثانیه فاصله، چون
    # واژه‌های دو تیتر یکی نبود.
    if (_advisory_refs(a.get("title", "")) & _advisory_refs(b.get("title", ""))) and len(wa & wb) >= 3:
        return True

    return False


def dedupe_stories(items):
    """درون یک دستهٔ تازه‌جمع‌آوری‌شده، تکراری‌ها را یکی می‌کند."""
    kept = []
    for it in sorted(items, key=lambda x: (x.get("tier", 3), true_age(x))):
        match = next((k for k in kept if same_story(it, k)), None)
        if match is None:
            kept.append(it)
        else:
            match["sources_count"] = int(match.get("sources_count", 1)) + 1
    return kept


BAD_SCRIPT = re.compile(r"[\u0400-\u04FF\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF\u0600-\u06FF]")
CYRILLIC = re.compile(r"[\u0400-\u04FF\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")


def bad_lang(text):
    return bool(CYRILLIC.search(text or ""))


STORY_WINDOW = {"news": 3, "security": 7, "tools": 14, "offers": 14, "showcase": 30}
STORY_FAMILY = ("news", "tools", "offers")


def load_stories():
    """حافظهٔ داستان‌ها. پنجرهٔ نگه‌داری به دسته بستگی دارد، نه یک عدد برای همه."""
    d = read_json(STORIES, {"items": []})
    out = []
    for x in d.get("items", []):
        kind = x.get("kind") or "news"
        days = STORY_WINDOW.get(kind, 7)
        try:
            ts = datetime.fromisoformat(str(x.get("t", "")))
        except Exception:
            continue
        if (now() - ts).days > days:
            continue
        out.append(x)
    d["items"] = out
    return d


CVE_RE = re.compile(r"CVE[-\u2011\u2013 ]?(\d{4})[-\u2011\u2013 ]?(\d{4,7})", re.I)


def cve_ids(text):
    """شناسهٔ CVE را یک‌شکل می‌کند.

    یک CVE می‌تواند ده مقاله داشته باشد و هر مقاله تیتر خودش. ولی شناسه
    یکی است. یک بار یک CVE بیست و دوم اکتبر اعلام شد، سی‌ام اکتبر یک
    مقاله‌اش پست شد و دوم نوامبر مقالهٔ دیگرش هم پست شد، چون دو تیتر
    هیچ واژهٔ مشترکی نداشتند جز نام کتابخانه.
    """
    return {"CVE-{}-{}".format(y, n) for y, n in CVE_RE.findall(text or "")}


def is_recent_story(store, item, ignore_id=""):
    """آیا همین داستان قبلاً پست شده؟ بین دسته‌های هم‌خانواده هم چک می‌شود.

    ignore_id یعنی رکورد خودِ همین پست را نادیده بگیر. پست نگه‌داشته‌شده
    موقع نگه‌داری در انبار ثبت می‌شود، پس بدون این استثنا خودش را تکراری
    می‌بیند و هرگز منتشر نمی‌شود.
    """
    kind = item.get("kind") or "news"
    probe = {"title": item.get("title", "")}
    mine = cve_ids(item.get("title", "") + " " + (item.get("desc") or ""))
    current = now()
    for s in store.get("items", []):
        if ignore_id and str(s.get("id") or "") == str(ignore_id):
            continue
        sk = s.get("kind") or "news"
        if sk == kind:
            days = STORY_WINDOW.get(kind, 7)
        elif sk in STORY_FAMILY and kind in STORY_FAMILY:
            days = 3
        else:
            continue
        try:
            ts = datetime.fromisoformat(str(s.get("t", "")))
        except Exception:
            continue
        if (current - ts).days > days:
            continue
        # شناسهٔ CVE از هر تطبیق واژگانی قوی‌تر است: همان حفره است،
        # هر چقدر هم تیتر عوض شده باشد.
        if mine and mine & set(s.get("cves") or []):
            return True
        if same_story(probe, s):
            return True
    return False


def add_story(store, item):
    """داستان را ثبت می‌کند. با همان شناسه دو بار ثبت نمی‌شود.

    پست نگه‌داشته‌شده موقع نگه‌داری ثبت می‌شود و بعد موقع انتشار دوباره
    از همین تابع می‌گذرد؛ بدون این سد، هر پست دو رکورد می‌ساخت.
    """
    iid = str(item.get("id") or "")
    if iid and any(str(x.get("id") or "") == iid for x in store.get("items", [])):
        return
    words, ents, vers = story_sig(item.get("title", ""))
    store["items"].append(
        {
            "t": now().isoformat(timespec="seconds"),
            "id": iid,
            "title": item.get("title", ""),
            "kind": item.get("kind") or "news",
            "link": item.get("link", ""),
            "ents": sorted(ents)[:12],
            "vers": sorted(vers)[:6],
            "cves": sorted(cve_ids(item.get("title", "") + " " + (item.get("desc") or "")))[:4],
        }
    )
    store["items"] = store["items"][-400:]


def reconcile_seen(seen):
    """حافظه را از آرشیو بازسازی می‌کند.

    آرشیو تنها جایی است که سابقهٔ ارسال را نگه می‌دارد. اگر اجرایی
    استیتش را ذخیره نکرده باشد، این تابع جلوی پست تکراری را می‌گیرد.
    """
    if not ARCHIVE.exists():
        return 0
    try:
        lines = ARCHIVE.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]
    except Exception:
        return 0
    added = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        iid = row.get("id")
        if iid and iid not in seen["ids"]:
            seen["ids"][iid] = str(row.get("at") or today())[:10]
            added += 1
    if added:
        log("reconcile: {} id restored from archive".format(added))
    return added


# ---------------------------------------------------------------- enrich

def plugin_slug(url):
    m = re.search(r"wordpress\.org/plugin/([a-z0-9\-]+)", url or "")
    return m.group(1) if m else ""


def plugin_info(slug):
    if not slug:
        return {}
    try:
        data = json.loads(
            http_get(
                "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information&request[slug]=" + slug,
                timeout=15,
                headers={"Accept": "application/json"},
            ).decode("utf-8", "replace")
        )
        return {
            "name": clean_text(data.get("name", "")),
            "installs": data.get("active_installs", 0),
            "rating": data.get("rating", 0),
            "version": data.get("version", ""),
            "updated": (data.get("last_updated") or "")[:10],
            "requires": data.get("requires", ""),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------- plugin watch

# فیدهای رسمی المنتور و ووکامرس یا مرده‌اند یا دیر می‌رسند. یک بار نسخهٔ
# ۴.۳.۶ المنتور منتشر شد و کانال هیچ خبری نداشت، چون منبعی نداشتیم که
# آن را ببیند. مخزن وردپرس منبع دست اول است: نسخه، تاریخ و فهرست تغییرات
# را همان لحظهٔ انتشار دارد. اینجا فقط نسخه عوض شده را می‌گیریم؛ هر
# اجرا یک درخواست کوچک برای هر افزونه.
PLUGINS_FILE = ROOT / "plugins.txt"
PLUGIN_API = (
    "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information"
    "&request[slug]={slug}&request[fields][sections]=changelog"
)
# یک تغییر امنیتی، هشدار امنیتی است نه خبر عادی. همین تفاوت، دستهٔ پست را
# تعیین می‌کند تا آپدیت امنیتی از صف خبر جا نماند.
# دو چیز مختلف که در فهرست تغییرات شبیه هم‌اند و یکی نیستند.
# «آسیب‌پذیری» یعنی حفره‌ای باز بوده و باید امروز ببندی: هشدار امنیتی.
# «تقویت امنیت» یعنی کد سخت‌تر شده و حفره‌ای گزارش نشده: آپدیت.
# یک بار آپدیت المنتور که فقط security enforcement بود، هشدار امنیتی شد.
VULN_RE = re.compile(
    r"(vulnerab\w*|CVE-\d{4}-\d+|exploit\w*|\bXSS\b|\bCSRF\b|\bSQLi\b|SQL injection|"
    r"\bRCE\b|\bSSRF\b|\bLFI\b|\bRFI\b|backdoor|privilege escalation|"
    r"unauthori[sz]ed (?:access|upload|delete)|(?:auth\w*|authorization) bypass|"
    r"arbitrary file|remote code|directory traversal|"
    r"آسیب[\s\u200c]?پذیری|حفرهٔ? امنیتی|بهره[\s\u200c]?برداری|اختیار غیرمجاز)",
    re.I,
)
HARDEN_RE = re.compile(
    r"(security enforcement|security hardening|harden\w*|sanitiz\w*|escaping|"
    r"security improvement|security enhancement|security fix|"
    r"تقویت امنیت|سخت[\s\u200c]?تر|اصلاح امنیتی|امنیت کد)",
    re.I,
)


def load_plugin_watch():
    """فهرست افزونه‌هایی که نسخه‌شان رصد می‌شود.

    هر خط: نامک | نام نمایشی | کمینهٔ نصب. فایل جدا است تا بدون دست زدن
    به کد عوض شود.
    """
    rows = []
    path = PLUGINS_FILE
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        slug = p[0].lower()
        if not re.fullmatch(r"[a-z0-9\-]{2,60}", slug):
            continue
        rows.append(
            {
                "slug": slug,
                "name": p[1] if len(p) > 1 and p[1] else "",
                "min_installs": int(p[2]) if len(p) > 2 and p[2].isdigit() else 0,
            }
        )
    return rows


def plugin_feed(slug):
    """اطلاعات افزونه به‌همراه فهرست تغییرات، مستقیم از مخزن وردپرس."""
    if not slug:
        return {}
    try:
        data = json.loads(
            http_get(
                PLUGIN_API.format(slug=slug),
                timeout=15,
                headers={"Accept": "application/json"},
            ).decode("utf-8", "replace")
        )
    except Exception as exc:
        log("  plugin feed failed:", slug, str(exc)[:60])
        return {}
    if not isinstance(data, dict) or data.get("error"):
        return {}
    sections = data.get("sections") or {}
    return {
        "name": clean_text(data.get("name", "")),
        "version": str(data.get("version") or "").strip(),
        "installs": int(data.get("active_installs") or 0),
        "updated": (data.get("last_updated") or "")[:10],
        "changelog": sections.get("changelog") or "",
    }


def changelog_block(changelog, version):
    """بخشِ همین نسخه از فهرست تغییرات را جدا می‌کند.

    ساختار مخزن وردپرس ساده است: عنوان نسخه در یک تیتر، بعد فهرست
    تغییرها. اگر نسخهٔ فعلی پیدا نشد، بالاترین بلوک را برمی‌داریم؛ آن هم
    نسخهٔ تازه است.
    """
    text = str(changelog or "")
    heads = [
        (m.start(), m.end(), clean_text(m.group(1)))
        for m in re.finditer(r"<h4[^>]*>(.*?)</h4>", text, re.S | re.I)
    ]
    if not heads:
        return "", []
    blocks = []
    for i, (start, end, head) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        m = re.search(r"[\d۰-۹]+(?:\.[\d۰-۹]+)+", head)
        blocks.append((head, m.group(0) if m else "", text[end:stop]))
    pick = None
    for head, ver, body in blocks:
        if ver and ver == str(version).strip():
            pick = (head, body)
            break
    if pick is None:
        pick = (blocks[0][0], blocks[0][2])
    lines = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", pick[1], re.S | re.I):
        t = clean_text(li)
        if t and t not in lines:
            lines.append(t)
    return pick[0], lines[:9]


def plugin_watch(kind, seen=None, commit=True):
    """نسخهٔ تازهٔ افزونه‌های پرنصب را به آیتم خبری تبدیل می‌کند.

    فقط وقتی نسخه عوض شده باشد آیتم ساخته می‌شود. اولین بار نسخهٔ فعلی
    ثبت می‌شود و پستی نمی‌رود؛ وگرنه اولین اجرا برای هر افزونه یک پست
    می‌ساخت که خبر تازه‌ای هم نداشت.
    """
    watch = load_plugin_watch()
    if not watch:
        return []
    state = read_json(PLUGIN_STATE, {"v": 1, "seen": {}})
    seen_map = dict(state.get("seen") or {})
    known = (seen or {}).get("ids") or {}
    rows, touched = [], {}
    for cfg in watch:
        info = plugin_feed(cfg["slug"])
        if not info or not info.get("version"):
            continue
        if cfg["min_installs"] and info.get("installs", 0) < cfg["min_installs"]:
            continue
        version = info["version"]
        prev = str((seen_map.get(cfg["slug"]) or {}).get("version") or "")
        if not prev:
            touched[cfg["slug"]] = {"version": version, "at": now().isoformat(timespec="seconds")}
            log("  plugin baseline:", cfg["slug"], version)
            continue
        if prev == version:
            continue
        head, lines = changelog_block(info.get("changelog"), version)
        if not lines:
            # فهرست تغییرات خالی است یا هنوز منتشر نشده. یک بار دیگر
            # امتحان می‌کنیم؛ بعد از سه تلاش نسخه را ثبت می‌کنیم تا هر
            # اجرا بی‌خود درخواست نفرستیم.
            tries = int((seen_map.get(cfg["slug"]) or {}).get("tries") or 0) + 1
            if tries >= 3:
                touched[cfg["slug"]] = {"version": version, "at": now().isoformat(timespec="seconds")}
            else:
                touched[cfg["slug"]] = {
                    "version": prev,
                    "at": (seen_map.get(cfg["slug"]) or {}).get("at") or "",
                    "tries": tries,
                }
            log("  plugin changelog empty:", cfg["slug"], version, "try", tries)
            continue
        joined = " ".join(lines)
        vuln = bool(VULN_RE.search(joined))
        hardening = bool(HARDEN_RE.search(joined))
        link = "https://wordpress.org/plugins/{}/".format(cfg["slug"])
        iid = item_id(link)
        if iid in known:
            touched[cfg["slug"]] = {"version": version, "at": now().isoformat(timespec="seconds")}
            continue
        name = cfg["name"] or info.get("name") or cfg["slug"]
        rows.append(
            {
                "id": iid,
                "title": "{} {}".format(name, version),
                "link": link,
                "desc": "نسخهٔ {} از {} منتشر شد. تغییرات این نسخه: {}".format(
                    version, name, " | ".join(lines)
                ),
                "source": "آپدیت افزونه",
                "lang": "en",
                "tier": 1,
                "group": "wp",
                "kind": "news",
                "age_hours": 0.0,
                "published": now().isoformat(timespec="seconds"),
                "added": now().isoformat(timespec="seconds"),
                "plugin": {
                    "name": name,
                    "version": version,
                    "prev": prev,
                    "installs": info.get("installs", 0),
                    "updated": info.get("updated", ""),
                },
                "plugin_update": {
                    "name": name,
                    "slug": cfg["slug"],
                    "version": version,
                    "prev": prev,
                    "date": head,
                    "changes": lines,
                    "vuln": vuln,
                    "hardening": hardening,
                },
            }
        )
        touched[cfg["slug"]] = {"version": version, "at": now().isoformat(timespec="seconds")}
        log("  plugin update:", cfg["slug"], prev, "->", version, "lines", len(lines))
    if touched and commit:
        merged = dict(seen_map)
        merged.update(touched)
        write_json(PLUGIN_STATE, {"v": 1, "seen": merged})
    return rows


def plugins_report():
    """وضعیت رصد نسخه. چیزی نمی‌نویسد؛ فقط می‌گوید چه چیزی ثبت شده."""
    watch = load_plugin_watch()
    state = read_json(PLUGIN_STATE, {"v": 1, "seen": {}})
    seen_map = state.get("seen") or {}
    print("watching {} plugins".format(len(watch)))
    for cfg in watch:
        info = plugin_feed(cfg["slug"])
        if not info:
            print("  {:<26} FAILED".format(cfg["slug"]))
            continue
        prev = str((seen_map.get(cfg["slug"]) or {}).get("version") or "")
        if not prev:
            mark = "NO BASE"
        elif prev == info["version"]:
            mark = "SAME"
        else:
            mark = "NEW"
        print(
            "  {:<26} now {:<10} base {:<10} {:<8} installs {}".format(
                cfg["slug"], info["version"], prev or "-", mark, info["installs"]
            )
        )
        if mark == "NEW":
            head, lines = changelog_block(info["changelog"], info["version"])
            print("      head: " + (head or "-"))
            for ln in lines:
                print("      - " + ln[:150])
    return 0


# ---------------------------------------------------------------- score

def true_age(item):
    """سن واقعی از زمان انتشار. فیلد age_hours فقط لحظهٔ جمع‌آوری را می‌داند."""
    pub = item.get("published") or ""
    if pub:
        try:
            return max(0.0, (now() - datetime.fromisoformat(str(pub))).total_seconds() / 3600.0)
        except Exception:
            pass
    return float(item.get("age_hours", 99) or 99)


def queue_age(item):
    """سن برای سد صف. اگر تاریخ انتشار نبود، تاریخ ورود به صف.

    فیدهایی که تاریخ نمی‌دهند نباید کل آیتم‌هایشان با سن نودونه حساب
    شود؛ آن‌ها تازه وارد شده‌اند، نه کهنه.
    """
    if item.get("published"):
        return true_age(item)
    try:
        added = datetime.fromisoformat(str(item.get("added")))
        return max(0.0, (now() - added).total_seconds() / 3600.0)
    except Exception:
        return true_age(item)


def max_age_for(kind, s=None):
    """سقف سن ورود به صف. همان عددی که مرحلهٔ مدل هم می‌سنجد.

    خبر کهنه نباید وارد صف شود. قبلاً سد سنی فقط سر وقت مدل اعمال
    می‌شد: صف پر می‌شد از خبر یک‌هفته‌ای، مدل ردش می‌کرد، و صف فقط
    شلوغ می‌شد. یک بار نسخهٔ ۷.۱.۴ وردپرس شش روز بعد از دو پستی که
    کانال دربارهٔ همان نسخه داده بود دوباره در صف نشست.
    """
    s = s if isinstance(s, dict) else settings()
    if kind == "security":
        return float(s.get("security_max_age_h", 24) or 24)
    if kind == "showcase":
        # نمایشگاه الهام طراحی است، نه خبر. عمرش با سقف انبارش هم‌تراز
        # است: شش آیتم، روزی یکی، یعنی شش روز.
        return float(s.get("showcase_max_age_h", 168) or 168)
    return 96.0 if kind in ("tools", "offers") else 48.0


def code_score(item, source_count):
    """امتیاز بخش کد: تازگی، اعتبار منبع، تأیید چندمنبعی.

    حداکثر پنجاه‌وپنج. دو بُعد دیگر، یعنی اهمیت رویداد و قابل استفاده بودن،
    از خود مدل می‌آید و در final_score اضافه می‌شود.
    """
    age = true_age(item)
    fresh = max(0.0, 20.0 * (1.0 - min(age, 48.0) / 48.0))
    tier = {1: 15.0, 2: 11.0, 3: 7.0}.get(int(item.get("tier", 3) or 3), 9.0)
    sc = int(source_count or 1)
    multi = 6.0 if sc <= 1 else 12.0 if sc == 2 else 20.0
    return round(fresh + tier + multi, 1)


DESIGN_WORDS = (
    "wordpress", "elementor", "woocommerce", "figma", "web design", "website",
    "landing page", "page builder", "css", "theme", "plugin", "hosting", "seo",
    "site builder", "webflow", "framer", "html", "template", "ui kit",
    "cms", "ecommerce", "shop", "storefront",
)
# اولویت کانال، به ترتیب: وردپرس، بعد ابزاری که کاری برای سایت می‌کند،
# بعد ابزار محتوا. وردپرس بالاتر می‌نشیند چون مخاطب با آن سایت می‌سازد.
WP_WORDS = (
    "wordpress", "woocommerce", "elementor", "divi", "gutenberg",
    "وردپرس", "ووکامرس", "المنتور", "افزونه", "پلاگین", "قالب",
)
SITE_WORDS = (
    "website", "web design", "landing page", "page builder", "seo", "hosting",
    "site builder", "webflow", "framer", "css", "figma", "ui kit", "template",
    "cms", "ecommerce", "storefront", "analytics", "contact form", "speed",
    "schema",
)
CONTENT_WORDS = (
    "writing", "copywriting", "blog", "article", "content", "text", "translate",
    "newsletter", "email", "summar", "editing", "editor", "seo content",
)
VIDEO_WORDS = (
    "video", "animation", "avatar", "voice", "speech", "tts", "music", "audio",
    "image gen", "text to image", "text-to-image", "film", "movie", "render",
)


def topic_bonus(item, entry):
    """اولویت موضوعی: وردپرس، بعد ابزار سایت، بعد محتوا، آخر ویدیو.

    برای خبر اعمال نمی‌شود، چون خبر ارزش خودش را دارد.
    """
    if item.get("kind") not in ("tools", "offers"):
        return 0.0
    blob = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("desc") or ""),
            str(entry.get("title") or ""),
            str(entry.get("summary") or ""),
            str(entry.get("details") or ""),
        ]
    ).lower()
    if any(w in blob for w in WP_WORDS):
        return 8.0
    if any(w in blob for w in SITE_WORDS):
        return 6.0
    if any(w in blob for w in CONTENT_WORDS):
        return 3.0
    if any(w in blob for w in VIDEO_WORDS):
        return 0.0
    return 2.0


def final_score(item, entry):
    """امتیاز نهایی از صد: کد به‌علاوه نظر مدل به‌علاوه اولویت موضوعی."""
    imp = max(0.0, min(num(entry.get("imp")), 10.0)) * 2.5
    use = max(0.0, min(num(entry.get("use")), 10.0)) * 2.0
    return round(code_score(item, item.get("sources_count", 1)) + imp + use + topic_bonus(item, entry), 1)


BANNED_PHRASES = ("متن ورودی", "جزئیات بیشتری ندارد", "اشاره دارد", "در دنیای امروز", "لازم به ذکر است")

# «یک مهندس می‌گوید...» یعنی هیچ. مخاطب نمی‌داند طرف کیست و چرا حرفش مهم
# است. گفتنش به مدل کافی نبود؛ پس سد قطعی گذاشتیم. عمداً فقط ابتدای تیتر
# گرفته می‌شود: اگر اسم در جملهٔ اول آمده باشد، تیتر با «یک شرکت» شروع
# نمی‌شود و پست سالم رد نمی‌شود.
VAGUE_SUBJECT_RE = re.compile(
    r"^\s*(?:به\s+گفتهٔ\s+|به\s+نقل\s+از\s+|طبق\s+)?(?:یک|یه)\s+"
    r"(?:مهندس|محقق|پژوهشگر|کارشناس|متخصص|برنامه‌نویس|توسعه‌دهنده|"
    r"نویسنده|وبلاگ‌نویس|بنیان‌گذار|استاد|دانشمند|مدیر|شرکت|استارتاپ)"
)

# مدل گاهی خودش برچسب می‌گذارد؛ پست نباید فرم پرکردنی باشد.
FIELD_LABELS = (
    "تیتر", "عنوان", "خلاصه", "جان مطلب", "جزئیات", "توضیح", "شرح",
    "چه چیزی می‌دهد", "چی گیرت می‌آید", "چرا مهم است", "مهم‌ترین نکته",
    "نام محصول", "نام ابزار", "نام سرویس", "دسته", "برچسب", "مزیت",
    "محدودیت", "شروع", "نحوهٔ شروع", "قیمت", "هزینه", "نسخه", "منبع",
)
LABEL_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(x) for x in FIELD_LABELS) + r")\s*[:：]\s*",
    re.MULTILINE,
)


def strip_labels(text):
    """برچسب فیلد را برمی‌دارد و خطی که فقط یک نام لاتین مانده باشد را حذف می‌کند."""
    out = []
    for line in str(text or "").splitlines():
        m = LABEL_RE.match(line)
        if not m:
            out.append(line)
            continue
        rest = line[m.end() :].strip()
        if not rest or not re.search(r"[\u0600-\u06FF]", rest):
            continue
        out.append(rest)
    return "\n".join(out).strip()


AR_MARKS = re.compile(r"[\u064B-\u0652\u0670\u0653-\u0655]")

# ترجمهٔ واژه‌به‌واژهٔ اصطلاح‌های انگلیسی. «کدنویسی با حس و حال» ترجمهٔ
# لفظ‌به‌لفظ vibe coding است و در فارسی هیچ معنایی ندارد. خودِ اصطلاح
# نام است و ترجمه نمی‌شود؛ همان لاتین می‌ماند و اگر لازم بود یک نیم‌جمله
# معنی‌اش را می‌گوید.
FIX_PHRASES = {
    "کدنویسی با حس و حال": "vibe coding",
    "کد نویسی با حس و حال": "vibe coding",
    "کدنویسی حسی": "vibe coding",
    "حس و حال کدنویسی": "vibe coding",
    "وایب کدینگ": "vibe coding",
    # اصطلاح‌های فنی هم نام‌اند. «منبع باز» ترجمهٔ لفظ‌به‌لفظ است و در
    # فارسی به گوش آشنا نیست؛ مخاطب همین اصطلاح را با همان شکل لاتین
    # می‌شناسد و با آن جست‌وجو می‌کند.
    "منبع باز": "open source",
    "متن‌باز": "open source",
    "وزن باز": "open weights",
}


def fix_phrases(text):
    out = str(text or "")
    for bad, good in FIX_PHRASES.items():
        out = out.replace(bad, good)
    return out


def clean_details(text):
    """جزئیات: برچسب حذف، خط تکراری یکی می‌شود، خط بی‌فارسی دور ریخته می‌شود."""
    keep = []
    keys = []
    for line in strip_labels(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if not re.search(r"[\u0600-\u06FF]", line):
            continue
        # خطی که فقط شمارهٔ نسخه است، نکته نیست.
        if version_only_line(line):
            continue
        # خطی که فقط یک عدد و یک اسم لاتین است، نکته نیست.
        if meta_fragment(line):
            continue
        # «نسخه ۷.۱.۴» و «نسخهٔ ۷.۱.۴» دو خط جدا نیستند.
        key = AR_MARKS.sub("", line)
        key = re.sub(r"[\s،.,:؛;!؟?\-–—_]+", " ", key).strip().lower()
        if not key:
            continue
        if any(key == k or key in k or k in key for k in keys):
            continue
        keys.append(key)
        keep.append(line)
    # مدل بیشتر وقت‌ها یک پاراگراف بلند می‌نویسد، در حالی که قالب چند خط
    # کوتاه می‌خواهد. جمله‌ها را جدا می‌کنیم تا بلوک نقل‌قول خوانا شود.
    # نقطه فقط وقتی مرز جمله است که بعدش فاصله بیاید، پس «۱.۵» نمی‌شکند.
    if len(keep) == 1 and len(keep[0]) > 140:
        parts = [p.strip() for p in re.split(r"(?<=[.!؟])\s+", keep[0]) if p.strip()]
        if len(parts) >= 2:
            keep = parts[:MAX_DETAIL_LINES]
    return "\n".join(keep)


def valid_name(raw, item):
    """نام رسمی محصول، فقط اگر در متن منبع هم باشد.

    مدل اگر نامی بسازد که در منبع نیست، همان لحظه دور ریخته می‌شود.
    نامی که در تیتر یا توضیح منبع نباشد، حاصل خیال است.
    """
    name = str(raw or "").strip().strip("«»\"'")
    if not (2 <= len(name) <= 40):
        return ""
    if not re.search(r"[A-Za-z]", name):
        return ""
    # واژهٔ عمومی نام محصول نیست، حتی اگر در منبع باشد.
    words = [w for w in re.split(r"[\s\-_.]+", name.lower()) if w]
    if words and all(w in NAME_STOP for w in words):
        return ""
    hay = " ".join([str(item.get("title", "")), str(item.get("desc", ""))]).lower()
    if name.lower() not in hay:
        return ""
    return name


# واژه‌های رایجی که اسم محصول نیستند، هرچند با حرف بزرگ شروع می‌شوند.
NAME_STOP = {
    "the", "a", "an", "show", "ask", "launch", "tell", "how", "to", "new", "free",
    "ai", "api", "hn", "guide", "tutorial", "introducing", "announcing", "top",
    "best", "why", "what", "week", "day", "release", "update", "vs",
    # واژه‌های عمومی که در متن منبع زیاد می‌آیند ولی نام محصول نیستند.
    # همین‌ها بودند که یک بار «Feature» را به‌عنوان نام محصول چاپ کردند.
    "feature", "features", "presence", "notification", "notifications",
    "security", "core", "version", "plugin", "theme", "block", "editor",
    "agent", "agents", "model", "models", "proposal", "design", "testing",
    "beta", "alpha", "preview", "weekly", "monthly", "summary", "meeting",
    "updates", "report", "notes", "agenda", "draft", "roadmap", "initiative",
    # صفت و اسم‌های عمومی که با حرف بزرگ شروع می‌شوند و نام محصول نیستند.
    # «Chinese AI models are politically complex» نام محصولش «Chinese» شد
    # و در تیتر پست فارسی چاپ شد.
    "chinese", "american", "european", "japanese", "korean", "russian",
    "indian", "german", "french", "british", "italian", "spanish",
    "canadian", "israeli", "iranian", "global", "world", "study",
    "researchers", "scientists", "company", "startup", "government",
    "users", "people", "here", "there", "this", "that", "these", "those",
    "every", "most", "more", "less", "first", "last", "next", "only",
    "just", "now", "then", "still", "even", "also", "but", "and",
}
NAME_RE = re.compile(r"[A-Z][A-Za-z0-9][A-Za-z0-9.\-]{1,29}")


def auto_name(item):
    """نام محصول را از تیتر منبع برمی‌دارد.

    اگر مدل نامی نداد، پست نباید بی‌نام بماند. تیتر منبع معمولاً نام
    محصول را دارد و آن قابل اعتمادتر از ساختن نام توسط مدل است.
    """
    for m in NAME_RE.finditer(str(item.get("title", ""))):
        word = m.group(0)
        if word.lower() in NAME_STOP:
            continue
        return word
    return ""


def rel_age(hours):
    """سن را به زبان آدم می‌گوید: «۱۸ ساعت پیش»، «یک روز پیش»."""
    try:
        h = float(hours)
    except Exception:
        return ""
    if h < 1:
        return "کمتر از یک ساعت پیش"
    if h < 24:
        return "{} ساعت پیش".format(fa_num(int(round(h))))
    d = int(round(h / 24.0))
    if d <= 1:
        return "یک روز پیش"
    return "{} روز پیش".format(fa_num(d))


def quality_ok(title, summary):
    """دروازهٔ کیفیت. پست بدتر از پست نبودن است."""
    s = (summary or "").strip()
    if len(s) < 80:
        return False, "خلاصه کوتاه"
    for phrase in BANNED_PHRASES:
        if phrase in s or phrase in (title or ""):
            return False, "عبارت ممنوع"
    if VAGUE_SUBJECT_RE.search(title or ""):
        return False, "سوژهٔ مبهم"
    # تیتر کوتاه (دو واژه) طبیعتاً در خلاصه تکرار می‌شود و نباید رد شود.
    # معیار «بازگویی» این است که خلاصه چیز تازه‌ای اضافه نکند، نه این که
    # واژه‌های تیتر را دوباره به کار ببرد. فارسی ناچار است سوژه را تکرار کند.
    # واژه‌های کوتاه به حساب نمی‌آیند: «حالا»، «هست»، «کرد» جمله را
    # بلند می‌کنند ولی اطلاعاتی اضافه نمی‌کنند.
    tt, st = tokens(title), tokens(summary)
    fresh = {w for w in (st - tt) if len(w) >= 4}
    if len(tt) >= 3 and len(tt & st) / len(tt) > 0.7 and len(fresh) < 4:
        return False, "بازگویی تیتر"
    # نام محصول لاتین است و در متن فارسی تکرار می‌شود. سد باید نسبت را
    # بسنجد نه تعداد را؛ قبلاً سه کلمهٔ لاتین کافی بود تا پست یک ابزار
    # هوش مصنوعی رد شود، در حالی که خودِ پرامپت گفته نام لاتین را بنویس.
    latin = re.findall(r"[A-Za-z]{3,}", s)
    if len(latin) > 6 and sum(len(w) for w in latin) > 0.25 * len(s):
        return False, "لاتین زیاد"
    return True, ""


# ---------------------------------------------------------------- model

PROVIDER_DEAD = set()
# نام لایه‌ای که در این اجرا جواب داد؛ برای صرفه‌جویی در سهمیهٔ لایه‌های پشتیبان.
ACTIVE_PROVIDER = ""


def provider_health():
    return read_json(SCHED, {}).get("prov_health") or {}


def mark_provider_fail(name, minutes=45):
    """لایه‌ای که خراب است، مدتی ته صف می‌رود.

    سهمیهٔ رایگان هر چند وقت یک بار می‌خوابد یا کند می‌شود. اگر هر اجرا
    دوباره اول سراغش برویم، فقط وقت و بودجه می‌سوزانیم و به لایهٔ سالم
    نمی‌رسیم. همین چیز بود که «بودجهٔ مدل تمام شد» را می‌ساخت.
    """
    if DRY or not name:
        return
    sch = read_json(SCHED, {})
    h = sch.setdefault("prov_health", {})
    old = h.get(name) or {}
    h[name] = {
        "until": (now() + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "fails": int(old.get("fails", 0)) + 1,
    }
    write_json(SCHED, sch)


def mark_provider_ok(name):
    """لایه‌ای که جواب داد سالم است، از لیست خراب‌ها پاک می‌شود."""
    if DRY or not name:
        return
    sch = read_json(SCHED, {})
    h = sch.get("prov_health") or {}
    if name in h:
        h.pop(name, None)
        sch["prov_health"] = h
        write_json(SCHED, sch)


def model_providers():
    """فهرست ارائه‌دهنده‌های مدل، به ترتیب اولویت.

    هر ارائه‌دهنده آدرس و کلید خودش را دارد، پس قطعی یک سرویس
    کل زنجیره را نمی‌خواباند. لایه‌ای که در اجرای اخیر خطای سرور یا
    تایم‌اوت داده، ته صف می‌رود ولی حذف نمی‌شود؛ اگر همه خراب بودند
    باز هم امتحان می‌شود.
    """
    s = settings()
    out = []
    for p in s.get("providers") or []:
        key = env_clean(p.get("key", ""))
        base = (p.get("base") or "").rstrip("/")
        models = p.get("models") or []
        if key and base and models:
            item = {
                "name": p.get("name") or base,
                "type": p.get("type") or "openai",
                "base": base,
                "key": key,
                "models": models,
            }
            # مهلت جدا برای هر لایه. اگر از این‌جا رد نشود، تنظیمِ
            # settings.json بی‌اثر می‌شود و هیچ‌کس خبردار نمی‌شود.
            if p.get("timeout"):
                item["timeout"] = int(p["timeout"])
            out.append(item)
    if not out:
        base = env_clean("API_BASE").rstrip("/")
        key = env_clean("API_KEY")
        models = s.get("models") or []
        if base and key and models:
            out = [{"name": "default", "base": base, "key": key, "models": models}]
        return out

    health = provider_health()
    stamp = now()

    def cooling(prov):
        rec = health.get(prov["name"]) or {}
        try:
            return datetime.fromisoformat(rec.get("until", "")) > stamp
        except Exception:
            return False

    warm = [p for p in out if not cooling(p)]
    cold = [p for p in out if cooling(p)]
    return (warm + cold) if warm else out


# مدل‌های گوگل پیش از جواب، فکر می‌کنند. برای این کار فکر کردن هیچ
# کمکی نمی‌کند و فقط وقت می‌برد؛ یک بار همین باعث شد هر چهار تلاش
# هفتادثانیه‌ای تمام شوند و لایه با «read operation timed out» رد شود.
# به ازای هر مدل یک بار آزموده می‌شود: اگر سرور فیلد را نشناخت، دیگر
# نمی‌فرستیم.
GEMINI_THINK = {}


def gemini_call(prov, model, system, user, timeout, json_mode=True):
    """فراخوانی مستقیم رابط گوگل.

    گوگل چند در دارد و کلیدهای تازه روی بعضی از آن‌ها جواب نمی‌دهند.
    پس هر دو نسخه و هر دو روش فرستادن کلید امتحان می‌شود تا یکی جواب بدهد.
    """
    root = prov["base"].replace("/openai", "").rstrip("/")
    bases = [root]
    if root.endswith("/v1beta"):
        bases.append(root[: -len("/v1beta")] + "/v1")

    def body(think_off):
        cfg = {"temperature": 0.35}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        if think_off:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        out = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": cfg,
        }
        if system:
            out["systemInstruction"] = {"parts": [{"text": system}]}
        return out

    last = None
    for base in bases:
        for via_header in (False, True):
            url = "{}/models/{}:generateContent".format(base, model)
            headers = {}
            if via_header:
                headers["x-goog-api-key"] = prov["key"]
            else:
                url += "?key=" + urllib.parse.quote(prov["key"])
            try:
                data = http_post_json(url, body(GEMINI_THINK.get(model, True)), headers=headers, timeout=timeout)
            except urllib.error.HTTPError as exc:
                try:
                    text = exc.read().decode("utf-8", "replace")
                except Exception:
                    text = ""
                # مدل این فیلد را نمی‌شناسد. بی‌صدا برمی‌داریم و یک بار
                # دیگر همان آدرس را امتحان می‌کنیم.
                if exc.code == 400 and "thinking" in text.lower():
                    GEMINI_THINK[model] = False
                    try:
                        data = http_post_json(url, body(False), headers=headers, timeout=timeout)
                    except Exception as exc2:
                        last = exc2
                        continue
                else:
                    last = exc
                    continue
            except Exception:
                # مهلت خواندن تمام شد. یعنی آدرس درست بود و درخواست
                # رسید؛ فقط جواب دیر آمد. عوض کردن نسخه یا روش فرستادن
                # کلید همین هفتاد ثانیه را سه بار دیگر تکرار می‌کند.
                raise
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                last = RuntimeError("پاسخ خالی از گوگل")
                continue
            return extract_json(text) if json_mode else text
    raise last if last else RuntimeError("گوگل جواب نداد")


class OutOfTime(RuntimeError):
    """وقت این اجرا تمام شد.

    خطا نیست. یعنی تا اینجا رسیدیم و بقیهٔ صف برای اجرای بعدی می‌ماند.
    جدا از خطاهای واقعی نگه داشته می‌شود تا برایش هشدار تلگرام نرود.
    """


def is_net_error(exc):
    """خطای شبکه، نه خطای مدل.

    فرقش مهم است: اگر نقطهٔ اتصال کند است یا جواب نمی‌دهد، امتحان کردن
    بقیهٔ مدل‌های همان لایه هیچ فایده‌ای ندارد. هر کدام یک مهلت کامل
    می‌سوزانند و بودجه پیش از رسیدن به لایهٔ بعد تمام می‌شود.
    """
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    return any(
        key in text
        for key in ("timed out", "timeout", "connection", "reset by peer", "unreachable", "temporarily")
    )


def response_content(data, prov_name, model):
    """متن پاسخ را از دل خروجی درمی‌آورد، با پیام خطای روشن.

    بعضی لایه‌ها خطا را با کد ۲۰۰ برمی‌گردانند و شکل پاسخ عوض می‌شود.
    قبلاً این حالت یک استثنای مبهم می‌داد که فقط نوشته بود «choices» و
    معلوم نبود مشکل چیست.
    """
    if isinstance(data, dict) and data.get("error") and not data.get("choices"):
        err = data["error"]
        if isinstance(err, dict):
            err = err.get("message") or err.get("code") or err
        raise RuntimeError("{}: {}".format(prov_name, str(err)[:150]))
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        shape = ",".join(sorted(data.keys()))[:120] if isinstance(data, dict) else type(data).__name__
        raise RuntimeError(
            "{}: پاسخ {} شکل ناشناس داشت ({})".format(prov_name, model, shape)
        )


def call_models(prompt_system, prompt_user, deadline=None, budget=None, only=None):
    """زنجیرهٔ ارائه‌دهنده‌ها با مهلت مطلق.

    هر مدل در هر اجرا فقط یک بار صدا زده می‌شود. مهلت هر تلاش از
    باقی‌ماندهٔ بودجه کم می‌شود، پس یک مدل کند نمی‌تواند کل اجرا را بخورد.
    ارائه‌دهنده‌ای که کلید یا سهمیه‌اش رد شود، تا آخر اجرا کنار می‌رود.
    """
    s = settings()
    per = int(s.get("model_timeout", 90))
    hard = time.time() + int(budget if budget is not None else s.get("model_budget_sec", 240))
    if deadline:
        hard = min(hard, float(deadline))
    provs = model_providers()
    if not provs:
        raise RuntimeError("هیچ ارائه‌دهندهٔ مدلی تنظیم نشده")
    errors = []
    for prov in provs:
        if only and prov["name"] != only:
            continue
        if prov["name"] in PROVIDER_DEAD:
            continue
        # در حالت مقایسه فقط مدل اول هر لایه امتحان می‌شود تا شرط
        # برابر باشد و یک لایه با چند مدل شانس بیشتری نداشته باشد.
        # هر لایه می‌تواند مهلت خودش را داشته باشد. لایه‌ای که دوبار
        # پشت سر هم در هفتاد ثانیه جواب نداده، باید کمتر وقت بگیرد تا
        # بقیهٔ زنجیره به آن برسند.
        ptimeout = int(prov.get("timeout") or per)
        for name in (prov["models"][:1] if only else prov["models"]):
            left = int(hard - time.time())
            if left < 25:
                raise OutOfTime("وقت مدل تمام شد، {} ثانیه مانده".format(max(left, 0)))
            try:
                if prov.get("type") == "gemini":
                    result = gemini_call(prov, name, prompt_system, prompt_user, min(ptimeout, left))
                    log("  model ok:", prov["name"], name)
                    globals()["ACTIVE_PROVIDER"] = prov["name"]
                    mark_provider_ok(prov["name"])
                    return result
                data = http_post_json(
                    prov["base"] + "/chat/completions",
                    {
                        "model": name,
                        "messages": [
                            {"role": "system", "content": prompt_system},
                            {"role": "user", "content": prompt_user},
                        ],
                        "temperature": 0.35,
                        # سقف توکن صریح. بدون آن، سقف پیش‌فرض سرویس
                        # جواب پنج آیتم را نصفه می‌برید و جیسون ناقص
                        # می‌شد؛ آن هم به‌صورت «مدل هیچ پستی ننوشت»
                        # دیده می‌شد، نه به‌صورت خطا.
                        "max_tokens": int(s.get("model_max_tokens", 3000)),
                    },
                    headers={"Authorization": "Bearer " + prov["key"]},
                    timeout=min(ptimeout, left),
                )
                content = response_content(data, prov["name"], name)
                log("  model ok:", prov["name"], name)
                globals()["ACTIVE_PROVIDER"] = prov["name"]
                mark_provider_ok(prov["name"])
                return extract_json(content)
            except urllib.error.HTTPError as exc:
                errors.append("{}:{}".format(name, exc.code))
                try:
                    body = exc.read().decode("utf-8", "replace")[:160]
                except Exception:
                    body = ""
                log("  model http", exc.code, prov["name"], name, body[:110])
                # کلید رد شده، سهمیه تمام شده، یا خود سرویس خوابیده.
                # هر سه یعنی این لایه الان به کار نمی‌آید.
                if exc.code in (401, 403, 429, 500, 502, 503, 504):
                    PROVIDER_DEAD.add(prov["name"])
                    # هر خطا مدت خودش را دارد. کلید رد شده وقت می‌خواهد،
                    # سهمیهٔ دقیقه‌ای چند دقیقه، و شلوغی موقت سرویس کمتر
                    # از همه. قبلاً هر سه یک‌جا چهل‌وپنج دقیقه کنار
                    # می‌رفتند و یک ۵۰۳ گذرا کل لایه را می‌خواباند.
                    if exc.code in (401, 403):
                        cool = 45
                    elif exc.code == 429:
                        cool = 15
                    else:
                        cool = 10
                    mark_provider_fail(prov["name"], cool)
                    break
                continue
            except Exception as exc:
                errors.append("{}:{}".format(name, str(exc)[:40]))
                log("  model fail:", prov["name"], name, str(exc)[:110])
                # تایم‌اوت یعنی خود نقطهٔ اتصال کند است، نه اینکه این مدل
                # خراب باشد. سه مدل دیگر همان لایه را امتحان کردن فقط
                # بودجه را می‌سوزاند و نمی‌گذارد به لایهٔ بعد برسیم.
                if is_net_error(exc):
                    PROVIDER_DEAD.add(prov["name"])
                    mark_provider_fail(prov["name"])
                    break
                continue
    raise RuntimeError("همهٔ لایه‌ها جواب ندادند: " + " | ".join(errors[-4:]))


def extract_json(text):
    text = text or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", text):
        chunk = text[match.start() :]
        try:
            obj, _ = decoder.raw_decode(chunk)
            if isinstance(obj, (dict, list)):
                return obj
        except Exception:
            continue
    raise RuntimeError("پاسخ مدل جی‌سون نداشت")


# پرامپت در فایل جدا است تا بازبینی و ویرایشش کد را دست نزند.
# اگر فایل نبود، اجرا باید صریح خطا بدهد، نه با پرامپت خالی کار کند.
PROMPT_FILE = ROOT / "prompt.txt"


def load_system_prompt():
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("prompt.txt خوانده نشد: {}".format(exc))
    if len(text) < 500:
        raise RuntimeError("prompt.txt خیلی کوتاه است؛ احتمالاً خالی شده")
    return text


SYSTEM_PROMPT = load_system_prompt()


KIND_HINT = {
    "news": "این دسته خبر و آپدیت است، نه معرفی ابزار.",
    "tools": (
        "این دسته ابزار است؛ چیزی که مخاطب امروز باز می‌کند و به کار سایتش می‌آید. "
        "اگر پلن رایگان دارد، در details بگو چطور شروع کند."
    ),
    "security": "این دسته هشدار امنیتی است؛ فقط آسیب‌پذیری واقعی در وردپرس، افزونه یا قالب.",
    "showcase": (
        "این دسته نمایشگاه است؛ سایتی که کسی ساخته و ساختش درس‌گرفتنی است. "
        "صفحهٔ فروش خدمت یا محصول این‌جا جایی ندارد."
    ),
}


def kind_hint(kind):
    hint = KIND_HINT.get(kind)
    return (hint + "\n\n") if hint else ""


# متن پوستهٔ سایت، نه متن مطلب. وقتی استخراج شکست می‌خورد همین جمله‌ها
# به مدل می‌رسند و مدل هم درست جواب می‌دهد: هیچ پستی نمی‌نویسد.
CHROME_RE = re.compile(
    r"(you signed in with another tab|go to file|branches\s+tags|"
    r"open more actions menu|latest commit history|skip to content|"
    r"toggle navigation|cookie policy|accept all cookies)",
    re.I,
)


def item_text(it, limit=2400):
    """متنی که مدل می‌خواند.

    متن کامل همیشه بهتر از توضیح فید نیست. برای صفحهٔ مخزن، متن کامل
    همان نوار سایت است و مدل از آن چیزی نمی‌فهمد؛ در آن حالت توضیح فید
    تنها دادهٔ واقعی است. اگر هر دو باشند، هر دو می‌روند.
    """
    full = (it.get("full") or "").strip()
    desc = (it.get("desc") or "").strip()
    # آپدیت افزونه: فهرست تغییرات، تنها دادهٔ واقعی است و باید خط‌به‌خط
    # خوانده شود، نه به‌صورت یک پاراگراف چسبیده.
    upd = it.get("plugin_update") or {}
    if upd.get("changes"):
        head = "نسخهٔ {} از {} منتشر شد (نسخهٔ قبلی {}). تغییرات این نسخه:".format(
            upd.get("version", ""), upd.get("name", ""), upd.get("prev", "")
        )
        return (head + "\n" + "\n".join("- " + str(c) for c in upd["changes"]))[:limit]
    if full and (len(full) < 200 or CHROME_RE.search(full[:400])):
        full = ""
    if full and desc and not full.startswith(desc[:40]):
        return (desc + "\n" + full)[:limit]
    return (full or desc)[:limit]


def build_prompt(items, extra):
    payload = []
    for it in items:
        pub = it.get("published", "")
        if not isinstance(pub, str):
            pub = pub.isoformat() if pub else ""
        payload.append(
            {
                "id": it["id"],
                "source": it["source"],
                "lang": it.get("lang", "en"),
                "title": it["title"],
                "text": item_text(it),
                "published": pub,
                "sources_count": it.get("sources_count", 1),
                "plugin": it.get("plugin", {}),
                "update": it.get("plugin_update", {}),
            }
        )
    return extra + "\n\nخبرهای ورودی:\n" + json.dumps(payload, ensure_ascii=False)


def read_optional(name, limit=2000):
    p = ROOT / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()[:limit]


# ---------------------------------------------------------------- telegram

# سرویس‌هایی که در مقاله‌ها بیشتر تبلیغ و لینک پشتیبان‌اند تا مقصد
# واقعی مخاطب. یک بار لینک نت‌لیفای به‌عنوان «جایی که ابزار را می‌گیری»
# به مخاطب نشان داده شد.
SPONSOR_DOMAINS = {
    "netlify.com", "vercel.com", "sentry.io", "jetbrains.com",
    "digitalocean.com", "linode.com", "mongodb.com", "fastly.com",
    "aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
}
TRACKING_RE = re.compile(r"(utm_|gclid=|fbclid=|affiliate|/partner|sponsor)", re.I)

# صفحه‌هایی که خودشان محل گرفتن محصول‌اند. اگر منبع پست یکی از این‌ها
# باشد، «لینک استفاده» جدا لازم نیست؛ خود لینک منبع همان است.
PRODUCT_HOSTS = (
    "producthunt.com", "npmjs.com", "pypi.org", "packagist.org",
    "apps.apple.com", "play.google.com", "huggingface.co",
    "addons.mozilla.org", "chrome.google.com",
)


def is_product_link(url):
    """این آدرس خودش صفحهٔ محصول است، نه مقاله‌ای دربارهٔ محصول."""
    parts = urllib.parse.urlparse(url or "")
    host = parts.netloc.lower()
    path = parts.path.strip("/")
    segs = [p for p in path.split("/") if p]
    if "github.com" in host or "gitlab.com" in host:
        # فقط مخزن، نه فهرست موضوع و نه جست‌وجو.
        if segs and segs[0] in ("topics", "search", "orgs", "collections", "sponsors"):
            return False
        return len(segs) >= 2
    if any(d in host for d in PRODUCT_HOSTS):
        return bool(segs)
    return False


HINT_STOP = {
    "model", "models", "release", "released", "releases", "launch", "launches",
    "update", "updated", "updates", "announce", "announces", "announced",
    "introduce", "introduces", "available", "availability", "version",
    "plugin", "plugins", "theme", "themes", "wordpress", "tool", "tools",
    "free", "open", "weights", "weight", "benchmark", "scientific", "science",
    "parameters", "billion", "million", "with", "from", "this", "that",
    "your", "site", "sites", "news", "first", "now", "new",
}


def hint_tokens(text):
    """کلمه‌های نام‌دار عنوان را جدا می‌کند تا با آدرس محصول مقایسه شوند.

    یک بار مقاله‌ای دربارهٔ مدل تازه، لینک اقدامش صفحهٔ مدل دیگری شد که
    فقط در متن نامش آمده بود. عنوان، نام خود محصول را دارد؛ آدرس هم
    معمولاً همان نام را. همین تطبیق جلوی آن خطا را می‌گیرد.
    """
    out = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{2,}", text or ""):
        low = word.lower().strip(".-")
        if len(low) < 4 or low in HINT_STOP:
            continue
        out.add(low)
        for part in re.split(r"[.\-]", low):
            if len(part) >= 4 and part not in HINT_STOP:
                out.add(part)
    return out


def extract_use_link(article_url, raw_html, hint=""):
    """از متن خام مقاله، لینک مستقیم به ابزار یا صفحهٔ استفاده را استخراج کن.

    مقاله‌های وبلاگ معمولاً لینک محصول را درون خود دارند. این تابع آن را
    از بین همهٔ لینک‌ها پیدا می‌کند و لینک‌های شبکهٔ اجتماعی را دور می‌ریزد.
    """
    # اگر خودِ صفحه محصول است، لینک استفادهٔ جدا معنا ندارد. هر لینک
    # دیگری داخل آن بخشی از مستندات یا وابستگی‌هاست؛ یک بار مخزن wrk
    # به‌عنوان «جایی که ابزار را می‌گیری» پیشنهاد شد.
    if is_product_link(article_url):
        return ""
    article_domain = urllib.parse.urlparse(article_url).netloc.lower()
    links = set()
    for m in re.finditer(r'href=["\'](https?://[^"\']+)["\']', raw_html):
        links.add(m.group(1))
    for m in re.finditer(r'href=["\'](/[^"\']+)["\']', raw_html):
        links.add(urllib.parse.urljoin(article_url, m.group(1)))
    if not links:
        return ""

    social = {
        "twitter.com", "x.com", "facebook.com", "instagram.com",
        "linkedin.com", "youtube.com", "tiktok.com", "reddit.com",
        "pinterest.com", "t.me", "discord.gg", "slack.com",
    }
    product_domains = {
        "github.com", "gitlab.com", "bitbucket.org",
        "wordpress.org", "wordpress.com",
        "apps.apple.com", "play.google.com",
        "chrome.google.com", "addons.mozilla.org",
        "npmjs.com", "pypi.org", "packagist.org",
        "figma.com", "canva.com", "notion.so",
        "openai.com", "anthropic.com", "claude.ai",
        "huggingface.co", "replicate.com",
        "cloudflare.com",
    }

    # صفحهٔ فهرست، صفحهٔ محصول نیست. یک بار لینک اقدام پستی که منبعش
    # یک مخزن گیت‌هاب بود، به فهرست موضوع‌ها رسید:
    # github.com/topics/mcp — که هیچ ابزاری در آن نیست.
    INDEX_PATHS = re.compile(
        r"^/(topics|search|explore|trending|collections|marketplace|sponsors|"
        r"orgs|settings|login|signup|features|pricing|about)(/|$)",
        re.I,
    )

    def is_index(link):
        return bool(INDEX_PATHS.search(urllib.parse.urlparse(link).path or "/"))

    # لینک خاص‌تر بهتر است. قبلاً کوتاه‌ترین لینک انتخاب می‌شد و برای
    # یک قالب، ریشهٔ سایت به‌جای صفحهٔ خود قالب برمی‌گشت.
    def specific(link):
        path = urllib.parse.urlparse(link).path.strip("/")
        depth = len([p for p in path.split("/") if p])
        return (-depth, len(link))

    ranked = sorted(links, key=specific)

    # ۱. دامنه‌های شناخته‌شدهٔ محصول.
    # ریشهٔ سایت به کار نمی‌آید و فهرست دسته هم نه؛ صفحهٔ خود محصول
    # می‌خواهیم. عمق دو معمولاً همان است (مخزن، قالب، پلاگین)؛ عمق
    # کمتر فهرست است و عمق بیشتر صفحهٔ فرعی.
    def product_rank(link):
        path = urllib.parse.urlparse(link).path.strip("/")
        depth = len([p for p in path.split("/") if p])
        return (abs(depth - 2), -depth, len(link))

    # آدرسی که نام خود محصول در آن هست، بر آدرسی که فقط در متن مقاله
    # نامش آمده اولویت دارد.
    hts = hint_tokens(hint)

    def name_hit(link):
        low = link.lower()
        return 1 if any(t in low for t in hts) else 0

    def rank(link):
        return (0 - name_hit(link),) + product_rank(link)

    for link in sorted(links, key=rank):
        domain = urllib.parse.urlparse(link).netloc.lower()
        if any(d in domain for d in product_domains):
            path = urllib.parse.urlparse(link).path.strip("/")
            if domain == "github.com" and path.count("/") < 1:
                continue
            # ریشهٔ دامنه هیچ‌وقت صفحهٔ محصول نیست. «v0.wordpress.com»
            # یک وبلاگ است، نه محصولی که مخاطب برود بگیرد.
            if not path:
                continue
            if is_index(link):
                continue
            # صفحهٔ گزارش تخلف و شکایت، صفحهٔ محصول نیست. یک بار خط
            # «جزئیات بیشتر از این آدرس» به آدرس گزارش تخلف وردپرس رسید،
            # چون آن لینک در فوتر صفحه بود و دامنه‌اش محصولی است.
            if re.search(
                r"/(abuse|report|dmca|privacy|terms|contact|cookie|share|trackback|wp-admin|wp-login)(/|$)",
                "/" + path,
                re.I,
            ) or re.search(r"(report_url|return_url|redirect_to|redirect)=", link, re.I):
                continue
            if re.search(r"/(reviews?|ratings?|support|feed)/?$", "/" + path, re.I):
                continue
            link = strip_tracking(link)
            # اگر بهترین لینک همان آدرس خود مقاله است، لینک اقدام جدا
            # لازم نیست. پستی که منبعش یک مخزن گیت‌هاب است نباید خط
            # «همین حالا بازش کن» داشته باشد که به جای دیگری برود.
            if link.rstrip("/") == (article_url or "").rstrip("/"):
                return ""
            # نام محصول در عنوان هست ولی هیچ لینکی با آن جور نیست: یعنی
            # این‌ها محصول‌های دیگری‌اند که در متن نامشان آمده. لینک غلط
            # بدتر از نبودن لینک است.
            if hts and not name_hit(link):
                return ""
            return link

    # ۲. برای وبلاگ الگنت تمز، لینک صفحهٔ محصول
    if "elegantthemes.com" in article_domain:
        for link in ranked:
            if "elegantthemes.com/gallery/" in link or "elegantthemes.com/divi" in link:
                return link

    # ۳. برای وبلاگ‌های وردپرس، لینک مخزن وردپرس
    if "wpmayor.com" in article_domain:
        for link in ranked:
            if "wordpress.org" in link:
                return link

    # ۴. اگر هیچ‌کدام نبود، هیچ. قبلاً اولین لینک خارجی برگردانده می‌شد و
    # نتیجه‌اش این بود که یک خبر با «جایی که ابزار را می‌گیری» به پروفایل
    # XFN و ریشهٔ وردپرس و داکیومنت گیت‌هاب می‌رسید. لینک غلط بدتر از
    # نبودن لینک است؛ پست بدون خط اقدام هم کامل است.
    return ""


def strip_tracking(url):
    """پارامترهای ردیابی را از آدرس برمی‌دارد."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    keep = [
        p
        for p in parts.query.split("&")
        if p and not re.match(r"^(utm_|gclid|fbclid|ref|aff|partner|sponsor)", p, re.I)
    ]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(keep), ""))


def fetch_article(url, limit=3200, hint=""):
    """متن مقاله و لینک استفاده را با هم می‌آورد."""
    try:
        raw = http_get(url, timeout=15).decode("utf-8", "replace")
    except Exception:
        return "", ""
    use_link = extract_use_link(url, raw, hint)
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", raw)
    # صفحه‌های امروزی متن اصلی را داخل article می‌گذارند و باقی صفحه نوار
    # بالا و فهرست است. گیت‌هاب هم README را همان‌جا می‌گذارد. قبلاً به‌جای
    # README متن «You signed in with another tab» به مدل می‌رسید و مدل
    # هیچ پستی نمی‌نوشت؛ صف پر می‌شد و کانال خالی می‌ماند.
    body = raw
    art = re.search(r"(?is)<article[^>]*>(.*?)</article>", raw)
    if art:
        body = art.group(1)
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
    if not paras:
        paras = re.findall(r"(?is)<(?:div|li)[^>]*>(.*?)</(?:div|li)>", body)
    if not paras and body is not raw:
        # بلوک article خالی بود؛ برگرد به کل صفحه.
        body = raw
        paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
        if not paras:
            paras = re.findall(r"(?is)<(?:div|li)[^>]*>(.*?)</(?:div|li)>", body)
    chunks = []
    total = 0
    for p in paras:
        text = clean_text(p)
        # آستانه پایین است چون قیمت و نسخه و تاریخ معمولاً در جمله‌های
        # کوتاه می‌آیند و قبلاً همان‌ها دور ریخته می‌شدند.
        if len(text) < 25:
            continue
        chunks.append(text)
        total += len(text)
        if total >= limit:
            break
    text = " ".join(chunks)[:limit]
    return text, use_link


def fetch_article_text(url, limit=2500):
    """فقط متن مقاله. برای سازگاری با کد قدیمی."""
    text, _ = fetch_article(url, limit)
    return text


FREE_HINT = re.compile(
    r"(رایگان|مجانی|بدون هزینه|بدون پرداخت|free tier|free plan|free trial|credits|"
    r"no credit card|بدون کارت|آزمایشی|هدیه|کردیت|سهمیه|متن‌باز|open source|open-source)",
    re.I,
)

PLUGIN_HINT = re.compile(
    r"(افزونه|پلاگین|plugin|قالب|theme|addon|ماژول|ووکامرس|woocommerce|المنتور|elementor)",
    re.I,
)

# قالب، پلاگین نیست. یک بار قالب Basin با برچسب «پلاگین تازه» منتشر شد،
# چون فهرست بالا هر دو را یک‌جا داشت. این دو فقط برای تفکیک همان دو
# به کار می‌آیند و جای فهرست بالا را نمی‌گیرند.
THEME_WORD = re.compile(r"(قالب|theme)", re.I)
PLUGIN_WORD = re.compile(r"(افزونه|پلاگین|plugin|addon|ماژول)", re.I)


def is_theme_title(text):
    """تیتر دربارهٔ قالب است، نه افزونه."""
    return bool(THEME_WORD.search(text or "")) and not PLUGIN_WORD.search(text or "")
TUTORIAL_HINT = re.compile(
    r"(آموزش|راهنمای|گام‌به‌گام|چطور|guide|tutorial|how to|walkthrough|step by step)", re.I
)
UPDATE_HINT = re.compile(
    r"(به‌روزرسانی|بروزرسانی|آپدیت|patch|patched|hotfix|bump|service pack)", re.I
)
# نسخهٔ تازهٔ یک چیز موجود، آپدیت است؛ تولد یک محصول تازه، خبر.
# قبلاً هر «۱.۰» در متن کافی بود تا پست «آپدیت» شود، حتی وقتی مدل
# تازه‌ای برای اولین بار منتشر شده بود و کاربر کاری برای به‌روز کردن
# نداشت.
RELEASE_VERB = re.compile(
    r"(منتشر|عرضه|آزاد|در دسترس|release|released|available|shipped|rolled out|out now)", re.I
)


def version_bump(text):
    """این عدد، نسخهٔ چیزی است یا بخشی از نام محصول؟

    «وردپرس ۷.۱.۴» نسخه است: عدد با فاصله آمده. «Hydrogen-1.0» نام است:
    عدد به خود نام چسبیده. همین تفاوت، آپدیت را از خبر جدا می‌کند.
    """
    for m in re.finditer(r"(?P<pre>.)(?P<ver>[\d۰-۹]+\.[\d۰-۹]+)", str(text or "")):
        pre = m.group("pre")
        if pre in "-_./" or (pre.isascii() and pre.isalpha()):
            continue
        return True
    return False
NEW_HINT = re.compile(r"(تازه|جدید|معرفی|رونمایی|new|launch|introduc|debut|unveil)", re.I)
WP_HINT = re.compile(
    r"(وردپرس|wordpress|افزونه|plugin|هسته|core|قالب|theme|ووکامرس|woocommerce|المنتور|elementor)",
    re.I,
)
URGENT_HINT = re.compile(
    r"(actively exploited|actively attempt|attempting to exploit|already exploiting|"
    r"exploited in the wild|under attack|zero-day|در حال سوءاستفاده|سوءاستفادهٔ فعال|"
    r"در حال بهره‌برداری|بهره‌برداری فعال)",
    re.I,
)
# «آموزش» یعنی راهنما، نه خبر. تیتری که با «چطور» شروع می‌شود آموزش است.
# نسخهٔ آزمایشی، هشدار امنیتی نیست. متنش از اصلاحیه حرف می‌زند ولی
# مخاطب قرار نیست امروز کاری بکند، جز این که روی سایت اصلی نصبش نکند.
BETA_RE = re.compile(r"\b(?:beta|rc\d*|release\s+candidate)\b|بتا|نسخهٔ? آزمایشی", re.I)

TUTORIAL_TITLE = re.compile(
    r"(how to|how-to|tutorial|walkthrough|step by step|step-by-step|a guide to|"
    r"^\s*(?:creating|building|using|understanding|designing|adding|making|"
    r"setting up|getting started)\b|"
    r"آموزش|راهنمای|گام‌به‌گام|چطور)",
    re.I,
)
# شمارهٔ نسخه یعنی به‌روزرسانی چیزی که از قبل بوده، نه محصول تازه.
VERSION_HINT = re.compile(
    r"(نسخهٔ?\s*\d|\bversion\s*\d|\bv\d+\.\d+|\d+\.\d+)",
    re.I,
)
# فرصت باید به کار مخاطب این کانال بیاید: هوش مصنوعی، طراحی، سایت، کد.
OFFER_TOPIC = re.compile(
    r"(هوش مصنوعی|\bai\b|a\.i|llm|model|api|token|credit|کردیت|توکن|سهمیه|رایگان|free|"
    r"design|طراحی|site|سایت|wordpress|وردپرس|plugin|افزونه|image|تصویر|video|ویدیو|"
    r"code|کد|developer|توسعه|server|سرور|cloud|ابر)",
    re.I,
)
# چیزهایی که به این کانال ربط ندارند، حتی اگر رایگان باشند.
OFFER_JUNK = re.compile(
    r"(board game|card game|video game|بازی تخته|بازی ویدیویی|فیلم|movie|music|آهنگ|"
    r"album|concert|کنسرت|lottery|قرعه‌کشی|casino|شرط‌بندی|gambling)",
    re.I,
)
# «نامشخص» یعنی فیلد پوک. پستی که وضعیتش معلوم نیست نباید منتشر شود.
HOLLOW_RE = re.compile(r"(نامشخص|نامعلوم|مشخص نیست|در دسترس نیست|unknown|unclear|ناموجود)", re.I)

# صورت‌جلسه و گزارش وضعیت، خبر نیست. تیم‌های متن‌باز هر هفته این‌ها را
# می‌نویسند و کانال از «خلاصهٔ جلسه» پر می‌شد، نه از خبر.
# سرگرمی و تزئینی: والپیپر، محافظ صفحه، تقویم رومیزی. این‌ها خبر
# نیستند و فقط توکن می‌سوزانند.
JUNK_NEWS_RE = re.compile(r"(wallpaper|screensaver|desktop calendar|calendar of)", re.I)

# خبری که موضوعش پول، دادگاه، یا سیاست داخلی یک شرکت است و به ساخت سایت
# ربطی ندارد، به کار مخاطب نمی‌آید. یک بار خبر «گلدمن ساکس دسترسی
# معامله‌گران به ابزار کدنویسی را قطع کرد» پست شد و مدل برایش ربط
# ساختگی ساخت: «برای کسانی که با این ابزارها سایت می‌سازند مهم است».
# چنین جمله‌ای در منبع نبود؛ خودِ مدل ساخته بود تا خبر را به کانال بچسباند.
OFF_AUDIENCE_RE = re.compile(
    r"(goldman|jpmorgan|morgan stanley|hedge fund|wall street|bankers?|"
    r"investment bank|\bipo\b|earnings report|stock (price|market)|shareholders?|"
    r"valuation|funding round|raises? \$|series [abc]\b|lawsuit|sues|sued|"
    r"\bcourt\b|antitrust|copyright|senate|congress|parliament|election|"
    r"معامله‌گر|بانکدار|بورس|سهام|شکایت|دادگاه|دادگستری|کنگره|مجلس|انتخابات)",
    re.I,
)
SITE_TOPIC_RE = re.compile(
    r"(wordpress|woocommerce|elementor|\bplugins?\b|\bthemes?\b|\bwebsite\b|"
    r"\bweb design\b|\bseo\b|\bhosting\b|\bcms\b|"
    r"وردپرس|ووکامرس|المنتور|افزونه|پلاگین|قالب|سایت)",
    re.I,
)

# کانال برای مخاطب وردپرسی است، نه برای دنبال‌کنندهٔ خبر هوش مصنوعی.
# ترتیب صف و ترتیب فرستادن به مدل باید همین را نشان بدهد. فید عمومی
# روزی چهل آیتم می‌دهد و اگر فقط تازه‌بودن معیار باشد، ته صف را پر
# می‌کند و خبر وردپرسی هیچ‌وقت به نوبت نمی‌رسد. سه رتبه کافی است:
#   ۰ وردپرس و ساخت سایت، از هر منبعی، حتی خبر هوش مصنوعی
#   ۱ فید وردپرسی که کلیدواژه ندارد (طراحی، استایل، هاست)
#   ۲ هوش مصنوعی عمومی که هیچ نشانی از وردپرس و ساخت سایت ندارد
WP_RANK_WP = 0
WP_RANK_NEAR = 1
WP_RANK_AI = 2


def wp_first(item):
    blob = " ".join(str(item.get(k) or "") for k in ("title", "desc", "link", "source"))
    if SITE_TOPIC_RE.search(blob):
        return WP_RANK_WP
    if (item.get("group") or "") != "ai":
        return WP_RANK_NEAR
    return WP_RANK_AI


def wp_first_sort(rows):
    """مرتب‌سازی پایدار: وردپرس اول، ترتیب قبلی داخل هر رتبه حفظ می‌شود."""
    return sorted(rows, key=wp_first)


# مدل وقتی خبر به سایت ربط ندارد، خودش ربط می‌سازد: «ولی این تصمیم برای
# کسانی که با این ابزارها سایت می‌سازند مهم است». این جمله در منبع نیست.
# سد قبلی (OFF_AUDIENCE_RE) جلوی رفتن چنین خبری به مدل را می‌گیرد، ولی
# بازنویسی پست‌های قدیمی از آن سد رد نمی‌شود. پس خودِ متن را هم می‌سنجیم:
# اگر جملهٔ ربط در پست باشد و منبع حرفی از سایت نزده باشد، ساختگی است.
SELF_MADE_RELEVANCE = re.compile(
    r"کسانی\s+که[^.\n]{0,60}(سایت|وب\s*سایت|فروشگاه)[^.\n]{0,25}"
    r"می[\s\u200c]{0,1}(سازند|سازد)"
    r"|(این|آن)\s+(تغییر|تصمیم|خبر|موضوع)[^.\n]{0,60}(مهم|به\s*کار)\s*"
    r"(است|می[\s\u200c]{0,1}آید)",
)


def relevance_ok(text, source_text):
    """ربط ساختگی را می‌گیرد، نه ربط واقعی."""
    if not SELF_MADE_RELEVANCE.search(str(text or "")):
        return True
    return bool(SITE_TOPIC_RE.search(str(source_text or "")))

# مخزن‌های «awesome» فهرست پیوند جمع می‌کنند، ابزار نمی‌سازند. کانال
# ابزار معرفی می‌کند. یک بار چنین فهرستی با برچسب «آموزش» پست شد و
# خود مدل هم نوشت «این یک لیست مرجعی است، نه ابزاری برای اجرا».
# خط تیره یا زیرخط لازم است تا «awesome new tool» گیر نیفتد.
# مقالهٔ فهرستی خبر نیست، حتی اگر تازه باشد. «۷ بهترین افزونهٔ وردپرس»
# و «بهترین قالب‌های ۲۰۲۶» هیچ اتفاقی را گزارش نمی‌کنند؛ فقط لیست‌اند.
# سد قبلی فقط «awesome-» را می‌گرفت و یک مقالهٔ «۷ Best» از آن رد شد.
LIST_REPO_RE = re.compile(
    r"awesome[-_]"
    r"|\b(?:top|best)\s+\d+\b"
    r"|\b\d+\s+(?:best|top|greatest)\b"
    r"|\b(?:best|top)\s+(?:\w+\s+){0,3}(?:plugins?|themes?|tools?|servers?|hosting|"
    r"page\s+builders?|wordpress\s+themes?)\b"
    r"|\bbest\s+practices\b"
    r"|بهترین‌های"
    r"|بهترین\s+(?:افزونه|قالب|ابزار|سرور|هاست|پلاگین)"
    r"|فهرست\s+(?:بهترین|کامل)"
    r"|راهنمای\s+خرید",
    re.I,
)

# دستهٔ ابزار از فیدهای عمومی هم پر می‌شود: هکرنیوز، ردیت، گیت‌هاب. آن
# فیدها هر چیزی را قبول می‌کنند، از بازی ۱۹۹۱ تا داشبورد در نوار مک.
# آیتمی که هیچ نشانه‌ای از هوش مصنوعی یا ساخت سایت ندارد، حتی به مدل
# نمی‌رود؛ هم توکن می‌سوزاند، هم دسته را شبیه انبار می‌کند.
BUILDER_TOPIC = re.compile(
    r"(\bai\b|artificial intelligence|\bllm\b|\bagents?\b|\bmcp\b|\bgpt\b|"
    r"wordpress|woocommerce|elementor|\bdivi\b|\bplugins?\b|\bthemes?\b|"
    r"website|\bweb app\b|frontend|backend|\bcss\b|\bhtml\b|javascript|\bseo\b|"
    r"hosting|domain name|\bapi\b|developer|coding|coder|\bsdk\b|"
    r"open.source|self.hosted|"
    r"هوش مصنوعی|مدل|عامل|وردپرس|افزونه|قالب|سایت|برنامه‌نویس)",
    re.I,
)

# مدل خودش می‌گوید که این چیز ابزار نیست. حرفش را قبول می‌کنیم.
NOT_A_TOOL_SELF_RE = re.compile(
    r"(لیست\s+(مرجعی|پیوند|لینک)|فهرست\s+(مرجعی|پیوند)|"
    r"نه\s+ابزاری\s+برای|مجموعه‌ای\s+از\s+(پیوند|لینک)|"
    r"صرفاً\s+یک\s+لیست|صرفا\s+یک\s+لیست)",
    re.I,
)

# مدل گاهی در متن خودش می‌نویسد که خبر به این کانال ربطی ندارد و باز هم
# پستش می‌کند. یک بار مجموعه والپیپر دسکتاپ با همین جمله منتشر شد:
# «این مطلب ارتباطی با وردپرس یا ساخت سایت ندارد.» تکیه بر نمرهٔ مدل
# کافی نیست؛ حرف خودش را هم باید شنید.
OFFTOPIC_SELF_RE = re.compile(
    r"(ارتباطی?\s+با[^.\n]{0,40}?(ندارد|نمی‌?شود|نیست)|"
    r"خارج\s+از\s+(حوزه|موضوع|تخصص|زمینه)|"
    r"بی[\s\u200c]?ربط|"
    r"(مربوط|مرتبط)\s+نیست|"
    r"ربطی\s+ندارد)",
    re.I,
)

NON_NEWS_RE = re.compile(
    r"(meeting[- ]summary|meeting[- ]notes|meeting[- ]minutes|weekly[- ]update|"
    r"monthly[- ]update|bi-?weekly[- ]update|status[- ]update|"
    r"call[- ]for[- ]testing|call[- ]for[- ]feedback|dev[ -]?chat|agenda|"
    r"summary[- ]of[- ]the[- ]meeting|"
    # تیم‌های توسعهٔ وردپرس کار روزمرهٔ خودشان را هم منتشر می‌کنند:
    # «تیم چه چیزی این هفته منتشر کرد»، «گفتگوی دوهفتگی»، «تغییرات تیم
    # بازبینی». این‌ها گزارش داخلی است، نه خبر برای مخاطب فارسی.
    r"team[- ]update|team[- ]changes|core[- ]team|review[- ]team|"
    r"bi-?weekly[- ](chat|call|meeting|sync)|weekly[- ](chat|sync)|"
    r"what[- ]we[- ]shipped|shipped[- ]this[- ]week|"
    # صفحهٔ گردآوری ماهانه، خبر نیست: یک صفحه است که ده اتفاق ماه را
    # فهرست می‌کند. مدل از آن یک تیتر درمی‌آورد که در خود صفحه نیست،
    # و خبرِ یک ماه پیش تازه به نظر می‌رسد. یک بار «رونمایی از مدل
    # جمینای» از همین صفحه بیرون آمد و پست شد.
    r"what[- ]we[- ]announced|the[- ]latest[- ].{0,30}(news|updates).{0,25}(in|of)[- ]"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"monthly[- ](roundup|recap|digest)|month[- ]in[- ]review|round[- ]?up:|"
    r"خلاصهٔ ماه|مرور ماه|این ماه در|"
    r"صورت‌جلسه|خلاصهٔ جلسه|خلاصه جلسه|گزارش هفتگی|گزارش جلسه|"
    r"جلسهٔ هفتگی|یادداشت جلسه|دستور جلسه)",
    re.I,
)

# پادکست و وبینار خبر نیست. تاورن هر هفته یک قسمت منتشر می‌کند و بعد از
# قانون «وردپرس اول» سر صف می‌نشست، در حالی که هیچ اتفاق تازه‌ای را
# گزارش نمی‌کند. آدرس هم بررسی می‌شود، چون تیتر همیشه واژه را ندارد.
PODCAST_RE = re.compile(
    r"/podcasts?/|\bpodcasts?\b|\bwebinars?\b|^#\d+\s*[\u2010-\u2015-]|پادکست|وبینار",
    re.I,
)

# برچسب مجاز هر دسته. مدل اجازه ندارد از این فهرست بیرون بزند.
LABELS = {
    "news": ("خبر", "آپدیت", "هشدار امنیتی", "فوری", "پلاگین تازه", "آموزش"),
    "tools": ("ابزار", "پلاگین تازه", "آموزش"),
    "offers": ("فرصت",),
    "security": ("هشدار امنیتی", "فوری"),
    "showcase": ("نمایشگاه",),
}
DEFAULT_LABEL = {
    "news": "خبر",
    "tools": "ابزار",
    "offers": "فرصت",
    "security": "هشدار امنیتی",
    "showcase": "نمایشگاه",
}


def entry_skipped(entry):
    """رد کردن یک راه دارد: فیلد skip.

    نمره ابزار رد کردن نیست. اگر مدل پست را نوشته، نمره فقط ترتیب
    انتشار را می‌گوید. قبلاً نمرهٔ صفر هم معنی رد داشت و همین دو
    معنی، رفتار مدل را تصادفی کرده بود.
    """
    raw = (entry or {}).get("skip")
    if raw is True:
        return True
    return str(raw).strip().lower() in ("true", "1", "yes", "بله")


def classify_label(kind, text, model_label="", urgent=False, title="", source_title=""):
    """برچسب را از دسته و متن می‌سازد، نه از سلیقهٔ مدل.

    خبر هرگز ابزار نمی‌شود، ابزار هرگز خبر نمی‌شود، و برچسب امنیتی فقط
    وقتی می‌آید که آسیب‌پذیری واقعی در وردپرس یا افزونه باشد.
    """
    text = str(text or "").translate(DIGITS_TO_LATIN)
    title = str(title or "").translate(DIGITS_TO_LATIN)
    source_title = str(source_title or "").translate(DIGITS_TO_LATIN)
    model_label = (model_label or "").strip()
    if kind == "security":
        return "فوری" if (urgent or URGENT_HINT.search(text)) else "هشدار امنیتی"
    if kind == "showcase":
        return "نمایشگاه"
    if kind == "offers":
        return "فرصت"

    # راهنما خبر نیست. «How To ...» در تیتر منبع، یا «چطور» در تیتر فارسی.
    tutorial = bool(TUTORIAL_TITLE.search(title) or TUTORIAL_TITLE.search(source_title))
    # بتا و آر‌سی هشدار امنیتی نیستند، حتی اگر متنشان از «اصلاحیهٔ امنیتی»
    # حرف بزند. یک بار «وردپرس ۷.۲ بتای ۲» با برچسب هشدار امنیتی پست شد.
    beta = bool(BETA_RE.search(title) or BETA_RE.search(source_title))
    advisory = is_advisory(text) and bool(WP_HINT.search(text)) and not tutorial and not beta
    # «نسخهٔ فارسی» نسخهٔ تازه نیست. برای همین فقط شمارهٔ واقعی یا فعل انتشار شاهد است.

    if kind == "news":
        if advisory:
            return "فوری" if (urgent or URGENT_HINT.search(text)) else "هشدار امنیتی"
        if tutorial:
            return "آموزش"
        # «پلاگین تازه» فقط برای محصول تازه، و فقط وقتی خود مدل هم همین را گفته باشد.
        # آموزش دربارهٔ قالب پلاگین تازه نیست، و نسخهٔ تازهٔ یک افزونهٔ قدیمی هم نیست.
        if (
            model_label == "پلاگین تازه"
            and PLUGIN_HINT.search(text)
            and not VERSION_HINT.search(title)
            and not is_theme_title(title)
        ):
            return "پلاگین تازه"
        if UPDATE_HINT.search(text) or (version_bump(text) and RELEASE_VERB.search(text)):
            return "آپدیت"
        return "خبر"

    # دستهٔ ابزار: پیشنهاد مدل فقط وقتی قبول می‌شود که متن هم شاهدش باشد.
    if model_label == "آموزش" and TUTORIAL_HINT.search(text):
        return "آموزش"
    if tutorial:
        return "آموزش"
    if (
        model_label == "پلاگین تازه"
        and PLUGIN_HINT.search(text)
        and not VERSION_HINT.search(title)
        and not is_theme_title(title)
    ):
        return "پلاگین تازه"
    return "ابزار"


def og_image(url):
    try:
        raw = http_get(url, timeout=12).decode("utf-8", "replace")
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]
        for pat in patterns:
            m = re.search(pat, raw, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
FA_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def num(value, default=0.0):
    """عدد از خروجی مدل.

    مدل هر از گاهی جای عدد رشته می‌گذارد. یک رشته، کل اجرا را نباید
    از کار بیندازد.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def fa_num(value):
    return str(value).translate(FA_DIGITS)


def to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy, gy = 979, gy - 1600
    else:
        jy, gy = 0, gy - 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def stamp(when=None):
    dt = when or now()
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            dt = now()
    if not isinstance(dt, datetime):
        dt = now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TZ)
    jy, jm, jd = to_jalali(local.year, local.month, local.day)
    return "{} {} {} · {}".format(
        fa_num(jd), FA_MONTHS[jm - 1], fa_num(jy), fa_num(local.strftime("%H:%M"))
    )


def esc(text):
    return html.escape(str(text or ""), quote=False)


# ---------- خوانایی فارسی و لاتین ----------
# بدون این کار، عبارت لاتین وسط جملهٔ فارسی جهت پاراگراف را به‌هم می‌ریزد
# و پست کثیف دیده می‌شود. ایزوله‌ی چپ‌به‌راست، همان چیزی است که مرورگرها
# برای همین کار ساخته‌اند و در تلگرام هم بی‌رنگ و نامرئی است.
LRI = "\u2066"
PDI = "\u2069"
# نام‌های چندبخشی باید یک واحد باشند. قبلاً «Goldman» و «Sachs» جدا
# ایزوله می‌شدند و در جملهٔ راست‌به‌چپ جابه‌جا می‌شدند: «Sachs Goldman».
LATIN_RUN = re.compile(
    r"[A-Za-z][A-Za-z0-9\.\-_/+#'’&]*"
    r"(?:[ \t]+[A-Za-z0-9][A-Za-z0-9\.\-_/+#'’&]*)*"
)


RTL_MARK = "\u200f"


def rtl_guard(text):
    """خطی که با لاتین یا رقم شروع شود، جهت پاراگراف را چپ می‌کند.

    تلگرام ایزولهٔ لاتین را برای تعیین جهت پاراگراف نادیده می‌گیرد. یک
    بار خط اول پست با نام لاتین شروع شد و کل خط به چپ چسبید: «Elementor
    ۴.۳.۶ …» سمت چپ نشست و خواندنش سخت شد. علامت راست‌به‌چپ ابتدای
    خط، اولین کاراکتر قوی پاراگراف را فارسی می‌کند.
    """
    out = []
    for line in str(text or "").split("\n"):
        head = line.lstrip()
        if head and head[0].isascii() and head[0].isalnum():
            line = RTL_MARK + line
        out.append(line)
    return "\n".join(out)


def bidi(text):
    text = str(text or "")
    if not text:
        return text
    try:
        if not settings().get("bidi_isolate", True):
            return rtl_guard(text)
    except Exception:
        pass
    return LATIN_RUN.sub(lambda m: LRI + m.group(0) + PDI, rtl_guard(text))


def send_to(chat, text, preview=True):
    if DRY or not (TG_TOKEN and chat):
        log("--- post ---")
        log(text)
        return {"ok": True, "dry": True}
    try:
        return http_post_json(
            "https://api.telegram.org/bot{}/sendMessage".format(TG_TOKEN),
            {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": not preview},
            timeout=30,
        )
    except urllib.error.HTTPError:
        # تلگرام صریح رد کرد. چیزی منتشر نشده، پس ثبت هم نمی‌شود.
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # درخواست رفت ولی جواب برنگشت. تلگرام در این حالت معمولاً پیام را
        # گرفته است. قبلاً این استثنا بالا می‌رفت، پست ثبت نمی‌شد و اجرای
        # بعدی همان خبر را دوباره می‌فرستاد: دو هشدار بروتلی با یک لینک،
        # چهار دقیقه فاصله، و هیچ ردی در آرشیو. حالا «فرستاده‌شده ولی
        # تأییدنشده» حساب می‌شود تا تکرار نشود.
        log("  telegram unconfirmed:", str(exc)[:100])
        return {"ok": True, "unconfirmed": True}


def send(text, preview=True):
    return send_to(TG_CHAT, text, preview)


def alert(text):
    """پیام‌های خطا و سلامت به چت شخصی می‌روند، نه کانال."""
    return send_to(TG_ADMIN or TG_CHAT, text, False)


def send_photo(photo_url, caption):
    if DRY or not (TG_TOKEN and TG_CHAT):
        log("--- photo post ---")
        log(caption)
        return {"ok": True, "dry": True}
    return http_post_json(
        "https://api.telegram.org/bot{}/sendPhoto".format(TG_TOKEN),
        {"chat_id": TG_CHAT, "photo": photo_url, "caption": caption[:1000], "parse_mode": "HTML"},
        timeout=30,
    )


def emoji_for(label):
    return (settings().get("emoji") or {}).get(label, "")


# کتیشن جای چند نکته است، نه جای بازنشر صفحهٔ خوانده‌شده. یک بار مدل
# هشت خط نکته فرستاد و پست از یک خبر به یک فایل خواندنی تبدیل شد.
MAX_DETAIL_LINES = 4


def version_only_line(line):
    """خطی که فقط شمارهٔ نسخه یا «بتا» است، نکته نیست.

    «نسخهٔ ۷.۱.۴» و «بتای ۲» آخر کادر می‌ماندند و چیزی به خواننده
    اضافه نمی‌کردند. اگر بعد از برداشتن رقم و کلمهٔ نسخه چیزی نماند،
    خط دور ریخته می‌شود.
    """
    probe = re.sub(r"[\u200b\u200c\u200e\u200f\u2066\u2067\u2068\u2069]", "", str(line or "")).strip()
    # شناسهٔ CVE هم اگر تنها خط باشد نکته نیست؛ بالاتر در متن آمده و
    # تکرارش فقط کادر را شلوغ می‌کند.
    probe = re.sub(r"cve[-\u2010-\u2015\s]*[0-9۰-۹]{4}[-\u2010-\u2015\s]*[0-9۰-۹]+", " ", probe, flags=re.I)
    probe = re.sub(r"[0-9۰-۹.,]+", "", probe)
    probe = re.sub(r"(?:نسخه[ٔۀ]?|بتای?|آزمایشی|version|v|rc)\s*", "", probe, flags=re.I)
    return not probe.strip(" .،:؛-–—")


def trim_details(text, limit=MAX_DETAIL_LINES):
    lines = [ln.strip() for ln in fix_newlines(text).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not version_only_line(ln)]
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + " …"


# حرف‌هایی که با هم جمله نمی‌سازند. اگر تنها فارسیِ یک خط همین‌ها باشند،
# خط داده نیست؛ یک عدد است با یک اسم لاتین چسبیده به آن.
META_STOP = frozenset(
    ("به", "از", "تا", "با", "در", "و", "یا", "بر", "که", "را", "هم", "است", "شد", "این", "آن")
)


def meta_fragment(text):
    """خطی مثل «PageSpeed ۷۰ به ۹۹» داده نیست.

    از یک مقالهٔ تحلیلی برداشته شد و نه می‌گفت کدام سایت، نه چرا. آخر
    کادر یک خط تنها و بی‌معنی می‌سازد و بدتر از خالی بودن است، چون
    شکل داده دارد. متای نسخه (فقط رقم) و متای فارسی دست نمی‌خورد.
    """
    s = re.sub(r"[\u200b\u200c\u200e\u200f\u2066\u2067\u2068\u2069]", "", str(text or ""))
    if not re.search(r"[A-Za-z]", s):
        return False
    # ارقام فارسی هم در بازهٔ حروف عربی‌اند. اگر جدا نشوند، خودشان
    # «کلمهٔ فارسی» حساب می‌شوند و خط هرگز دور نمی‌رود.
    fa_words = [
        w
        for w in re.findall(r"[\u0600-\u06FF\u200c]+", s)
        if len(w) > 1 and not re.fullmatch(r"[۰-۹]+", w)
    ]
    # خطی که هیچ فارسی ندارد، نام محصول و نسخه است؛ متای آپدیت افزونه
    # همین است و باید بماند. سخت‌گیری فقط وقتی است که فارسی دارد و آن
    # فارسی چیزِ معناداری نمی‌گوید.
    if not fa_words:
        return False
    return not [w for w in fa_words if w not in META_STOP]


def fix_newlines(text):
    """مدل گاهی جای خط تازه، دو حرف بک‌اسلش و n می‌فرستد.

    یک بار کل کتیشن یک خط شد و بین جمله‌ها «\n» چاپ شد.
    """
    return str(text or "").replace("\\r", "").replace("\\n", "\n")


# خط دعوت به اقدام. کوتاه و مستقیم، بعدش خود آدرس می‌آید.
ACTION_LINE = {
    "ابزار": "همین حالا بازش کن و امتحان کن:",
    "فرصت": "همین حالا با ایمیلت ثبت‌نام کن و بگیر:",
    "پلاگین تازه": "همین حالا نصبش کن:",
    "آموزش": "راهنمای کامل از این آدرس:",
    "نمایشگاه": "دیدن سایت از این آدرس:",
    "آپدیت": "همین حالا به‌روزرسانی کن:",
    "خبر": "جزئیات بیشتر از این آدرس:",
    "هشدار امنیتی": "همین حالا وصله کن:",
    "فوری": "همین حالا وصله کن:",
}


def url_for_href(url):
    """آدرس بدون پیشوند در تلگرام لینک نمی‌شود.

    نمایش آدرس دست‌نخورده می‌ماند، فقط چیزی که داخل href می‌رود
    کامل می‌شود تا تلگرام پیام را رد نکند.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        return "https://" + u
    return u.replace('"', "%22")


# نام‌های آشنایی که در تیتر فارسی هم می‌آیند. اگر شکل فارسی‌اش در تیتر
# باشد، چسباندن نام لاتین جلوی تیتر فقط تکرار است: «WordPress — وردپرس».
FA_NAME = {
    "wordpress": "وردپرس",
    "woocommerce": "ووکامرس",
    "elementor": "المنتور",
    "gutenberg": "گوتنبرگ",
    "php": "پی‌اچ‌پی",
    "mysql": "مای‌اس‌کیو‌ال",
    "seo": "سئو",
}


def publish_cfg(s=None):
    return ((s or settings()).get("publish") or {})


def in_publish_window(s=None, dt=None):
    """آیا الان وقت انتشار است؟

    کانال برای مخاطب ایرانی است. پست ساعت سه بامداد دیده نمی‌شود؛ فقط
    صف را شلوغ می‌کند و صبح زیر ده پست دیگر گم می‌شود.
    """
    cfg = publish_cfg(s)
    if not cfg.get("enabled", True):
        return True
    moment = (dt or now()).astimezone(TZ)
    start = str(cfg.get("start", "08:00"))
    end = str(cfg.get("end", "23:30"))
    return start <= moment.strftime("%H:%M") <= end


def next_window_start(s=None, dt=None):
    """کی در باز می‌شود؟ برای گفتن به کاربر در گزارش."""
    cfg = publish_cfg(s)
    moment = (dt or now()).astimezone(TZ)
    start = str(cfg.get("start", "08:00"))
    try:
        hh, mm = [int(x) for x in start.split(":")]
    except Exception:
        hh, mm = 8, 0
    target = moment.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= moment:
        target = target + timedelta(days=1)
    return target


def hold_ready(it, label, text, score, kind):
    """پست آماده را نگه می‌دارد تا در پنجرهٔ انتشار برود.

    نوشتن متن کار سنگین است و به مدل و وقت نیاز دارد؛ انتشار کار سبک.
    جدا کردنشان یعنی مدل شب کار می‌کند و کانال صبح پر می‌شود.
    """
    cfg = publish_cfg()
    store = read_json(READY, {"items": []})
    items = store.get("items") or []
    if len(items) >= int(cfg.get("max_ready", 8)):
        return False
    if any(x.get("id") == it.get("id") for x in items):
        it["held"] = True
        return True
    items.append(
        {
            "id": it.get("id"),
            "at": now().isoformat(timespec="seconds"),
            "kind": kind,
            "label": label,
            "text": text,
            "score": score,
            "title": it.get("title", ""),
            "link": it.get("link", ""),
            "source": it.get("source", ""),
            "group": it.get("group", ""),
            "photo": og_image(it["link"]) if kind == "showcase" else "",
        }
    )
    store["items"] = items
    if not DRY:
        write_json(READY, store)
    it["held"] = True
    return True


def publish_ready(s, st, seen, hard_deadline=None):
    """یکی از پست‌های آماده را در پنجرهٔ انتشار می‌فرستد.

    هر اجرا حداکثر یک پست، با فاصلهٔ مشخص. صبح که کانال باز می‌شود
    قوی‌ترین پست اول می‌رود، نه قدیمی‌ترین.
    """
    cfg = publish_cfg(s)
    if not cfg.get("enabled", True) or not in_publish_window(s):
        return 0
    store = read_json(READY, {"items": []})
    items = store.get("items") or []
    if not items:
        return 0
    sch = read_json(SCHED, {})
    gap = int(cfg.get("gap_min", 70))
    last = sch.get("last_publish")
    if last:
        try:
            if (now() - datetime.fromisoformat(last)).total_seconds() < gap * 60:
                return 0
        except Exception:
            pass
    d = read_json(DAILY, {"date": today(), "count": 0})
    if d.get("date") != today():
        d = {"date": today(), "count": 0}
    d.setdefault("kinds", {})
    store_story = load_stories()
    items.sort(key=lambda x: (float(x.get("score") or 0), str(x.get("at") or "")), reverse=True)
    keep = []
    done = False
    for row in items:
        if done:
            keep.append(row)
            continue
        kind = row.get("kind") or "news"
        if daily_room(s, kind, d) <= 0:
            keep.append(row)
            continue
        held_h = 99.0
        try:
            held_h = (now() - datetime.fromisoformat(str(row.get("at")))).total_seconds() / 3600
        except Exception:
            pass
        if held_h > float(cfg.get("max_hold_h", 30)):
            log("  held post too old, dropped:", str(row.get("title") or "")[:40])
            continue
        # دو نسخه از یک داستان در صف نگه‌داری ممکن است بنشیند؛ دومی این‌جا
        # می‌افتد. رکورد خودِ همین پست نادیده گرفته می‌شود.
        if is_recent_story(store_story, row, ignore_id=row.get("id")):
            log("  duplicate story in hold, dropped:", str(row.get("title") or "")[:40])
            continue
        try:
            if row.get("photo"):
                res = send_photo(row["photo"], row["text"])
            else:
                res = send(row["text"])
        except Exception as exc:
            log("  publish error:", str(exc)[:120])
            keep.append(row)
            break
        mid = ((res or {}).get("result") or {}).get("message_id")
        if mid:
            row["msg_id"] = mid
        q = read_json(QUEUE, {"v": 1, "items": []})
        target = next((x for x in q["items"] if x.get("id") == row.get("id")), None)
        if target:
            target["posted"] = True
            target.pop("held", None)
            if row.get("msg_id"):
                target["msg_id"] = row["msg_id"]
        seen["ids"][row.get("id")] = today()
        add_story(store_story, target or row)
        d["count"] = int(d.get("count", 0)) + 1
        d["kinds"][kind] = int(d["kinds"].get(kind, 0)) + 1
        if (row.get("group") or (target or {}).get("group") or "") == "ai":
            d["kinds"]["ai"] = int(d["kinds"].get("ai", 0)) + 1
        archive_row(target or row, row.get("label", ""), "", "", row.get("score", 0), kind)
        if not DRY:
            write_json(QUEUE, q)
            write_json(DAILY, d)
            write_json(SEEN, seen)
            write_json(STORIES, store_story)
        fresh = read_json(SCHED, {})
        fresh["last_publish"] = now().isoformat(timespec="seconds")
        if not DRY:
            write_json(SCHED, fresh)
        log("published from hold:", kind, str(row.get("title") or "")[:40])
        done = True
    if not DRY:
        write_json(READY, {"items": keep})
    return 1 if done else 0


def action_links_on():
    """خط «از این آدرس شروع کن» پیش‌فرض خاموش است.

    تجربهٔ این پروژه: این خط بیشتر از آن که کمک کند، خراب شد. یک بار به
    صفحهٔ گزارش تخلف وردپرس رسید، یک بار به مخزن گیت‌هاب محصول دیگری،
    یک بار به صفحهٔ افزونه‌ای که ربطی به خبر نداشت. اعتبار کانال با یک
    لینک غلط می‌رود و با صد پست درست برنمی‌گردد. پست بدون این خط هم
    کامل است، چون خط «منبع» بالاخره به خودِ مطلب می‌رسد.
    """
    return bool(settings().get("action_links", False))


def fa_digits_text(text):
    """ارقام لاتین را فارسی می‌کند، ولی به توکن لاتین دست نمی‌زند.

    «7.1.4» و «100,000» فارسی می‌شوند؛ «GPT-4o» و «v2.3» دست نمی‌خورند،
    چون رقم داخل یک توکن لاتین است و فارسی کردنش اسم را خراب می‌کند.
    """
    def one(token):
        if re.search(r"[A-Za-z]", token):
            return token
        return token.translate(FA_DIGITS)

    return re.sub(r"\S+", lambda m: one(m.group(0)), str(text or ""))


# برچسبی که مدل در تیتر تکرار می‌کند. ترکیب‌ها اول می‌آیند تا «آپدیت
# امنیتی» یکجا برداشته شود، نه اینکه فقط «آپدیت» برود و «امنیتی» در
# تیتر بماند. یک بار تیتر «آپدیت امنیتی المنتور» به «امنیتی المنتور»
# تبدیل شد.
LEAD_COMPOUND = (
    "هشدار امنیتی", "آپدیت امنیتی", "به‌روزرسانی امنیتی", "اصلاحیهٔ امنیتی",
    "پلاگین تازه", "افزونهٔ تازه", "خبر فوری", "آپدیت فوری", "هشدار فوری",
)
LEAD_SINGLE = ("هشدار", "آپدیت", "به‌روزرسانی", "خبر", "ابزار", "آموزش", "فرصت", "نمایشگاه", "فوری")
# اگر بعد از برچسب یکی از این کلمه‌ها بیاید، برچسب بخشی از معناست نه تکرار
# آن: «ابزار جدید گوگل» با حذف «ابزار» می‌شود «جدید گوگل» و بی‌معنا.
LABEL_STOP = ("جدید", "تازه", "بهتر", "رایگان", "بزرگ", "امنیتی", "فوری", "رسمی", "مهم")


def strip_lead_label(title, label):
    """برچسبی که مدل در ابتدای تیتر گذاشته را برمی‌دارد، بدون شکستن جمله."""
    title = (title or "").strip()
    if not title:
        return title
    for cand in tuple(LEAD_COMPOUND) + ((label or ""),) + LEAD_SINGLE:
        cand = (cand or "").strip()
        if not cand or not title.startswith(cand):
            continue
        rest = title[len(cand):]
        if not rest.strip():
            return ""
        if rest[0] in ":،-–—":
            return rest.lstrip(" :،-–—").strip()
        if rest[0] != " ":
            continue
        if " " in cand:
            return rest.strip()
        nxt = rest.split()[0]
        if nxt in LABEL_STOP:
            continue
        return rest.strip()
    return title


def format_post(label, title, summary, details, meta, link, hashtags, when=None, name="", use_link=""):
    """ساخت متن پست.

    ترتیب ثابت است: برچسب و تیتر، خلاصهٔ دو سه خطی، نقل‌قول با توضیح
    اضافه، خط اقدام با خود آدرس، و آخر منبع که روی کلمه‌اش لینک است.
    """
    tag = emoji_for(label)
    # ارقام همه‌جا فارسی می‌شوند، از یک جا. اگر در خود کد هم عوض شود،
    # دو منبع حقیقت می‌شود و یکی جا می‌ماند.
    title = fa_digits_text((title or "").strip())
    summary = fa_digits_text(summary)
    details = fa_digits_text(details)
    meta = fa_digits_text(meta)
    # اگر مدل نام لاتین را در تیتر نگذاشت و تیتر هیچ لاتینی ندارد،
    # نام تأییدشده را جلوی تیتر می‌گذاریم. مخاطب باید بداند از چه
    # چیزی حرف می‌زنیم. مگر اینکه شکل فارسی نام از قبل در تیتر باشد.
    if title and name and not re.search(r"[A-Za-z]", title):
        fa = FA_NAME.get(name.lower())
        if not (fa and fa in title):
            title = "{} — {}".format(name, title)
    if title:
        # برچسب را در تیتر تکرار نکن. یک بار شد «ابزار: ابزار HOL Guard».
        title = strip_lead_label(title, label)
        # اگر خود تیتر دونقطه داشته باشد، خط دو دونقطه می‌گیرد:
        # «ابزار: HOL Guard: آنتی‌ویروس...». اولی می‌ماند، دومی خط تیره.
        if ":" in title:
            title = re.sub(r"\s*(?<!\d):(?!\d)\s*", " — ", title, count=1)
        head = "{} {}: {}".format(tag, label, title).strip()
    else:
        head = "{} {}".format(tag, label).strip()
    parts = ["<b>{}</b>".format(esc(bidi(head))), "", esc(bidi(fix_newlines(summary)))]
    inner = []
    if details:
        inner.append(esc(bidi(trim_details(details))))
    # متا جای داده است، نه جای صفت. یک بار مدل «open source» را در متا
    # گذاشت و آخر کادر یک خط تنها و بی‌مصرف افتاد، در حالی که همان
    # جمله در خلاصه هم بود. متایی که عدد یا قیمت ندارد، داده نیست.
    # متای فقط-نسخه، وقتی تیتر خودش همان نسخه را گفته، تکرار است و
    # آخر کادر یک خط تنها و بی‌مصرف می‌سازد.
    if meta and version_only_line(meta):
        digits = re.findall(r"[0-9]+", meta.translate(DIGITS_TO_LATIN))
        if digits and all(d in title.translate(DIGITS_TO_LATIN) for d in digits):
            meta = ""
    meta_ok = bool(meta) and bool(re.search(r"[\d۰-۹]", meta) or len(meta) > 30)
    # عدد داشتن کافی نیست. «PageSpeed ۷۰ به ۹۹» هم عدد دارد، ولی نمی‌گوید
    # کدام سایت و چرا. خطی که فقط یک عدد و یک اسم لاتین است دور می‌رود.
    if meta_ok and meta_fragment(meta):
        meta_ok = False
    if meta_ok:
        inner.append(esc(bidi(meta)))
    if inner:
        parts.append("")
        parts.append("<blockquote expandable>{}</blockquote>".format("\n".join(inner)))
    # لینک اقدام: خود آدرس دیده می‌شود، نه یک کلمهٔ مبهم.
    if use_link and use_link != link:
        parts.append("")
        parts.append(esc(ACTION_LINE.get(label, "از این آدرس شروع کن:")))
        parts.append('<a href="{}">{}</a>'.format(url_for_href(use_link), esc(use_link)))
    # منبع: لینک روی کلمه می‌نشیند، نه روی آدرس.
    if link:
        parts.append("")
        parts.append(
            'اطلاعات بیشتر دربارهٔ این موضوع را از <a href="{}">منبع</a> بخوانید.'.format(
                url_for_href(link)
            )
        )
    if when is not None:
        parts.append("")
        parts.append("<i>{}</i>".format(esc(stamp(when))))
    if hashtags:
        parts.append("")
        parts.append(hashtags)
    return "\n".join(parts)


# ---------------------------------------------------------------- security

SEC_LABEL = "هشدار امنیتی"

ADV_HIT = re.compile(
    r"(vulnerab\w*|CVE-\d{4}-\d+|security release|\bpatched\b|\bexploit\w*|"
    r"\bXSS\b|\bRCE\b|\bCSRF\b|\bSSRF\b|\bLFI\b|\bRFI\b|\bSQLi\b|SQL injection|"
    r"\bbackdoor\b|privilege escalation|"
    # فارسی هم لازم است: متن پست را مدل می‌نویسد و کلمهٔ انگلیسی منبع
    # همیشه در همان جمله نمی‌ماند.
    r"آسیب[\s\u200c]?پذیری|حفرهٔ? امنیتی|مشکل امنیتی|اصلاحیهٔ? امنیتی|وصلهٔ? امنیتی|"
    # مدل معمولاً نمی‌نویسد «اصلاحیهٔ امنیتی»؛ می‌نویسد «اصلاح امنیتی».
    # یک بار آپدیت امنیتی المنتور با برچسب «آپدیت» پست شد و در صف
    # امنیتی نیفتاد.
    r"اصلاح امنیتی|آپدیت امنیتی|به[\s\u200c]?روزرسانی امنیتی|باگ امنیتی|"
    r"مسئلهٔ? امنیتی)",
    re.I,
)
ADV_SKIP = re.compile(r"(bug bounty|vulnerability report|weekly report|monthly report|podcast|webinar)", re.I)


def is_advisory(text):
    return bool(ADV_HIT.search(text or "")) and not bool(ADV_SKIP.search(text or ""))


def security_run(s, st, seen, hard_deadline=None):
    sources = load_sources("security")
    # هشدار امنیتی کهنه، هشدار نیست. کاربر باید همین امروز کاری بکند.
    max_age = float(s.get("security_max_age_h", 24))
    fresh = []
    stale = 0
    started = time.time()
    fetch_budget = 90.0
    if hard_deadline:
        fetch_budget = max(20.0, min(90.0, (float(hard_deadline) - started) / 2.0))
    for idx, src in enumerate(sources):
        if time.time() - started > fetch_budget:
            log("security: fetch budget reached, {} sources skipped".format(len(sources) - idx))
            break
        must = src.get("must") or ""
        for it in fetch_source(src, st):
            if must:
                hay = ((it.get("title") or "") + " " + (it.get("desc") or "")).lower()
                if must not in hay:
                    continue
            iid = item_id(it["link"])
            if iid in seen["ids"]:
                continue
            age = 99
            if it["published"]:
                age = (now() - it["published"]).total_seconds() / 3600
            if age > max_age:
                stale += 1
                continue
            if not is_advisory(it["title"] + " " + it["desc"]):
                continue
            fresh.append(
                {
                    **it,
                    "id": iid,
                    "source": src["name"],
                    "lang": src["lang"],
                    "tier": src["tier"],
                    "age_hours": age,
                }
            )
    if stale:
        log("security: {} کهنه (بیشتر از {} ساعت) کنار گذاشته شد".format(stale, int(max_age)))

    fresh.sort(key=lambda x: x["age_hours"])
    fresh = dedupe_stories(fresh)
    batch = fresh[:10]
    if not batch:
        log("security: nothing new")
        return 0

    results = {}
    try:
        res = call_models(
            SYSTEM_PROMPT,
            build_prompt(
                batch,
                "این‌ها هشدار امنیتی تازه‌اند. برچسب همه «هشدار امنیتی» باشد. "
                "در خلاصه بگو چه چیزی آسیب‌پذیر است و کاربر چه اقدامی باید بکند.",
            ),
            deadline=hard_deadline,
        )
        for entry in res.get("items", []) if isinstance(res, dict) else []:
            results[str(entry.get("id", ""))] = entry
    except Exception as exc:
        log("model unavailable, template mode:", str(exc)[:120])

    posted = 0
    store = load_stories()
    d = read_json(DAILY, {"date": today(), "count": 0})
    if d.get("date") != today():
        d = {"date": today(), "count": 0}
    d.setdefault("kinds", {})
    my_limit = daily_room(s, "security", d)
    if my_limit <= 0:
        log("security: no room left today")
        return 0
    posted_sec = int(d["kinds"].get("security", 0))
    for it in batch:
        if posted >= my_limit:
            log("security: share for today is used up")
            break
        if is_recent_story(store, it):
            log("  already covered:", it["title"][:60])
            continue
        if not link_alive(it["link"]):
            log("  dead link skipped:", it["link"])
            continue
        entry = results.get(it["id"])
        # «بحرانی» به‌تنهایی فوری نیست. هر آسیب‌پذیری بحرانی، هشدار است نه
        # خبر فوری؛ فوری یعنی همین حالا در حال سوءاستفاده است. و هشداری
        # که اصل ماجرایش چند روز پیش منتشر شده، امروز فوری نیست: یک CVE
        # که گوگل بیست و دوم اکتبر اعلام کرده بود، دوم نوامبر «فوری»
        # فرستاده شد و خواننده فکر کرد همین امروز کشف شده.
        urgent = bool(
            re.search(
                r"(actively exploited|actively attempt|attempting to exploit|already exploiting|"
                r"exploited in the wild|under attack|zero-day|در حال سوءاستفاده|بهره‌برداری فعال)",
                it.get("desc", "") + " " + it.get("title", ""),
                re.I,
            )
        ) and float(it.get("age_hours") or 0) <= float(s.get("urgent_max_age_h", 72))
        sec_text = " ".join(
            [
                it.get("title", "") or "",
                it.get("desc", "") or "",
                (entry or {}).get("title", "") or "",
                (entry or {}).get("summary", "") or "",
            ]
        )
        if entry:
            label = classify_label("security", sec_text, entry.get("label", ""), urgent)
            title = entry.get("title") or it["title"]
            summary = entry.get("summary") or ""
            details = clean_details(entry.get("details") or "")
            meta = entry.get("meta") or ""
        else:
            label = classify_label("security", sec_text, "", urgent)
            title = "هشدار تازه از {}".format(it["source"])
            summary = "منبع امنیتی تازه‌ای منتشر کرده است. متن اصلی داخل کادر آمده."
            details = it["title"] + ("\n" + it["desc"][:400] if it["desc"] else "")
            meta = "منبع: " + it["source"] + " | ترجمه نشده، مدل در دسترس نبود"
        # سن هشدار صریح گفته می‌شود تا خواننده بداند چقدر تازه است.
        age_h = float(it.get("age_hours") or 0)
        if age_h >= 6:
            meta = (meta + "\n" if meta else "") + "منتشر شده " + rel_age(age_h)
        tags = "#فوری #هشدار_امنیتی #امنیت" if urgent else "#هشدار_امنیتی #امنیت"
        text = format_post(label, title, summary, details, meta, it["link"], tags, it.get("published"))
        try:
            send(text)
        except Exception as exc:
            log("  telegram error:", exc)
            break
        seen["ids"][it["id"]] = today()
        add_story(store, it)
        d["count"] = int(d.get("count", 0)) + 1
        posted_sec += 1
        d["kinds"]["security"] = posted_sec
        # هر پست همان لحظه ثبت می‌شود. اگر اجرا وسط کار قطع شود،
        # پست بعدی همین خبر را دوباره نمی‌فرستد.
        if not DRY:
            write_json(SEEN, seen)
            write_json(STORIES, store)
            write_json(DAILY, d)
        archive_row(it, label, title, summary, entry.get("score", 100) if entry else 100, "security")
        posted += 1
        time.sleep(2)

    if not DRY:
        write_json(STORIES, store)
    log("security posted:", posted)
    return posted


# ---------------------------------------------------------------- main flows

def queue_kind_count(kind):
    """چند آیتم از این دسته همین حالا در صف است."""
    q = read_json(QUEUE, {"v": 1, "items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    return sum(1 for x in items if (x.get("kind") or "") == kind)


def added_today(kind):
    """چند آیتم از این دسته امروز وارد صف شده."""
    q = read_json(QUEUE, {"v": 1, "items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    today = now().date().isoformat()
    return sum(
        1 for x in items if (x.get("kind") or "") == kind and str(x.get("added") or "")[:10] == today
    )


def trim_intake(kind, rows, s):
    """سقف ورود به صف: هم روزانه، هم انبار.

    بدون این، فید ابزار هر بار چهل آیتم می‌آورد و دستهٔ ابزار با سقف
    یک پست در روز، صد و پنجاه روز صف می‌سازد. سقف انبار جلوی همین را
    می‌گیرد: تا وقتی هشت ابزار در صف است، ابزار تازه وارد نمی‌شود.
    """
    if not rows:
        return rows
    backlog = int((s.get("backlog_cap") or {}).get(kind, 0) or 0)
    daily = int((s.get("intake_per_day") or {}).get(kind, 0) or 0)
    room = len(rows)
    if backlog > 0:
        room = min(room, max(0, backlog - queue_kind_count(kind)))
    if daily > 0:
        room = min(room, max(0, daily - added_today(kind)))
    if room >= len(rows):
        return rows
    ordered = sorted(rows, key=lambda r: r.get("published") or "", reverse=True)
    # تازه‌ترین اول، ولی موضوع مقدم بر تازه‌بودن است. سقف ورود نباید
    # جای خبر وردپرسی را به خبر عمومی هوش مصنوعی بدهد.
    ordered = wp_first_sort(ordered)
    kept = ordered[:room]
    log(
        "intake {}: {} dropped, backlog {}/{} daily {}/{} wp {}/{}".format(
            kind,
            len(rows) - len(kept),
            queue_kind_count(kind),
            backlog or "-",
            added_today(kind),
            daily or "-",
            sum(1 for x in kept if wp_first(x) == WP_RANK_WP),
            len(kept),
        )
    )
    return kept


def prune_queue(s=None, apply=True):
    """صف را به سقف‌ها برمی‌گرداند: ابزار و نمایشگاه تا سقف، خبر کهنه بیرون.

    این کار عمداً داخل خود موتور است، نه یک اسکریپت بیرونی. پاک کردن
    دستی صف یک بار همزمان با ذخیرهٔ اجرا شد، وضعیت عقب افتاد و همان
    دو خبر دوباره پست شدند. حالا خود موتور روزی یک بار تمیزش می‌کند.
    """
    s = s or settings()
    caps = s.get("backlog_cap") or {}
    keep_h = float(s.get("news_keep_h", 72))
    q = read_json(QUEUE, {"v": 1, "items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    before = len(items)
    # آیتمی که پست شده دیگر در صف نمی‌ماند. ماندنش یعنی خطر پست دوباره.
    seen_ids = read_json(SEEN, {"v": 1, "ids": {}}).get("ids") or {}
    if seen_ids:
        items = [x for x in items if x.get("id") not in seen_ids]
    kept = []

    for kind in ("tools", "showcase"):
        group = [x for x in items if (x.get("kind") or "") == kind]
        cap = int(caps.get(kind, 0) or 0)
        if cap <= 0:
            kept.extend(group)
            continue
        group.sort(key=lambda x: (x.get("published") or x.get("added") or ""), reverse=True)
        group = wp_first_sort(group)
        kept.extend(group[:cap])

    for x in items:
        kind = x.get("kind") or ""
        if kind in ("tools", "showcase"):
            continue
        if x.get("plugin_update"):
            kept.append(x)
            continue
        try:
            age = (now() - datetime.fromisoformat(str(x.get("published") or ""))).total_seconds() / 3600
        except Exception:
            age = 0.0
        if age <= keep_h:
            kept.append(x)

    kept.sort(key=lambda x: (x.get("published") or x.get("added") or ""), reverse=True)
    if apply and len(kept) != before and not DRY:
        write_json(QUEUE, {"v": 1, "items": kept})
    return before, len(kept)


def ai_intake_room():
    """چند خبر هوش مصنوعی دیگر امروز اجازهٔ ورود به صف دارد.

    سقف انتشار (ai_cap_per_day) فقط جلوی پست شدن را می‌گرفت، نه جلوی
    وارد شدن. نتیجه این شد که صف هشتاد و سه درصد هوش مصنوعی شد: سه فید
    AI روزی پنجاه خبر می‌دادند و فیدهای وردپرس روزی یکی دو تا. مدل هم
    بیشتر وقتش را صرف رد کردن همان‌ها می‌کرد.
    """
    cap = int(settings().get("ai_intake_per_day", 10))
    if cap <= 0:
        return 0
    q = read_json(QUEUE, {"items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    today = now().date().isoformat()
    used = sum(
        1
        for x in items
        if (x.get("group") or "") == "ai" and str(x.get("added") or "")[:10] == today
    )
    return max(0, cap - used)


def collect(kind, st, seen, budget_sec=None, watch=True):
    """منابع را می‌خواند و آیتم تازه جمع می‌کند.

    یک منبع کند نباید کل اجرا را بخورد. اگر وقت تمام شد، بقیهٔ منابع
    رد می‌شوند و اجرای بعدی می‌خواندشان. ترتیب فایل منابع یعنی منابع
    رسمی اول خوانده می‌شوند و آن‌ها هرگز قربانی نمی‌شوند.
    """
    sources = load_sources(kind)
    rows = []
    blocked = load_blocked()
    started = time.time()
    budget = float(budget_sec or 180)
    # سد سن، همان‌جا که آیتم وارد می‌شود. کار مدل نیست که خبر کهنه را
    # رد کند؛ اگر کهنه وارد صف شود، فقط جای خبر تازه را می‌گیرد.
    gate = max_age_for(kind)
    stale = 0
    for idx, src in enumerate(sources):
        if time.time() - started > budget:
            log("collect: budget reached, {} of {} sources skipped".format(len(sources) - idx, len(sources)))
            break
        must = src.get("must") or ""
        for it in fetch_source(src, st):
            if must:
                hay = ((it.get("title") or "") + " " + (it.get("desc") or "")).lower()
                if must not in hay:
                    continue
            iid = item_id(it["link"])
            if iid in seen["ids"]:
                continue
            hit = blocked_hit(
                {"title": it["title"], "desc": it["desc"], "link": it["link"], "source": src["name"]},
                blocked,
            )
            if hit:
                log("  blocked (رقیب):", hit, it["title"][:40])
                continue
            if kind != "security" and persian_product(it):
                log("  blocked (کالای فارسی):", it["title"][:40])
                continue
            # فیدی که تاریخ نمی‌دهد تازه حساب می‌شود، نه کهنه. با عدد
            # بزرگ، کل آیتم‌های چنین فیدی بی‌صدا حذف می‌شد.
            age = 0.0
            if it["published"]:
                age = (now() - it["published"]).total_seconds() / 3600
            if age > gate:
                stale += 1
                continue
            rows.append(
                {
                    "id": iid,
                    "title": it["title"],
                    "link": it["link"],
                    "desc": it["desc"],
                    "source": src["name"],
                    "lang": src["lang"],
                    "tier": src["tier"],
                    "group": src.get("group", ""),
                    "kind": kind,
                    "age_hours": round(age, 1),
                    "published": it["published"].isoformat() if it["published"] else "",
                    "added": now().isoformat(timespec="seconds"),
                }
            )
    if stale:
        log("collect {}: {} old item(s) dropped, gate {}h".format(kind, stale, int(gate)))
    # سقف ورودی گروه هوش مصنوعی. بدون این، صف از خبر AI پر می‌شود و
    # ستون اصلی کانال، یعنی وردپرس، جایی برای آمدن ندارد.
    if any((r.get("group") or "") == "ai" for r in rows):
        room = ai_intake_room()
        kept, dropped = [], 0
        for r in rows:
            if (r.get("group") or "") == "ai":
                if room <= 0:
                    dropped += 1
                    continue
                room -= 1
            kept.append(r)
        if dropped:
            log("ai intake: {} dropped, daily cap reached".format(dropped))
        rows = kept
    # سقف ورود. پیش از رصد نسخه، چون آپدیت افزونه آیتم کمیاب است و
    # نباید قربانی سقف انبار شود.
    rows = trim_intake(kind, rows, settings())
    # نسخهٔ تازهٔ افزونه‌های پرنصب. این تنها منبعی است که خبر آپدیت را
    # همان روزِ انتشار می‌آورد؛ فیدهای خودشان دیر یا هرگز نمی‌آورند.
    # همه در دستهٔ خبر می‌آیند و برچسب امنیتی را خود متن تعیین می‌کند.
    if watch and kind == "news":
        try:
            extra_rows = plugin_watch(kind, seen)
        except Exception as exc:
            log("plugin watch failed:", str(exc)[:80])
            extra_rows = []
        if extra_rows:
            log("plugin watch:", len(extra_rows), "new version(s)")
            rows.extend(extra_rows)
    return rows


def enrich_plugins(rows):
    for it in rows:
        slug = plugin_slug(it["link"])
        if slug:
            info = plugin_info(slug)
            if info:
                it["plugin"] = info


def queue_merge(rows, s):
    q = read_json(QUEUE, {"v": 1, "items": []})
    have = {x["id"] for x in q["items"]}
    added = 0
    for r in rows:
        if r["id"] in have:
            continue
        q["items"].append(r)
        have.add(r["id"])
        added += 1
    # دستهٔ آیتم‌های قدیمی با فهرست فعلی منابع هم‌تراز می‌شود. وقتی فیدی
    # از «ابزار» به «خبر» می‌رود، آیتم‌های همان فید هم باید بروند؛ وگرنه
    # یک خبر تا چند روز در دستهٔ ابزار می‌ماند و آن‌جا رد می‌شود.
    kind_of = {}
    for kind in ("news", "tools", "offers", "security", "showcase"):
        try:
            for src in load_sources(kind):
                kind_of[src["name"]] = kind
        except Exception:
            pass
    moved = 0
    for x in q["items"]:
        want = kind_of.get(x.get("source"))
        if want and x.get("kind") != want:
            x["kind"] = want
            moved += 1
    if moved:
        log("queue: {} item moved to its feed's current category".format(moved))
    cutoff = now() - timedelta(days=int(s.get("queue_days", 7)))
    kept = []
    for x in q["items"]:
        try:
            ts = datetime.fromisoformat(x["added"])
        except Exception:
            ts = now()
        if ts >= cutoff:
            kept.append(x)
    q["items"] = kept
    # سد سن. آیتمی که از این سن گذشته هرگز پست نمی‌شود؛ ماندنش در صف
    # فقط جای آیتم تازه را می‌گیرد و آمار صف را دروغ می‌کند.
    gates = {k: max_age_for(k, s) for k in ("news", "tools", "offers", "showcase", "security")}
    kept = []
    stale = 0
    for x in q["items"]:
        k = x.get("kind") or "news"
        # آپدیت افزونه آیتم کمیاب است و در پاک‌سازی هم مستثنی می‌شود.
        if x.get("plugin_update"):
            kept.append(x)
            continue
        if queue_age(x) > gates.get(k, 48.0):
            stale += 1
            continue
        kept.append(x)
    if stale:
        q["items"] = kept
        log("queue: {} old item(s) dropped by the age gate".format(stale))
    # سقف انبار. هر دسته بیشتر از این در صف نمی‌ماند و قدیمی‌ترها می‌روند.
    # سقف در لحظهٔ ورود هم هست، ولی این یکی تضمین است: حتی اگر یک اجرا
    # چهل آیتم بیاورد، صف از سقف رد نمی‌شود.
    caps = s.get("backlog_cap") or {}
    trimmed = 0
    for kind, cap in caps.items():
        cap = int(cap or 0)
        if cap <= 0:
            continue
        group = [x for x in kept if (x.get("kind") or "") == kind]
        if len(group) <= cap:
            continue
        group.sort(key=lambda x: (x.get("published") or x.get("added") or ""), reverse=True)
        # وقتی سقف انبار پر است، آن‌چه بیرون می‌رود باید خبر عمومی باشد،
        # نه خبر وردپرسی. اولویت موضوع بر تازگی مقدم است.
        group = wp_first_sort(group)
        drop = {id(x) for x in group[cap:]}
        kept = [x for x in kept if id(x) not in drop]
        trimmed += len(drop)
    if trimmed:
        q["items"] = kept
        log("queue: {} old item(s) dropped by the backlog cap".format(trimmed))
    if not DRY:
        write_json(QUEUE, q)
    return q, added


KIND_TAG = {
    "news": "#وردپرس",
    "tools": "#هوش_مصنوعی",
    "offers": "#فرصت_رایگان",
    "showcase": "#طراحی_سایت",
    "security": "#امنیت",
}
KIND_FA = {"news": "خبر", "tools": "ابزار", "offers": "فرصت", "showcase": "نمایشگاه", "security": "امنیت"}

# برچسب دسته را کورکورانه نچسبان. دستهٔ خبر هم خبر وردپرس دارد هم خبر
# هوش مصنوعی؛ یک بار خبر انویدیا با برچسب «وردپرس» رفت. مخاطب وردپرسی
# با دیدن آن برچسب روی خبر انویدیا، به کل کانال شک می‌کند.
WP_TAG_WORDS = (
    "wordpress", "woocommerce", "elementor", "gutenberg", "divi",
    "وردپرس", "ووکامرس", "المنتور", "افزونه", "پلاگین", "قالب",
)


def kind_tag_for(kind, text):
    if kind != "news":
        return KIND_TAG.get(kind, "#وردپرس")
    low = str(text or "").lower()
    return "#وردپرس" if any(w in low for w in WP_TAG_WORDS) else "#هوش_مصنوعی"


def cap_per_source(items, limit=3):
    out, counts = [], {}
    for it in items:
        key = it.get("source", "")
        if counts.get(key, 0) >= limit:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(it)
    return out


def daily_room(s, kind, d):
    """این دسته همین حالا چند پست اجازه دارد.

    سقف روز سقف است. ولی هر دستهٔ دیگری که هنوز به سقف خودش نرسیده،
    یک جا برای خودش نگه می‌دارد تا یک دسته کل بودجهٔ روز را نخورد و
    بقیه تا آخر روز بی‌سهم نمانند.
    """
    cap = int(s.get("daily_cap", 10))
    caps = s.get("caps") or {}
    kinds = d.get("kinds") or {}
    remaining = cap - int(d.get("count", 0))
    if remaining <= 0:
        return 0
    # صفر یعنی این دسته امروز تعطیل است. `or` صفر را «نبود» حساب
    # می‌کرد و دستهٔ خاموش‌شده با سقف کل روز برمی‌گشت.
    kind_cap = int(caps[kind]) if kind in caps else int(cap)
    posted_kind = int(kinds.get(kind, 0))
    starving = 0
    for other, value in caps.items():
        if other == kind:
            continue
        if int(kinds.get(other, 0)) < int(value or 0):
            starving += 1
    room = remaining - starving
    if room < 1:
        room = 1
    return max(0, min(kind_cap - posted_kind, room))


def fetch_full_parallel(chunk, workers=6):
    """متن کامل همهٔ آیتم‌های یک chunk را هم‌زمان می‌آورد.

    قبلاً یکی‌یکی خوانده می‌شد: ده آیتم در بدترین حالت صد و پنجاه ثانیه.
    همان چیزی که بودجهٔ اجرا را می‌خورد و به مدل نمی‌رسید.
    """
    todo = [
        it
        for it in chunk
        if not it.get("full") or CHROME_RE.search((it.get("full") or "")[:400])
    ]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        futs = {pool.submit(fetch_article, it["link"], 3200, it.get("title", "") or ""): it for it in todo}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                text, use_link = fut.result()
            except Exception:
                text, use_link = "", ""
            # بازخوانی موفق، مقدار قدیمی را باطل می‌کند. قبلاً لینک
            # استفادهٔ کهنه روی مقدار تازه اولویت داشت و پست به همان
            # لینک غلط قدیمی می‌رفت.
            if text:
                it["full"] = text
                it["use_link"] = use_link


def ai_floor_promote(rows, s, d):
    """کف روزانهٔ هوش مصنوعی: حداقل یک پست در روز، حتی با قانون وردپرس اول.

    قانون «وردپرس اول» خبر گروه ai را ته صف می‌برد. با سقف ده آیتم در
    سبد، ممکن بود هیچ آیتم ai به مدل نرسد و سهم هوش مصنوعی برای همیشه
    صفر بماند. این تابع بهترین آیتم گروه ai را بعد از اولین آیتم
    وردپرسی می‌آورد: کانال با وردپرس شروع می‌شود، ولی سهم هوش مصنوعی
    هم دیده می‌شود. اگر امروز سهمش پر شده باشد، دست نمی‌زند.
    """
    floor = int(s.get("ai_floor_per_day", 0) or 0)
    if floor <= 0 or not rows:
        return rows
    done = int((d.get("kinds") or {}).get("ai", 0))
    held = sum(
        1
        for r in read_json(READY, {"items": []}).get("items", [])
        if (r.get("group") or "") == "ai"
    )
    if done + held >= floor:
        return rows
    best = None
    for x in rows:
        if (x.get("group") or "") == "ai":
            best = x
            break
    if best is None:
        return rows
    rest = [x for x in rows if x is not best]
    at = 1 if rest and wp_first(rest[0]) == WP_RANK_WP else 0
    log("  ai floor: promoted", (best.get("title") or "")[:50])
    return rest[:at] + [best] + rest[at:]


def model_run(kind, s, st, seen, extra, hard_deadline=None):
    # جمع‌آوری نباید بیشتر از یک‌سوم وقت را ببرد، وگرنه به مدل نمی‌رسیم
    # و همان چیزی می‌شود که یک بار دیدیم: «بودجهٔ مدل تمام شد، صفر ثانیه مانده».
    remaining = (float(hard_deadline) - time.time()) if hard_deadline else float(s.get("run_budget_sec", 600))
    collect_budget = max(45.0, min(150.0, remaining / 4.0))
    rows = collect(kind, st, seen, budget_sec=collect_budget)
    rows = dedupe_stories(rows)
    enrich_plugins(rows)
    log("new items:", len(rows))
    q, added = queue_merge(rows, s)
    pending = [
        x
        for x in q["items"]
        if x.get("kind") == kind
        and not x.get("posted")
        and not x.get("channel_skip")
        and not x.get("held")
    ]
    log("queue for", kind, ":", len(pending))
    if not pending:
        return 0

    pending.sort(key=lambda x: code_score(x, x.get("sources_count", 1)), reverse=True)
    # امتیاز داخل هر رتبه تصمیم می‌گیرد، ولی موضوع اولویت دارد: خبر
    # وردپرسی قبل از خبر عمومی هوش مصنوعی به مدل می‌رود، حتی اگر امتیازش
    # کمتر باشد. مدل فقط چیزی را می‌بیند که به او داده شود.
    pending = wp_first_sort(pending)
    # صورت‌جلسه و گزارش هفتگی خبر نیست. قبل از سوزاندن توکن مدل کنار می‌رود.
    # آدرس هم بررسی می‌شود، چون تیتر ممکن است ترجمه شده باشد و کلمهٔ
    # اصلی را نگه نداشته باشد.
    if kind in ("news", "tools"):
        skipped = 0
        kept = []
        blocked_words = load_blocked()
        for x in pending:
            blob = " ".join([x.get("title", "") or "", x.get("link", "") or ""])
            if NON_NEWS_RE.search(blob) or JUNK_NEWS_RE.search(blob):
                x["channel_skip"] = True
                skipped += 1
                continue
            # فهرست پیوند، ابزار نیست.
            if LIST_REPO_RE.search(blob):
                x["channel_skip"] = True
                skipped += 1
                continue
            # پادکست و وبینار اتفاق تازه‌ای را گزارش نمی‌کنند. بعد از
            # قانون «وردپرس اول» اینها بالای صف می‌نشستند و جای خبر
            # واقعی را می‌گرفتند. فقط نام منبع، تیتر و آدرس بررسی
            # می‌شود، نه متن. مقاله‌ای که حالت خبری دارد و فقط داخلش
            # از یک گفتگو حرف زده، خبر است و نباید دور ریخته شود.
            if kind == "news" and PODCAST_RE.search(blob + " " + (x.get("source") or "")):
                x["channel_skip"] = True
                skipped += 1
                continue
            # دستهٔ ابزار را فیدهای عمومی هم پر می‌کنند. آیتمی که هیچ
            # نشانه‌ای از هوش مصنوعی یا ساخت سایت ندارد، ابزار این کانال
            # نیست. اینجا فقط کنار گذاشته می‌شود، پاک نمی‌شود.
            if kind == "tools" and not BUILDER_TOPIC.search(
                blob + " " + (x.get("desc", "") or "")
            ):
                x["channel_skip"] = True
                skipped += 1
                continue
            # خبر پول، دادگاه، یا سیاست داخلی شرکت‌ها. اگر هیچ نشانی از
            # وردپرس و ساخت سایت نداشته باشد، مخاطب کاری با آن ندارد.
            if kind == "news":
                wide = blob + " " + (x.get("desc", "") or "")
                if OFF_AUDIENCE_RE.search(wide) and not SITE_TOPIC_RE.search(wide):
                    x["channel_skip"] = True
                    skipped += 1
                    continue
            # رقیب هرگز. این سد اینجا هم هست چون آیتم ممکن است از قبل در
            # صف مانده باشد، پیش از آن که فهرست رقیب‌ها را ببندیم.
            if blocked_hit(x, blocked_words) or persian_product(x):
                x["channel_skip"] = True
                skipped += 1
                continue
            kept.append(x)
        if skipped:
            log("  non-news skipped:", skipped)
            pending = kept
        if not pending:
            return 0
    # آیتمی که یک بار پست شده نباید دوباره به مدل برود. یک بار صف عقب
    # افتاد و همان آپدیت لایت‌اسپید دو بار در کانال رفت.
    posted_ids = (seen or {}).get("ids") or {}
    if posted_ids:
        before_n = len(pending)
        pending = [x for x in pending if x.get("id") not in posted_ids]
        if len(pending) < before_n:
            log("  already posted skipped:", before_n - len(pending))
    # سن واقعی، نه سن لحظهٔ جمع‌آوری. آیتمی که سه روز در صف مانده نباید
    # به‌عنوان خبر تازه پست شود، و نباید توکن مدل هم برایش بسوزد.
    max_age_gate = max_age_for(kind, s)
    fresh = [x for x in pending if true_age(x) <= max_age_gate]
    if len(fresh) < len(pending):
        log("  dropped stale:", len(pending) - len(fresh), "of", len(pending))
    if not fresh:
        return 0
    # سقف هر منبع در یک اجرا. دو آموزش از یک وبلاگ پشت سر هم، اسپم است.
    psc = s.get("per_source_cap", 3)
    per_source = int(psc.get(kind, psc.get("default", 3))) if isinstance(psc, dict) else int(psc)
    capped = cap_per_source(fresh, per_source)

    d = read_json(DAILY, {"date": today(), "count": 0})
    if d.get("date") != today():
        d = {"date": today(), "count": 0}

    # سهم هوش مصنوعی هم سقف دارد هم کف. کف باید قبل از برش سبد اعمال
    # شود، وگرنه آیتم گروه ai ته صف می‌ماند و با برش ده‌تایی هیچ‌وقت به
    # چشم مدل نمی‌رسد.
    if kind == "news":
        capped = ai_floor_promote(capped, s, d)

    pool = capped[: int(s.get("max_items_per_call", 25))]

    # سهم هوش مصنوعی سقف دارد. کانال قرار است هشتاد درصد وردپرس و طراحی
    # سایت باشد. بی‌سقف، خبر هوش مصنوعی جذاب‌تر است و کم‌کم همهٔ کانال را
    # می‌خورد. آیتمی که امروز بیرون می‌ماند پاک نمی‌شود؛ فردا شانس دارد.
    if kind == "news":
        ai_cap = int(s.get("ai_cap_per_day", 1))
        ai_done = int((d.get("kinds") or {}).get("ai", 0))
        ai_held = sum(
            1
            for r in read_json(READY, {"items": []}).get("items", [])
            if (r.get("group") or "") == "ai"
        )
        ai_left = max(0, ai_cap - ai_done - ai_held)
        narrowed = []
        used = 0
        for x in pool:
            if (x.get("group") or "") == "ai":
                if used >= ai_left:
                    continue
                used += 1
            narrowed.append(x)
        if len(narrowed) != len(pool):
            log("  ai quota: {} left, held back {}".format(ai_left, len(pool) - len(narrowed)))
        pool = narrowed
        if not pool:
            log("  ai quota full and nothing else to post")
            return 0

    cap = int(s.get("daily_cap", 10))
    _caps = s.get("caps") or {}
    kind_cap = int(_caps[kind]) if kind in _caps else int(cap)
    per_run = int(s.get("max_posts_per_run", 4))
    posted = 0
    store = load_stories()
    failures = 0
    deadline = time.time() + int(s.get("run_budget_sec", 600))
    if hard_deadline:
        deadline = min(deadline, float(hard_deadline))
    d.setdefault("kinds", {})
    posted_kind = int(d["kinds"].get(kind, 0))
    # پست‌هایی که نوشته شده‌اند و در نوبت انتشارند هم سهم روز را
    # می‌خورند؛ وگرنه هر اجرا چهار پست تازه می‌نوشت و انبار پر می‌شد
    # از پست‌هایی که هیچ‌وقت نوبتشان نمی‌شد.
    held_by_kind = {}
    for _row in read_json(READY, {"items": []}).get("items") or []:
        _k = _row.get("kind") or "news"
        held_by_kind[_k] = held_by_kind.get(_k, 0) + 1

    # سهم این دسته از بودجهٔ امروز، با احترام به سهم بقیه.
    my_limit = daily_room(s, kind, d)
    fast_cfg = s.get("fast_lane") or {}
    fast_score = float(fast_cfg.get("score", 85))
    fast_age = float(fast_cfg.get("max_age_h", 3))
    fast_per_day = int(fast_cfg.get("per_day", 2))
    fast_used = int(d.get("fast", 0))
    # مسیر سریع چند جای اضافه دارد و سقف روز را می‌شکند، فقط برای خبر خیلی مهم.
    hard_cap = cap + max(0, fast_per_day - fast_used)
    if int(d["count"]) >= hard_cap:
        log("daily cap reached")
        return 0
    log("room today:", my_limit)
    # اگر سهم امروز این دسته تمام شده و مسیر سریع هم ظرفیت ندارد،
    # تماس مدل هیچ پستی درنمی‌آورد و فقط توکن می‌سوزاند. این حالت
    # روزی چند بار پیش می‌آمد و سهمیهٔ لایه‌های رایگان را می‌خورد.
    if my_limit <= 0 and fast_used >= fast_per_day:
        log("no room left today for", kind, "- model call skipped")
        return 0
    # سهم امروز تمام شده ولی مسیر سریع باز است. آن مسیر فقط برای خبر
    # خیلی تازه است؛ اگر هیچ آیتم تازه‌ای در صف نیست، تماس مدل فقط
    # توکن می‌سوزاند و ته‌اش «سهم این اجرا تمام شد» چاپ می‌شود. یک بار
    # چهار تماس مدل کامل رفت و صفر پست داد.
    if my_limit <= 0:
        fresh_pool = [x for x in pool if float(x.get("age_hours") or 99) <= fast_age]
        if not fresh_pool:
            log("share for", kind, "is used up and nothing is fresh enough for the fast lane")
            return 0
        pool = fresh_pool
    rejected = []

    step = max(3, int(s.get("chunk_size", 7)))
    # اگر اجرای قبلی روی لایهٔ پشتیبان افتاده بود، همهٔ آیتم‌ها را در یک تماس
    # می‌فرستیم تا سهمیهٔ آن لایه کمتر بسوزد.
    primary = (model_providers() or [{}])[0].get("name", "")
    last_prov = read_json(SCHED, {}).get("last_provider") or ""
    # فقط لایه‌های تنگ‌سهمیه صرفه‌جویی می‌کنند. فرستادن ده آیتم در یک تماس
    # سهمیه را نگه می‌دارد ولی کیفیت نوشتن را پایین می‌آورد؛ برای لایه‌ای
    # که سهمیهٔ روزانه‌اش کم نیست، این معامله به صرفه نیست.
    tight = set(s.get("frugal_providers") or ["openrouter", "google"])
    if primary in tight and last_prov and last_prov != primary:
        step = max(step, int(s.get("max_items_per_call", 14)))
        log("frugal mode: single call per run, last provider was", last_prov)
    for start in range(0, len(pool), step):
        chunk = pool[start : start + step]
        if posted >= per_run or int(d["count"]) >= hard_cap:
            break
        # اگر وقتِ یک تماس کامل نیست، شروع نکن. نصفه شروع کردن فقط
        # توکن و وقت را می‌سوزاند و آخرش هم پستی درنمی‌آید.
        if deadline - time.time() < 60:
            log("  out of time before chunk", start // step + 1, "| left", int(deadline - time.time()), "s")
            break
        try:
            fetch_full_parallel(chunk)
            filled = sum(1 for x in chunk if x.get("full"))
            log("  full text: {}/{}".format(filled, len(chunk)))
            result = call_models(SYSTEM_PROMPT, build_prompt(chunk, kind_hint(kind) + extra), deadline)
        except OutOfTime as exc:
            # وقت تمام شد، نه خطا. صف سالم می‌ماند و اجرای بعدی ادامه می‌دهد.
            # عمداً هشدار نمی‌فرستد؛ همین هشدار بود که کانال را پر کرده بود.
            log("  out of time, queue stays for next run:", str(exc)[:80])
            break
        except Exception as exc:
            failures += 1
            log("model error:", str(exc)[:200])
            if start == 0:
                alert(
                    "<b>هشدار رادار</b>\n"
                    "هیچ لایهٔ مدلی جواب نداد. صف سالم است؛ اجرای بعدی جبران می‌کند.\n"
                    "<code>{}</code>".format(esc(str(exc)[:300]))
                )
            break

        items = result.get("items", []) if isinstance(result, dict) else result
        by_id = {x["id"]: x for x in chunk}

        def rank(entry):
            target = by_id.get(str(entry.get("id", "")))
            if not target:
                return -1
            return final_score(target, entry)

        items.sort(key=rank, reverse=True)
        for entry in items:
            if posted >= per_run or int(d["count"]) >= hard_cap:
                break
            it = by_id.get(str(entry.get("id", "")))
            if not it:
                # مدل گاهی شناسه را از خودش می‌سازد و پست از دست می‌رفت.
                # اگر دسته فقط یک آیتم دارد، همین یکی را بردار؛ برای دستهٔ
                # چندآیتمی این کار خطرناک است و رد می‌شود.
                if len(chunk) == 1:
                    it = chunk[0]
                else:
                    log("  unknown id from model:", str(entry.get("id", ""))[:30])
                    continue
            if entry_skipped(entry):
                reason = str(entry.get("why") or "").strip()[:90] or "بدون دلیل"
                log("  model skipped:", it.get("title", "")[:40], "|", reason)
                rejected.append((it, entry, "رد مدل: " + reason))
                continue
            # برچسب‌هایی که مدل خودش می‌گذارد حذف می‌شوند تا پست فرم پرکردنی نباشد.
            for field in ("title", "summary", "meta", "name"):
                if entry.get(field):
                    entry[field] = strip_labels(entry[field])
                    entry[field] = fix_phrases(entry[field])
            if entry.get("details"):
                entry["details"] = fix_phrases(clean_details(entry["details"]))
            text_all = " ".join(
                [
                    it.get("desc", "") or "",
                    it.get("full", "") or "",
                    entry.get("title", "") or "",
                    entry.get("summary", "") or "",
                    entry.get("details", "") or "",
                ]
            )
            # برچسب از متنِ خودِ رویداد ساخته می‌شود، نه از متن کامل مقاله.
            # متن کامل نوار کنار و لینک‌های بی‌ربط دارد و همان باعث شد
            # یک آموزش ساخت جدول محصول با برچسب «هشدار امنیتی» پست شود.
            label_text = " ".join(
                [
                    it.get("title", "") or "",
                    it.get("desc", "") or "",
                    entry.get("title", "") or "",
                    entry.get("summary", "") or "",
                    entry.get("details", "") or "",
                ]
            )
            # برچسب همان چیزی است که مخاطب اول می‌بیند، پس باید قبل از
            # سدها معلوم باشد. سدها بر اساس برچسب کار می‌کنند، نه بر اساس
            # دستهٔ فید: یک خبر هوش مصنوعی که از فید «ابزار» آمده، خبر است
            # و نباید به‌خاطر نداشتن «لینک استفاده» رد شود.
            label = classify_label(
                kind,
                label_text,
                entry.get("label", ""),
                title=entry.get("title", ""),
                source_title=it.get("title", ""),
            )
            # آپدیت نسخه، هشدار امنیتی نیست؛ مگر خود فهرست تغییرات از
            # آسیب‌پذیری یا اکسپلویت اسم برده باشد. «تقویت امنیت» یعنی
            # سخت‌تر شدن کد، نه حفره‌ای که باز بوده. یک بار آپدیت
            # المنتور که فقط «security enforcement» بود با برچسب
            # هشدار امنیتی رفت.
            upd = it.get("plugin_update") or {}
            if upd and not upd.get("vuln") and label in ("هشدار امنیتی", "فوری"):
                log("  update is hardening, not a vulnerability:", it["title"][:40])
                label = "آپدیت"
            news_like = kind != "offers" and label in ("خبر", "آپدیت", "هشدار امنیتی", "فوری")
            # پستی که برچسب خبری ندارد باید مثل محصول سنجیده شود، حتی اگر
            # برچسبش «آموزش» باشد. وگرنه مدل با عوض کردن برچسب از سد
            # «قابل استفاده» فرار می‌کند؛ یک ابزار با نمرهٔ کاربرد سه
            # همین‌طور رد شد.
            needs_product = kind in ("tools", "offers", "news") and not news_like
            score = final_score(it, entry)
            fast = False
            # سهم این اجرا یا سقف دسته که تمام شد، فقط یک خبر خیلی مهم
            # و خیلی تازه می‌تواند از سد رد شود، آن هم دو بار در روز.
            if posted >= my_limit or posted_kind >= kind_cap:
                if (
                    score >= fast_score
                    and true_age(it) <= fast_age
                    and fast_used < fast_per_day
                ):
                    fast = True
                    fast_used += 1
                    log("fast lane:", kind, entry.get("title", "")[:40])
                else:
                    log("share for this run is used up")
                    break
            if not (entry.get("title") or "").strip() or not (entry.get("summary") or "").strip():
                continue
            # نمره دیگر ابزار رد کردن نیست؛ فقط skip و سدهای پایین رد می‌کنند.
            if bad_lang(entry.get("title", "") + entry.get("summary", "") + entry.get("details", "")):
                log("  bad script skipped:", entry.get("title", "")[:50])
                continue
            # اگر خود مدل در متنش نوشته که این موضوع به کانال نمی‌خورد،
            # حرفش را قبول می‌کنیم. نمره‌اش را قبول نکردیم و همین یک بار
            # کافی بود تا یک مجموعه والپیپر منتشر شود.
            said = " ".join(
                [
                    entry.get("title", "") or "",
                    entry.get("summary", "") or "",
                    entry.get("details", "") or "",
                    entry.get("why", "") or "",
                ]
            )
            if OFFTOPIC_SELF_RE.search(said):
                log("  model called it off-topic:", entry.get("title", "")[:50])
                rejected.append((it, entry, "خودِ مدل گفت بی‌ربط است"))
                continue
            # همین الگو، یک بار دیگر: مدل نوشت «این یک لیست مرجعی است،
            # نه ابزاری برای اجرا» و باز هم در دستهٔ ابزار پست شد.
            if kind in ("news", "tools", "showcase") and NOT_A_TOOL_SELF_RE.search(said):
                log("  model said it is a list, not a tool:", entry.get("title", "")[:50])
                rejected.append((it, entry, "خودِ مدل گفت فهرست است نه ابزار"))
                continue
            # فرصت و ابزار ارزششان به اعتبارشان است نه به تازگی خبر؛ پنجره بازتر.
            if true_age(it) > max_age_gate:
                log("  too old for a standalone post:", it["title"][:50])
                continue
            if news_like and num(entry.get("imp")) < 4:
                rejected.append((it, entry, "اهمیت کم"))
                continue
            # راهنما خبر نیست. وبلاگ سازنده‌ها هر روز آموزش می‌نویسند و
            # کانال پر می‌شد از «چطور فلان کار را بکنیم».
            if kind == "news" and TUTORIAL_TITLE.search(it.get("title", "")):
                it["channel_skip"] = True  # دیگر به مدل نمی‌رود
                rejected.append((it, entry, "آموزش است نه خبر"))
                continue
            # «قابل استفاده» فقط برای پست‌هایی است که خودشان را ابزار
            # معرفی می‌کنند. یک خبر هوش مصنوعی که در دستهٔ ابزار آمده،
            # ابزار نیست و سنجیدنش با این معیار غلط است.
            if needs_product and num(entry.get("use")) < 5:
                rejected.append((it, entry, "قابل استفاده نیست"))
                continue
            # در دستهٔ فرصت، چیزی که رایگان نیست فرصت نیست.
            if kind == "offers" and not FREE_HINT.search(text_all):
                rejected.append((it, entry, "فرصت نیست"))
                continue
            # فرصت باید به کار مخاطب بیاید، نه هر چیز رایگانی در اینترنت.
            if kind == "offers":
                if OFFER_JUNK.search(it.get("title", "") + " " + it.get("desc", "")):
                    it["channel_skip"] = True  # دیگر به مدل نمی‌رود
                    rejected.append((it, entry, "به کار کانال نمی‌آید"))
                    continue
                if not OFFER_TOPIC.search(label_text):
                    it["channel_skip"] = True
                    rejected.append((it, entry, "موضوع ربطی ندارد"))
                    continue
                if num(entry.get("use")) < 7:
                    rejected.append((it, entry, "کم‌بها"))
                    continue
            ok_quality, why = quality_ok(entry.get("title", ""), entry.get("summary", ""))
            if not ok_quality:
                log("  quality gate:", why, entry.get("title", "")[:40])
                rejected.append((it, entry, why))
                continue
            # ربط ساختگی: جمله‌ای که مدل به خبر می‌چسباند تا آن را به کانال
            # مربوط کند، در حالی که منبع حرفی از سایت نزده.
            if not relevance_ok(
                " ".join([entry.get("summary", ""), entry.get("details", "")]),
                " ".join([it.get("title", ""), it.get("desc", ""), it.get("full", "")]),
            ):
                log("  made-up relevance:", entry.get("title", "")[:40])
                rejected.append((it, entry, "ربط ساختگی"))
                continue
            # بلوک نقل‌قول نباید خالی یا تشریفاتی باشد.
            det = (entry.get("details") or "").strip()
            need = 60 if needs_product else 30
            det_lines = len([x for x in det.splitlines() if x.strip()])
            # یک بند بلند هم «پوک» نیست. سد باید محتوا را بسنجد نه شکل
            # شکستن خط را؛ مدل گاهی یک پاراگراف چهارخطی می‌نویسد و قبلاً
            # فقط به‌خاطر یک‌خطی بودن رد می‌شد.
            if len(det) < need or (det_lines < 2 and len(det) < 150):
                log("  hollow blockquote:", entry.get("title", "")[:40])
                rejected.append((it, entry, "جان مطلب پوک"))
                continue
            # «وضعیت دسترسی: نامشخص» یعنی مدل چیزی نمی‌داند و دارد جا پر می‌کند.
            if HOLLOW_RE.search(det) or HOLLOW_RE.search(entry.get("summary", "")):
                log("  unknown status:", entry.get("title", "")[:40])
                rejected.append((it, entry, "وضعیت نامعلوم"))
                continue
            # نام محصول: اول از مدل، و اگر نداد از تیتر منبع. پست بی‌نام نمی‌رود.
            name = valid_name(entry.get("name", ""), it) or auto_name(it)
            if needs_product and not name:
                log("  nameless post:", entry.get("title", "")[:40])
                rejected.append((it, entry, "بی‌نام"))
                continue
            # لینک استفاده برای فرصت واجب است: فرصتی که راه گرفتنش را
            # نگوید بی‌فایده است. برای ابزار واجب نیست؛ خود لینک منبع
            # جایی برای رفتن است و پست بدون خط اقدام هم کامل می‌شود.
            # قبلاً این سد هر ابزاری را که صفحهٔ محصول جدا نداشت رد
            # می‌کرد و کانال خالی می‌ماند.
            if kind == "offers" and not (it.get("use_link") or is_product_link(it.get("link", ""))):
                log("  no use link:", entry.get("title", "")[:40])
                rejected.append((it, entry, "لینک استفاده ندارد"))
                continue
            if is_recent_story(store, it):
                log("  already covered:", it["title"][:60])
                continue
            if not link_alive(it["link"]):
                log("  dead link skipped:", it["link"])
                continue
            label_tag = "#" + re.sub(r"\s+", "_", label)
            kind_tag = kind_tag_for(
                kind,
                " ".join(
                    [
                        entry.get("title", ""),
                        entry.get("summary", ""),
                        entry.get("details", ""),
                        it.get("title", ""),
                    ]
                ),
            )
            if label_tag == kind_tag or kind_tag.startswith(label_tag):
                tags = kind_tag
            elif label_tag.startswith(kind_tag):
                tags = label_tag
            else:
                tags = label_tag + " " + kind_tag
            text = format_post(
                label,
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("details", ""),
                entry.get("meta", ""),
                it["link"],
                tags,
                it.get("published"),
                name=name,
                use_link=it.get("use_link", "") if (kind == "offers" or action_links_on()) else "",
            )
            # خارج از پنجرهٔ انتشار، متن نوشته و نگه داشته می‌شود.
            # هشدار امنیتی و خبر فوری منتظر ساعت نمی‌مانند.
            # هر پست نوشته می‌شود و بعد با فاصله منتشر می‌شود، نه همه
            # با هم. قبلاً چهار پست یک اجرا با هم می‌رفتند و صبح کانال
            # شبیه سیل می‌شد. هشدار امنیتی و خبر فوری منتظر نمی‌مانند.
            if publish_cfg(s).get("enabled", True) and label not in (
                publish_cfg(s).get("bypass_labels") or []
            ):
                if kind_cap - posted_kind - held_by_kind.get(kind, 0) <= 0 and not fast:
                    log("  no room left to hold more of", kind)
                    break
                if not hold_ready(it, label, text, score, kind):
                    log("  ready store is full, stopping this run")
                    break
                held_by_kind[kind] = held_by_kind.get(kind, 0) + 1
                # پست نگه‌داشته‌شده هم داستانش همین‌جا ثبت می‌شود. قبلاً
                # فقط پست منتشرشده ثبت می‌شد؛ پس اجرای بعد همان خبر را
                # دوباره می‌ساخت و دو نسخه در صف نگه‌داری می‌نشست و هر دو
                # منتشر می‌شد.
                add_story(store, it)
                if not DRY:
                    write_json(QUEUE, q)
                    write_json(STORIES, store)
                log("  held for publishing:", entry.get("title", "")[:40])
                continue
            try:
                if kind == "showcase":
                    pic = og_image(it["link"])
                    if pic:
                        res = send_photo(pic, text)
                    else:
                        res = send(text)
                else:
                    res = send(text)
            except Exception as exc:
                log("  telegram error:", exc)
                break
            # شناسهٔ پیام را نگه می‌داریم. بدون آن، پاک کردن یا ویرایش همان
            # پست در کانال ممکن نیست؛ یک بار همین نبودن، تمیز کردن کانال
            # را غیرممکن کرد.
            mid = ((res or {}).get("result") or {}).get("message_id")
            if mid:
                it["msg_id"] = mid
            seen["ids"][it["id"]] = today()
            it["posted"] = True
            add_story(store, it)
            d["count"] += 1
            posted += 1
            posted_kind += 1
            d["kinds"][kind] = posted_kind
            if (it.get("group") or "") == "ai":
                d["kinds"]["ai"] = int(d["kinds"].get("ai", 0)) + 1
            if fast:
                d["fast"] = int(d.get("fast", 0)) + 1
            archive_row(it, label, entry.get("title", ""), entry.get("summary", ""), score, kind)
            if not DRY:
                write_json(QUEUE, q)
                write_json(DAILY, d)
                write_json(STORIES, store)
                write_json(SEEN, seen)
            time.sleep(3)

    if rejected and len(rejected) >= 2 and not DRY:
        counts = {}
        for _, _, why in rejected:
            counts[why] = counts.get(why, 0) + 1
        summary = "، ".join("{} {}".format(fa_num(v), k) for k, v in counts.items())
        try:
            send_to(personal_chat(), "<b>رد شد — {}</b>\n{}".format(KIND_FA.get(kind, kind), esc(summary)), preview=False)
        except Exception as exc:
            log("reject report failed:", str(exc)[:100])

    if not DRY:
        write_json(STORIES, store)
        write_json(QUEUE, q)
        write_json(DAILY, d)
    log("posted:", posted, "| queued:", len(pending) - posted)
    return posted


def archive_row(it, label, title, summary, score, kind):
    if DRY:
        return
    ARCH.mkdir(parents=True, exist_ok=True)
    row = {
        "id": it["id"],
        "at": now().isoformat(timespec="seconds"),
        "kind": kind,
        "label": label,
        "source": it.get("source", ""),
        "link": it.get("link", ""),
        "title_orig": it.get("title", ""),
        "title_fa": title,
        "summary_fa": summary,
        "score": score,
        "msg_id": it.get("msg_id") or 0,
    }
    with ARCHIVE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def age_label(hours):
    h = float(hours or 0)
    if h < 1:
        return "کمتر از یک ساعت پیش"
    if h < 24:
        return "{} ساعت پیش".format(fa_num(int(h)))
    return "{} روز پیش".format(fa_num(int(h // 24)))


def personal_chat():
    return env_clean("TG_PERSONAL") or env_clean("TG_ADMIN") or TG_CHAT


def digest_run(s, hours=24, top_n=10, mark=True):
    q = read_json(QUEUE, {"v": 1, "items": []})
    cutoff = now() - timedelta(hours=hours)
    pending = []
    for x in q["items"]:
        if x.get("posted") or x.get("digest_sent"):
            continue
        try:
            ts = datetime.fromisoformat(str(x.get("published") or x.get("added") or ""))
        except Exception:
            ts = None
        if ts and ts < cutoff:
            continue
        pending.append(x)
    pending.sort(key=lambda x: x.get("age_hours", 99))
    top = pending[:top_n]
    if not top:
        log("nothing to digest")
        return 0

    tr = {}
    try:
        res = call_models(
            SYSTEM_PROMPT,
            build_prompt(
                top,
                "این‌ها خبرهایی هستند که به پست کانال نرسیدند. برای هر کدام فقط یک تیتر فارسی کوتاه "
                "و یک خط خودمونی بنویس که بگوید این چیه و به چه کار می‌آید. جزئیات و متا را خالی بگذار و score را صفر بده.",
            ),
            budget=150,
        )
        items = res.get("items", []) if isinstance(res, dict) else res
        for entry in items or []:
            tr[str(entry.get("id", ""))] = (entry.get("title", ""), entry.get("summary", ""))
    except Exception as exc:
        log("digest model skipped:", str(exc)[:120])

    lines = [
        "<b>{} جمع‌بندی برای خودت</b>".format(emoji_for("جمع‌بندی")),
        "<i>{} مورد از {} ساعت گذشته</i>".format(fa_num(len(top)), fa_num(hours)),
        "",
    ]
    blocks = []
    for it in top:
        title, gist = tr.get(it["id"], ("", ""))
        title = str(title or "").strip()
        # تیتر انگلیسی خام برای کانال فارسی بی‌مصرف است. آیتمی که ترجمه نشد، نمی‌آید.
        if not title or not re.search(r"[\u0600-\u06FF]", title):
            continue
        block = ["• <b>{}</b>".format(esc(title[:120]))]
        if gist:
            block.append(esc(str(gist)[:240]))
        name = valid_name(it.get("name", ""), it) or auto_name(it)
        if name:
            block.append("<b>{}</b>".format(esc(name)))
        use_link = it.get("use_link", "")
        if use_link:
            block.append('<a href="{}">لینک استفاده</a>'.format(use_link.replace('"', "%22")))
        else:
            search_name = name or (it.get("title", "").split()[0] if it.get("title") else "این ابزار")
            block.append("<i>⚠️ لینک استفاده پیدا نشد — با «{}» دنبالش بگرد</i>".format(esc(search_name)))
        block.append('<a href="{}">لینک منبع</a>'.format(it["link"].replace('"', "%22")))
        pub = it.get("published") or ""
        if pub:
            block.append("<i>{}</i>".format(esc(stamp(pub))))
        block.append("")
        blocks.append("\n".join(block))

    # برش بر اساس بلوک، تا هیچ لینکی نصفه نماند.
    if not blocks:
        log("digest: nothing translated, not sending")
        return 0
    text = "\n".join(lines)
    for block in blocks:
        if len(text) + len(block) > 3900:
            text += "\n<i>بقیه در اجرای بعد.</i>"
            break
        text += block
    target = personal_chat()
    try:
        send_to(target, text, preview=False)
    except Exception as exc:
        log("personal send failed:", str(exc)[:150])
        try:
            alert(
                "<b>ارسال جمع‌بندی به کانال شخصی نشد</b>\n"
                "ربات باید در آن کانال ادمین باشد. متن در چت خودت می‌آید.\n"
                "<code>{}</code>".format(esc(str(exc)[:200]))
            )
            send_to(TG_ADMIN or TG_CHAT, text, preview=False)
        except Exception as exc2:
            log("fallback send failed:", str(exc2)[:120])
    if mark and not DRY:
        for it in top:
            it["digest_sent"] = True
            it["channel_skip"] = True
        write_json(QUEUE, q)
    log("digest items:", len(top))
    return len(top)


def report_run(s, st):
    total = 0
    broken = 0
    if ARCHIVE.exists():
        for line in ARCHIVE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            total += 1
            try:
                json.loads(line)
            except Exception:
                broken += 1
    lines = ["<b>گزارش هفتگی رادار</b>", ""]
    lines.append("آرشیو: {} پست".format(total))
    if broken:
        lines.append("خط خراب در آرشیو: {}".format(broken))
    bad = [v["name"] for v in st.values() if int(v.get("fails", 0)) >= 3]
    lines.append("منابع خراب: {}".format("، ".join(bad) if bad else "هیچ"))
    q = read_json(QUEUE, {"v": 1, "items": []})
    lines.append("در صف: {}".format(len([x for x in q["items"] if not x.get("posted")])))
    lines.append("")
    lines.append("#گزارش")
    alert("\n".join(lines))
    return 1


def watchdog_run():
    last = read_json(LAST, {})
    ts = last.get("at")
    if not ts:
        alert("<b>نگهبان</b>\nهیچ اجرایی ثبت نشده.")
        return 0
    age = (now() - datetime.fromisoformat(ts)).total_seconds() / 3600
    if age > 6:
        alert("<b>نگهبان</b>\n{:.0f} ساعت است هیچ اجرایی ثبت نشده. چرخه خوابیده.".format(age))
        return 1
    log("watchdog ok, last run {:.1f}h ago".format(age))
    return 0


# ---------------------------------------------------------------- auto

def guardian_ready(s, role="main"):
    """آیا این اجرا باید کار کند؟

    دو نقش داریم. تریگر اصلی با فاصلهٔ کوتاه می‌زند و فقط باید از روی‌هم‌افتادن
    جلوگیری کند، پس فاصلهٔ کوتاه کافی است. تریگر پشتیبان همان فاصله را
    می‌زند ولی فقط وقتی وارد می‌شود که تریگر اصلی مدتی ساکت بوده باشد؛ وگرنه
    هر دو با هم می‌دوند و سهمیهٔ مدل را دو بار می‌سوزانند.
    """
    ls = read_json(LAST_START, {})
    try:
        gap = (now() - datetime.fromisoformat(str(ls.get("at", "")))).total_seconds() / 60.0
    except Exception:
        gap = 999.0
    if role == "backup":
        limit = float(s.get("backup_min_gap_min", 25))
    else:
        limit = float(s.get("guard_min_gap_min", 5))
    if gap < limit:
        log("guardian({}): last auto start {:.0f} min ago, skipping".format(role, gap))
        return False
    if not DRY:
        write_json(LAST_START, {"at": now().isoformat(timespec="seconds"), "src": role})
    return True


def auto_run(s, st, seen, extra, role="main"):
    """یک اجرا که خودش تصمیم می‌گیرد کدام رادار وقتش است.

    جای زمان‌بندی گیت‌هاب را می‌گیرد، چون آن زمان‌بندی با تأخیر چندساعته
    می‌رسد. این تابع با تریگر بیرونی هر چند دقیقه صدا زده می‌شود؛ فاصلهٔ
    واقعی را از تاریخ اجراها ببین، نه از این متن.
    """
    if not guardian_ready(s, role):
        return 0

    # اول نوبت پست‌های نگه‌داشته‌شده است. اگر داخل پنجرهٔ انتشار باشیم،
    # یکی از آماده‌ها می‌رود و بعد سراغ جمع‌آوری می‌رویم.
    publish_ready(s, st, seen)

    sch = read_json(SCHED, {})
    started = time.time()
    budget = int(s.get("auto_budget_sec", 720))
    hard = started + budget
    ran = []

    def left():
        return budget - (time.time() - started)

    def is_due(key, minutes):
        ts = sch.get(key)
        if not ts:
            return True
        try:
            return (now() - datetime.fromisoformat(ts)).total_seconds() >= minutes * 60
        except Exception:
            return True

    def mark(key, value=None):
        # فایل را تازه می‌خوانیم. در طول اجرا ممکن است جای دیگری هم
        # نوشته شده باشد (مثل وضعیت سلامت لایه‌های مدل) و نوشتن نسخهٔ
        # کهنه روی آن، آن را پاک می‌کرد.
        fresh = read_json(SCHED, {})
        fresh[key] = value or now().isoformat(timespec="seconds")
        sch.clear()
        sch.update(fresh)
        if not DRY:
            write_json(SCHED, fresh)

    # تمیزکاری روزانهٔ صف. روزی یک بار، قبل از هر چیز دیگر. دستی پاک
    # کردن صف با ذخیرهٔ اجرا تصادم می‌کرد و همان پست دوباره می‌رفت.
    today = now().date().isoformat()
    if sch.get("pruned_day") != today:
        before, after = prune_queue(s)
        if before != after:
            log("queue prune: {} -> {}".format(before, after))
        mark("pruned_day", today)

    teh = now().astimezone(TZ)

    def overdue(key, minutes):
        """چقدر از وقتش گذشته، به واحد دورهٔ خودش. بزرگ‌تر یعنی واجب‌تر.

        سقف دارد تا دسته‌ای که مدت‌ها اجرا نشده، برای همیشه نوبت را
        قبضه نکند و بقیه را گرسنه نگذارد.
        """
        ts = sch.get(key)
        if not ts:
            return 2.5
        try:
            gap = (now() - datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return 2.5
        return min(gap / max(60.0, minutes * 60.0), 2.5)

    # هر اجرا فقط یک دستهٔ سنگین را برمی‌دارد. دلیلش ساده است: بودجهٔ یک
    # اجرا اندازهٔ یک دسته است، نه چند دسته. قبلاً چهار دسته پشت سر هم
    # شروع می‌شدند، هر کدام تکه‌ای از وقت را می‌خورد و آخری همیشه
    # نیمه‌کاره می‌ماند — همان «وقت این اجرا تمام شد» که مدام می‌آمد.
    # حالا هر اجرا یک دسته، و نوبت با آنی است که دیرتر شده.
    due = []
    for key, minutes in (
        ("news", int(s.get("news_every_min", 55))),
        ("tools", int(s.get("tools_every_min", 300))),
    ):
        if is_due(key, minutes):
            due.append((overdue(key, minutes), key))
    if teh.weekday() in (0, 2, 4) and is_due("showcase", 2880):
        due.append((overdue("showcase", 2880), "showcase"))

    # نوبت برابر وقتی هر دو تازه‌اند: خبر اول، بعد ابزار، آخر نمایش.
    order = {"news": 0, "tools": 1, "showcase": 3}
    due.sort(key=lambda pair: (-pair[0], order.get(pair[1], 9)))

    if due and left() > 240:
        key = due[0][1]
        model_run(key, s, st, seen, extra, hard_deadline=hard)
        mark(key)
        ran.append(key)
    elif due:
        log("skipped", due[0][1], "| left", int(left()), "s")

    # امنیت سبک‌تر است و سقف روزانه‌اش دو تاست، پس بعد از دستهٔ اصلی
    # می‌آید. قبلاً در هر اجرا صدا زده می‌شد: روزی نود و شش تماس مدل
    # که سهمیهٔ لایه‌های رایگان را می‌سوزاند و خبر را بی‌وقت می‌گذاشت.
    sec_every = int(s.get("security_every_min", 60))
    if is_due("security", sec_every) and left() > 180:
        security_run(s, st, seen, hard_deadline=min(hard - 60, time.time() + 240))
        mark("security")
        ran.append("security")

    day = teh.date().isoformat()
    if teh.hour == 23 and sch.get("digest_day") != day and left() > 120:
        digest_run(s)
        mark("digest_day", day)
        ran.append("digest")

    week = "{}-{}".format(*tuple(teh.isocalendar())[:2])
    if teh.weekday() == 0 and 9 <= teh.hour <= 11 and sch.get("report_week") != week and left() > 120:
        report_run(s, st)
        mark("report_week", week)
        ran.append("report")

    log("auto ran:", ", ".join(ran) if ran else "nothing due", "| left", int(left()), "s")
    if ACTIVE_PROVIDER:
        fresh = read_json(SCHED, {})
        fresh["last_provider"] = ACTIVE_PROVIDER
        sch.clear()
        sch.update(fresh)
        if not DRY:
            write_json(SCHED, fresh)
    return 0


def preview_run(s, st, seen, kind="news", limit=1, pick=""):
    """یک پست کامل از تازه‌ترین مطلب، فقط برای چت شخصی.

    برای این ساخته شد که لحن و شکل پست را ببینیم و اصلاح کنیم، بی‌آنکه
    کسی در کانال چیزی ببیند و بی‌آنکه آیتم «دیده‌شده» ثبت شود. اگر همان
    پست به کانال می‌رفت، پذیرفته می‌شد یا با چه دلیلی رد می‌شد، پایینش
    نوشته می‌شود.
    """
    chat = env_clean("TG_PERSONAL") or env_clean("TG_ADMIN")
    if not chat:
        log("preview: no personal chat configured")
        return 0
    kind = (kind or "news").strip().lower()
    rows = dedupe_stories(collect(kind, st, seen, budget_sec=80, watch=False))
    # اگر مطلب مشخصی خواسته شده و در جمع‌آوری تازه نبود، صف را هم نگاه
    # کن. جمع‌آوری بودجه دارد و ممکن است همان منبع در این اجرا خوانده
    # نشود؛ ولی آیتم قبلاً در صف نشسته است.
    if pick:
        q = read_json(QUEUE, {"v": 1, "items": []})
        have = {x.get("id") for x in rows}
        rows = rows + [
            x
            for x in q.get("items", [])
            if (x.get("kind") or "news") == kind and not x.get("posted") and x.get("id") not in have
        ]
    kept = []
    blocked_words = load_blocked()
    for x in rows:
        if blocked_hit(x, blocked_words) or (kind != "security" and persian_product(x)):
            continue
        blob = " ".join(
            [x.get("title") or "", x.get("link") or "", x.get("desc") or ""]
        )
        if NON_NEWS_RE.search(blob) or JUNK_NEWS_RE.search(blob) or LIST_REPO_RE.search(blob):
            continue
        if kind == "news" and PODCAST_RE.search(blob):
            continue
        if kind == "news" and OFF_AUDIENCE_RE.search(blob) and not SITE_TOPIC_RE.search(blob):
            continue
        if kind == "tools" and not BUILDER_TOPIC.search(blob):
            continue
        kept.append(x)
    # با --pick می‌شود یک خبر مشخص را برای آزمون لحن انتخاب کرد، نه
    # فقط تازه‌ترین را. برای چک کردن یک مطلب خاص لازم است.
    if pick:
        needle = pick.strip().lower()
        kept = [
            x
            for x in kept
            if needle in ((x.get("title") or "") + " " + (x.get("link") or "")).lower()
        ]
        if not kept:
            send_to(chat, "پیش‌نمایش: مطلبی با این نشانی در صف نبود: " + esc(pick), False)
            return 0
    if not kept:
        send_to(chat, "پیش‌نمایش: آیتم تازه‌ای پیدا نشد. کمی بعد دوباره بزن.", False)
        return 0
    kept.sort(key=lambda x: true_age(x))
    batch = kept[:limit]
    fetch_full_parallel(batch)
    try:
        res = call_models(SYSTEM_PROMPT, build_prompt(batch, kind_hint(kind)), budget=150)
    except Exception as exc:
        send_to(chat, "پیش‌نمایش: مدل جواب نداد — " + esc(str(exc)[:200]), False)
        return 0
    items = res.get("items", []) if isinstance(res, dict) else []
    if not items:
        send_to(chat, "پیش‌نمایش: پاسخ خالی بود.", False)
        return 0
    by_id = {x["id"]: x for x in batch}
    for entry in items[:limit]:
        it = by_id.get(str(entry.get("id", ""))) or batch[0]
        if entry_skipped(entry):
            send_to(
                chat,
                "پیش‌نمایش: مدل این یکی را رد کرد — {}".format(
                    esc(str(entry.get("why") or "").strip()[:150] or "بدون دلیل")
                ),
                False,
            )
            continue
        for field in ("title", "summary", "meta", "name"):
            if entry.get(field):
                entry[field] = fix_phrases(strip_labels(entry[field]))
        entry["details"] = fix_phrases(clean_details(entry.get("details") or ""))
        text_all = " ".join(
            [
                it.get("desc") or "",
                it.get("full") or "",
                entry.get("title") or "",
                entry.get("summary") or "",
                entry.get("details") or "",
            ]
        )
        label = classify_label(
            kind, text_all, entry.get("label", ""), False, entry.get("title", ""), it.get("title", "")
        )
        name = valid_name(entry.get("name", ""), it) or auto_name(it)
        tags = "#" + re.sub(r"\s+", "_", label)
        body = format_post(
            label,
            entry.get("title", ""),
            entry.get("summary", ""),
            entry.get("details", ""),
            entry.get("meta", ""),
            it.get("link", ""),
            tags,
            it.get("published"),
            name=name,
            use_link=it.get("use_link", "") if (kind == "offers" or action_links_on()) else "",
        )
        ok, why = quality_ok(entry.get("title", ""), entry.get("summary", ""))
        verdict = "اگر می‌رفت، پذیرفته می‌شد" if ok else "اگر می‌رفت، رد می‌شد: " + why
        head = (
            "<b>پیش‌نمایش — به کانال نرفت</b>\n"
            "دسته: {}\nبرچسب: {}\nنمره: {}\n{}\nعنوان منبع: {}".format(
                KIND_FA.get(kind, kind), label, num(entry.get("score")), verdict, it.get("title", "")
            )
        )
        send_to(chat, head, False)
        send_to(chat, body)
        log("--- preview post ---")
        log(body)
    log("preview sent:", len(items[:limit]))
    return 0


def compare_run(s, st, seen, limit=2, kind=""):
    """همان آیتم را به هر لایه می‌دهد تا کیفیت را با چشم خودت ببینی.

    جای حدس زدن دربارهٔ «کدام مدل بهتر می‌نویسد» این را ساخته‌ام.
    هیچ پستی در کانال نمی‌رود؛ فقط به چت شخصی می‌آید.
    """
    chat = env_clean("TG_PERSONAL") or TG_ADMIN
    if not chat:
        log("compare: no personal chat configured")
        return 0

    def keep(x):
        blob = " ".join([x.get("title", "") or "", x.get("link", "") or ""])
        return not (
            NON_NEWS_RE.search(blob) or JUNK_NEWS_RE.search(blob) or LIST_REPO_RE.search(blob)
        )

    def pool(k):
        # همان پنجرهٔ تازگی که در اجرای واقعی هست، وگرنه خبر کهنه هم
        # وارد مقایسه می‌شود و نتیجه گمراه‌کننده است.
        rows = dedupe_stories(collect(k, st, seen, budget_sec=60, watch=False))
        rows = [x for x in rows if keep(x)]
        fresh = [x for x in rows if true_age(x) <= 48]
        return fresh or rows

    # دسته مهم است. یک بار یک قالب رایگان را در دستهٔ «خبر» به مدل
    # دادیم، مدل درست گفت این خبر نیست و صفر داد، و ما به‌غلط نتیجه
    # گرفتیم که مدل ضعیف است.
    kind = (kind or "").strip().lower()
    if kind in KIND_HINT:
        rows = pool(kind)
    else:
        kind, rows = "", []
        for k in ("news", "tools", "offers"):
            got = pool(k)
            if len(got) > len(rows):
                kind, rows = k, got
        kind = kind or "news"

    if not rows:
        send_to(chat, "مقایسه: آیتم تازه‌ای پیدا نشد. کمی بعد دوباره بزن.", False)
        return 0
    rows.sort(key=lambda x: code_score(x, x.get("sources_count", 1)), reverse=True)
    batch = rows[:limit]
    fetch_full_parallel(batch)
    prompt = build_prompt(batch, kind_hint(kind))

    send_to(
        chat,
        "مقایسهٔ مدل‌ها روی {} آیتم از دستهٔ «{}». هر لایه یک پیام می‌فرستد.".format(
            len(batch), KIND_FA.get(kind, kind)
        ),
        False,
    )
    for prov in model_providers():
        model = (prov.get("models") or [""])[0]
        head = "<b>{} / {}</b>".format(esc(prov["name"]), esc(model))
        try:
            res = call_models(SYSTEM_PROMPT, prompt, budget=150, only=prov["name"])
        except Exception as exc:
            send_to(chat, head + "\nخطا: " + esc(str(exc)[:200]), False)
            continue
        items = res.get("items", []) if isinstance(res, dict) else (res or [])
        if not items:
            send_to(chat, head + "\nپاسخ خالی بود.", False)
            continue
        by_id = {x["id"]: x for x in batch}
        written = 0
        skipped = []
        for entry in items[:limit]:
            title = (entry.get("title") or "").strip()
            # برای آیتم ردشده، تیتر مدل خالی است. آن‌وقت باید تیتر
            # خودِ خبر را نشان دهیم، نه یک علامت سؤال.
            src = (by_id.get(str(entry.get("id", ""))) or {}).get("title") or ""
            score = num(entry.get("score"))
            why = str(entry.get("why") or "").strip()
            said = " ".join(
                [
                    entry.get("title", "") or "",
                    entry.get("summary", "") or "",
                    entry.get("details", "") or "",
                    why,
                ]
            )
            # همان دروازهٔ اجرای واقعی، به همان ترتیب. هر چه اینجا
            # می‌ماند در کانال هم می‌ماند؛ پس دلیل ماندنش هم باید
            # دیده شود، نه اینکه فقط یک پیام خالی بیاید.
            if not title:
                skipped.append(("بی‌تیتر", src or title or "?", score, why))
                continue
            if not (entry.get("summary") or "").strip():
                skipped.append(("بی‌خلاصه", src or title, score, why))
                continue
            if entry_skipped(entry):
                skipped.append(("رد مدل", src or title or "?", score, why))
                continue
            if OFFTOPIC_SELF_RE.search(said):
                skipped.append(("خودش گفت بی‌ربط", src or title, score, why))
                continue
            if kind in ("news", "tools", "showcase") and NOT_A_TOOL_SELF_RE.search(said):
                skipped.append(("خودش گفت فهرست است", src or title, score, why))
                continue
            it = by_id.get(str(entry.get("id", ""))) or batch[0]
            text = format_post(
                entry.get("label") or "خبر",
                title,
                entry.get("summary", ""),
                entry.get("details", ""),
                entry.get("meta", ""),
                it.get("link", ""),
                "",
                None,
                name=entry.get("name", ""),
                use_link=it.get("use_link", "") if action_links_on() else "",
            )
            # نمره‌ها را پایین پست می‌گذاریم تا معلوم باشد چرا این
            # پیش‌نویس از سد رد شده و آن یکی نه.
            foot = "نمره {} · اهمیت {} · کاربرد {}".format(
                fa_num(entry.get("score", "?")),
                fa_num(entry.get("imp", "?")),
                fa_num(entry.get("use", "?")),
            )
            send_to(chat, head + "\n\n" + text + "\n\n" + esc(foot), False)
            written += 1
        if skipped:
            lines = [head, ""]
            if not written:
                lines.append("هیچ‌کدام پیش‌نویس نشدند:")
            for reason, title, score, why in skipped:
                lines.append(
                    "• رد شد ({}) — نمره {}".format(esc(reason), fa_num(int(score)))
                )
                lines.append("   " + esc(title[:50]))
                if why:
                    lines.append("   " + esc(why[:120]))
            send_to(chat, "\n".join(lines), False)
    return 0


# ---------------------------------------------------------------- entry

def selftest_run():
    lines = ["<b>خودآزمایی رادار</b>", ""]
    lines.append("توکن: " + ("هست" if TG_TOKEN else "خالی"))
    lines.append("چت کانال: " + esc(TG_CHAT or "خالی"))
    lines.append("چت شخصی: " + esc(TG_ADMIN or "خالی"))
    lines.append("کانال شخصی: " + esc(env_clean("TG_PERSONAL") or "خالی"))
    lines.append("")
    provs = model_providers()
    if not provs:
        lines.append("هیچ لایهٔ مدلی تنظیم نشده")
    for prov in provs:
        ok = False
        errs = []
        last_body = ""
        for name in prov["models"]:
            try:
                if prov.get("type") == "gemini":
                    answer = gemini_call(prov, name, "", "بگو سلام", 45, json_mode=False).strip()[:50]
                else:
                    data = http_post_json(
                        prov["base"] + "/chat/completions",
                        {"model": name, "messages": [{"role": "user", "content": "بگو سلام"}]},
                        headers={"Authorization": "Bearer " + prov["key"]},
                        timeout=45,
                    )
                    answer = (data["choices"][0]["message"].get("content") or "").strip()[:50]
                lines.append("✅ {} — {} — {}".format(prov["name"], esc(name), esc(answer) or "(پاسخ خالی)"))
                ok = True
                break
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", "replace")
                    last_body = re.sub(r"\s+", " ", body)[:150]
                except Exception:
                    last_body = ""
                errs.append("{}:{}".format(name, exc.code))
            except Exception as exc:
                errs.append("{}:{}".format(name, str(exc)[:45]))
        if not ok:
            tail = " | ".join(errs[-3:])
            if last_body:
                tail = tail + " — " + last_body
            lines.append("❌ {} — {}".format(prov["name"], esc(tail)))
        if not ok and prov["name"] == "google":
            try:
                base = prov["base"].replace("/openai", "")
                raw = http_get(base + "/models?key=" + urllib.parse.quote(prov["key"]), timeout=30, headers={"Accept": "application/json"})
                data = json.loads(raw.decode("utf-8", "replace"))
                names = [
                    str(m.get("name", "")).replace("models/", "")
                    for m in data.get("models", [])
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])
                ]
                lines.append("مدل‌های موجود: " + esc("، ".join(names[:12]) if names else "هیچ"))
            except Exception as exc2:
                lines.append("فهرست مدل‌ها نیامد: " + esc(str(exc2)[:90]))
    alert("\n".join(lines))
    log("\n".join(lines))
    return 0



def whoami_run():
    if not TG_TOKEN:
        log("TG_TOKEN خالی است. فایل .env را پر کن.")
        return 1
    me = json.loads(http_get("https://api.telegram.org/bot{}/getMe".format(TG_TOKEN), timeout=20))
    log("بات:", me.get("result", {}).get("username"))
    upd = json.loads(http_get("https://api.telegram.org/bot{}/getUpdates".format(TG_TOKEN), timeout=20))
    seen = set()
    for u in upd.get("result", []):
        for key in ("message", "channel_post", "edited_message", "my_chat_member"):
            chat = (u.get(key) or {}).get("chat")
            if chat and chat["id"] not in seen:
                seen.add(chat["id"])
                log("چت:", chat["id"], "|", chat.get("title") or chat.get("username") or chat.get("first_name"))
    if not seen:
        log("هیچ چتی پیدا نشد. یک پیام در کانال بگذار و دوباره اجرا کن.")
    return 0


def main():
    argv = list(sys.argv[1:])
    # --src مشخص می‌کند تریگر اصلی صدا زده یا تریگر پشتیبان.
    role = "main"
    if "--src" in argv:
        i = argv.index("--src")
        if i + 1 < len(argv):
            role = argv[i + 1]
            del argv[i : i + 2]
    # دسته‌ای که مقایسه رویش انجام می‌شود. خالی یعنی هر سه را نگاه کن و
    # آن را بردار که واقعاً آیتم دارد.
    cmp_kind = ""
    if "--kind" in argv:
        i = argv.index("--kind")
        if i + 1 < len(argv):
            cmp_kind = argv[i + 1]
            del argv[i : i + 2]
    # انتخاب یک مطلب مشخص برای پیش‌نمایش، با تکه‌ای از تیتر یا نشانی.
    pick = ""
    if "--pick" in argv:
        i = argv.index("--pick")
        if i + 1 < len(argv):
            pick = argv[i + 1]
            del argv[i : i + 2]
    cmd = next((a for a in argv if not a.startswith("-")), "news")
    # چند خبر برای مقایسهٔ مدل‌ها.
    cmp_limit = 2
    if "--limit" in argv:
        i = argv.index("--limit")
        if i + 1 < len(argv):
            try:
                cmp_limit = max(1, min(4, int(argv[i + 1])))
            except Exception:
                pass
            del argv[i : i + 2]
    if cmd == "whoami":
        return whoami_run()
    if cmd == "selftest":
        return selftest_run()
    if cmd == "plugins":
        return plugins_report()
    STATE.mkdir(parents=True, exist_ok=True)
    ARCH.mkdir(parents=True, exist_ok=True)
    s = settings()
    st = read_json(STATS, {})
    seen = read_json(SEEN, {"v": 1, "ids": {}})
    reconcile_seen(seen)

    extra = ""
    access = read_optional("access.txt")
    glossary = read_optional("glossary.txt")
    if access:
        extra += "فایل دسترسی (فقط از این نقل کن):\n" + access + "\n"
    if glossary:
        extra += "املای ثابت نام‌ها:\n" + glossary + "\n"

    if cmd == "security":
        security_run(s, st, seen)
    elif cmd == "digest":
        digest_run(s)
    elif cmd == "backlog":
        digest_run(s, hours=48, top_n=10, mark=False)
    elif cmd == "report":
        report_run(s, st)
    elif cmd == "watchdog":
        return watchdog_run()
    elif cmd == "auto":
        auto_run(s, st, seen, extra, role)
    elif cmd == "compare":
        # عمداً زودتر برمی‌گردد: مقایسه نباید آیتم‌ها را «دیده‌شده»
        # ثبت کند، وگرنه همان خبر دیگر هیچ‌وقت پست نمی‌شود.
        return compare_run(s, st, seen, cmp_limit, cmp_kind)
    elif cmd == "preview":
        # پیش‌نمایش هم چیزی را ثبت نمی‌کند و فقط به چت شخصی می‌رود.
        return preview_run(s, st, seen, cmp_kind or "news", pick=pick)
    else:
        # اجرای دسته‌ای هم اول پست‌های نگه‌داشته‌شده را خالی می‌کند.
        publish_ready(s, st, seen)
        model_run(cmd, s, st, seen, extra)

    cutoff = (now() - timedelta(days=180)).date().isoformat()
    seen["ids"] = {k: v for k, v in seen["ids"].items() if v >= cutoff}
    if not DRY:
        write_json(SEEN, seen)
        write_json(STATS, st)
        write_json(LAST, {"at": now().isoformat(timespec="seconds"), "cmd": cmd})
    return 0


if __name__ == "__main__":
    sys.exit(main())
