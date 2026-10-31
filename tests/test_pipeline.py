"""آزمون بقیهٔ موتور: نگهبان، سهمیهٔ روزانه، دروازهٔ کیفیت، سن، اولویت موضوعی.

اجرا: python tests/test_pipeline.py
"""
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402

FAILS = []
COUNT = 0


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILS.append("{} | got={!r} want={!r}".format(label, got, want))


# ---------------------------------------------------------------- نگهبان
tmp = Path(tempfile.mkdtemp()) / "last_start.json"
radar.LAST_START = tmp
radar.DRY = False

s = {"guard_min_gap_min": 10}

# بار اول: فایلی نیست، باید اجرا شود
check("نگهبان: بار اول اجرا می‌کند", radar.guardian_ready(s), True)
# بار دوم بلافاصله: باید رد کند
check("نگهبان: بلافاصله بعدش رد می‌کند", radar.guardian_ready(s), False)

# پنج دقیقه پیش: هنوز رد می‌کند
radar.write_json(tmp, {"at": (radar.now() - timedelta(minutes=5)).isoformat()})
check("نگهبان: پنج دقیقه پیش، رد می‌کند", radar.guardian_ready(s), False)

# دوازده دقیقه پیش: اجرا می‌کند
radar.write_json(tmp, {"at": (radar.now() - timedelta(minutes=12)).isoformat()})
check("نگهبان: دوازده دقیقه پیش، اجرا می‌کند", radar.guardian_ready(s), True)

# فایل خراب: باید اجرا کند، نه اینکه بترکد
tmp.write_text("not json", encoding="utf-8")
check("نگهبان: فایل خراب، اجرا می‌کند", radar.guardian_ready(s), True)

# ---------------------------------------------------------------- نگهبان پشتیبان
tmp2 = Path(tempfile.mkdtemp()) / "last_start.json"
radar.LAST_START = tmp2
sb = {"guard_min_gap_min": 5, "backup_min_gap_min": 12}

radar.write_json(tmp2, {"at": (radar.now() - timedelta(minutes=7)).isoformat(), "src": "main"})
check("پشتیبان: هفت دقیقه بعد از اصلی وارد نمی‌شود", radar.guardian_ready(sb, "backup"), False)
check("اصلی: هفت دقیقه بعد از اصلی وارد می‌شود", radar.guardian_ready(sb, "main"), True)

radar.write_json(tmp2, {"at": (radar.now() - timedelta(minutes=30)).isoformat(), "src": "main"})
check("پشتیبان: سی دقیقه سکوت اصلی، وارد می‌شود", radar.guardian_ready(sb, "backup"), True)
check("پشتیبان: نقشش در فایل ثبت شد", radar.read_json(tmp2, {}).get("src"), "backup")

radar.write_json(tmp2, {"at": (radar.now() - timedelta(minutes=3)).isoformat(), "src": "backup"})
check("اصلی: سه دقیقه بعد از پشتیبان صبر می‌کند", radar.guardian_ready(sb, "main"), False)

# ورکر کلادفلر باید خودش را پشتیبان معرفی کند، وگرنه اجرای اصلی دوباره می‌شود.
worker = Path(__file__).resolve().parent.parent / "trigger" / "worker.js"
src = worker.read_text(encoding="utf-8")
check("تریگر: ورکر خودش را پشتیبان معرفی می‌کند", bool(re.search(r"src\s*:\s*[\"']backup[\"']", src)), True)

# ---------------------------------------------------------------- سهمیهٔ روزانه
sched = {
    "daily_cap": 11,
    "caps": {"news": 4, "tools": 2, "offers": 2, "security": 2, "showcase": 1},
}


def room(kind, kinds):
    d = {"count": sum(int(v) for v in kinds.values()), "kinds": dict(kinds)}
    return radar.daily_room(sched, kind, d)


check("سهمیه: روز خالی، خبر چهار", room("news", {}), 4)
check("سهمیه: روز خالی، ابزار دو", room("tools", {}), 2)
check("سهمیه: خبر چهار پست گذاشته، ابزار دو", room("tools", {"news": 4}), 2)
check(
    "سهمیه: روز پر، نمایشگاه هم یک جا دارد",
    room("showcase", {"news": 4, "tools": 2, "offers": 2, "security": 2}),
    1,
)
check(
    "سهمیه: روز کامل، هیچ‌کس پست نمی‌کند",
    room("news", {"news": 4, "tools": 2, "offers": 2, "security": 2, "showcase": 1}),
    0,
)
check("سهمیه: امنیت در روز پر هم سهمش را دارد", room("security", {"news": 4, "tools": 2, "offers": 2}), 2)
check("سهمیه: دستهٔ ناشناخته هم بی‌سهم نمی‌ماند", room("offers", {"news": 4, "tools": 2}), 2)

# ---------------------------------------------------------------- دروازهٔ کیفیت
ok, _ = radar.quality_ok("تست", "این یک خلاصهٔ کوتاه است.")
check("کیفیت: خلاصهٔ کوتاه رد می‌شود", ok, False)

long_ok = "این ابزار یک صفحه‌ساز تازه است که با آن می‌توانی بدون کدنویسی سایت بسازی و سریع منتشر کنی."
ok, why = radar.quality_ok("صفحه‌ساز تازه", long_ok)
check("کیفیت: خلاصهٔ سالم قبول می‌شود", ok, True)

ok, why = radar.quality_ok("خبر", "در دنیای امروز این ابزار می‌تواند کارهای زیادی انجام دهد و برای همه مفید باشد.")
check("کیفیت: کلیشه رد می‌شود", ok, False)

title = "WordPress 7.1.4 released with security fixes"
echo = title + " را منتشر کرد و حالا در دسترس همه هست."
ok, why = radar.quality_ok(title, echo)
check("کیفیت: بازگویی تیتر رد می‌شود", ok, False)

latin = "این ابزار از Model Context Protocol و OpenAI و Anthropic و Claude و Gemini پشتیبانی می‌کند و کارش خوب است."
ok, why = radar.quality_ok("ابزار", latin)
check("کیفیت: لاتین زیاد رد می‌شود", ok, False)

# ---------------------------------------------------------------- حذف برچسب
raw = "نام محصول: ZCode\nقیمت: رایگان برای همه\nZCode\nاین اعتبار سه روزه است."
check(
    "برچسب: پاک شد",
    radar.clean_details(raw),
    "رایگان برای همه\nاین اعتبار سه روزه است.",
)

check("برچسب: خط فارسی سالم می‌ماند", radar.strip_labels("خلاصه: این یک ابزار است."), "این یک ابزار است.")

check(
    "جزئیات: خط تکراری یکی می‌شود",
    radar.clean_details("نسخه 7.1.4 منتشر شد\nنسخهٔ 7.1.4 منتشر شد\nاین نسخه ۲۲ حفره را می‌بندد."),
    "نسخه 7.1.4 منتشر شد\nاین نسخه ۲۲ حفره را می‌بندد.",
)

# ---------------------------------------------------------------- سن به زبان آدم
check("سن: نیم ساعت", radar.rel_age(0.5), "کمتر از یک ساعت پیش")
check("سن: هجده ساعت", radar.rel_age(18), "۱۸ ساعت پیش")
check("سن: سی ساعت", radar.rel_age(30), "یک روز پیش")
check("سن: هفتاد ساعت", radar.rel_age(70), "۳ روز پیش")

# ---------------------------------------------------------------- اولویت موضوعی
def bonus(title, kind="tools"):
    return radar.topic_bonus({"title": title, "kind": kind}, {"title": title, "summary": title})


check("اولویت: طراحی سایت بالاتر از ویدیو", bonus("AI website builder for WordPress") > bonus("AI video generator"), True)
check("اولویت: محتوا بالاتر از ویدیو", bonus("AI writing assistant for blogs") > bonus("AI video generator"), True)
check("اولویت: طراحی سایت بالاتر از محتوا", bonus("Elementor page builder kit") > bonus("AI copywriting tool"), True)
check("اولویت: خبر اولویت نمی‌گیرد", bonus("AI website builder", kind="news"), 0.0)

# ---------------------------------------------------------------- سن واقعی و امتیاز
now = radar.now()
fresh_item = {"title": "x", "published": (now - timedelta(hours=2)).isoformat(), "tier": 1, "kind": "news"}
old_item = {"title": "x", "published": (now - timedelta(hours=40)).isoformat(), "tier": 1, "kind": "news"}
check("امتیاز: تازه بیشتر از کهنه", radar.code_score(fresh_item, 1) > radar.code_score(old_item, 1), True)
check("امتیاز: دو منبع بیشتر از یک منبع", radar.code_score(fresh_item, 2) > radar.code_score(fresh_item, 1), True)

# ---------------------------------------------------------------- تازگی هشدار امنیتی
# هشدار امنیتی کهنه نباید پست شود، حتی اگر مدل و لینک سالم باشند.
box = Path(tempfile.mkdtemp())
radar.STORIES = box / "stories.json"
radar.SEEN = box / "seen.json"
radar.DAILY = box / "daily.json"
radar.DRY = True

sent = []
radar.send = lambda text: sent.append(text)
radar.link_alive = lambda link: True
radar.archive_row = lambda *a, **k: None
radar.is_recent_story = lambda store, it: False
radar.load_sources = lambda kind: [{"name": "آزمون", "url": "x", "lang": "en", "tier": 1}]
radar.call_models = lambda sysp, prompt: {"items": []}

t0 = radar.now()
sec_items = [
    {"title": "Critical RCE in WordPress core", "desc": "CVE-2026-1111 patched", "link": "https://a.test/fresh",
     "published": t0 - timedelta(hours=2)},
    {"title": "Critical RCE in Elementor", "desc": "CVE-2026-2222 patched", "link": "https://a.test/mid",
     "published": t0 - timedelta(hours=30)},
    {"title": "Critical XSS in a plugin", "desc": "CVE-2026-3333 patched", "link": "https://a.test/old",
     "published": t0 - timedelta(hours=70)},
]
radar.fetch_source = lambda src, st: sec_items

sec_settings = {"security_max_age_h": 24, "daily_cap": 11, "caps": {"security": 2}}
n = radar.security_run(sec_settings, {}, {"v": 1, "ids": {}})
check("امنیت: فقط هشدار تازه پست شد", n, 1)
check("امنیت: یک پیام فرستاده شد", len(sent), 1)
check("امنیت: هشدار دو ساعته رفت", "a.test/fresh" in sent[0], True)
check("امنیت: هشدار سی ساعته رد شد", "a.test/mid" in sent[0], False)
check("امنیت: هشدار هفتاد ساعته رد شد", "a.test/old" in sent[0], False)
check("امنیت: هشدار کهنه هیچ پیامی نساخت", any("a.test/old" in t for t in sent), False)

# سقف روزانه: هشدار امنیتی هم باید بودجه را رعایت کند.
radar.DRY = False
radar.write_json(radar.DAILY, {"date": radar.today(), "count": 11, "kinds": {}})
sent.clear()
n = radar.security_run(sec_settings, {}, {"v": 1, "ids": {}})
check("امنیت: روز پر، پستی نمی‌رود", n, 0)
check("امنیت: روز پر، پیامی ساخته نشد", len(sent), 0)

# سقف دسته: حداکثر دو هشدار در روز.
radar.write_json(radar.DAILY, {"date": radar.today(), "count": 2, "kinds": {"security": 2}})
sent.clear()
n = radar.security_run(sec_settings, {}, {"v": 1, "ids": {}})
check("امنیت: سقف دوتایی دسته رعایت شد", n, 0)

# ---------------------------------------------------------------- برچسب درست
def lab(kind, text, model_label="", urgent=False):
    return radar.classify_label(kind, text, model_label, urgent)


check("برچسب: خبر نسخه‌دار آپدیت است", lab("news", "WordPress 7.1.4 released with security fixes"), "آپدیت")
check("برچسب: خبر معمولی خبر است", lab("news", "گوگل سیاست تازه‌ای برای محتوای هوش مصنوعی گذاشت"), "خبر")
check(
    "برچسب: آسیب‌پذیری فعال فوری است",
    lab("news", "Critical vulnerability patched in WordPress core, actively exploited in the wild"),
    "فوری",
)
check(
    "برچسب: آسیب‌پذیری وردپرس هشدار است",
    lab("news", "Critical vulnerability patched in a WordPress plugin"),
    "هشدار امنیتی",
)
check(
    "برچسب: ابزار خبر نمی‌شود",
    lab("tools", "AI video generator for social media", "خبر"),
    "ابزار",
)
check(
    "برچسب: ابزار سالم ابزار می‌ماند",
    lab("tools", "Cloudflare Workers AI free daily allowance", "ابزار"),
    "ابزار",
)
check(
    "برچسب: افزونهٔ تازه پذیرفته می‌شود",
    lab("tools", "افزونهٔ تازه برای ساخت فرم در وردپرس معرفی شد", "پلاگین تازه"),
    "پلاگین تازه",
)
check(
    "برچسب: افزونهٔ قدیمی «تازه» نیست",
    lab("tools", "افزونهٔ فرم ساز نسخهٔ ۲.۱ منتشر شد", "پلاگین تازه"),
    "ابزار",
)
check("برچسب: آموزش پذیرفته می‌شود", lab("tools", "آموزش ساخت فروشگاه با وردپرس", "آموزش"), "آموزش")

# همان پستی که در کانال آمد: قالب تازه، نه آپدیت. «نسخهٔ فارسی» نسخهٔ تازه نیست.
theme = (
    "راه‌اندازی سریع سایت با قالب رایگان و فارسی. این قالب با صفحه‌ساز هماهنگ است. "
    "نسخه فارسی شده دارای فونت و راست‌چین است."
)
check("برچسب: قالب تازه آپدیت نمی‌شود", lab("news", theme, "پلاگین تازه"), "پلاگین تازه")
check("برچسب: «نسخهٔ فارسی» آپدیت نمی‌سازد", lab("news", theme, "خبر"), "خبر")
check("برچسب: نسخهٔ عددی آپدیت است", lab("news", "وردپرس ۷.۱.۴ منتشر شد", "خبر"), "آپدیت")
check("برچسب: فرصت همیشه فرصت است", lab("offers", "پلن رایگان دائمی", "خبر"), "فرصت")
check("برچسب: امنیت فوری", lab("security", "critical RCE exploited", "", True), "فوری")
check("برچسب: امنیت عادی", lab("security", "patched XSS in a plugin", ""), "هشدار امنیتی")

# همان موردی که در آرشیو غلط برچسب خورده بود: خبر هوش مصنوعی با برچسب امنیتی.
glm = "Open-weight model GLM 5.3 built full Firefox exploit chains with zero human input in under 48 hours"
check("برچسب: خبر هوش مصنوعی هشدار امنیتی نمی‌شود", lab("tools", glm, "هشدار امنیتی"), "ابزار")
check("برچسب: همان متن در خبر هم هشدار نمی‌شود", lab("news", glm, "هشدار امنیتی") != "هشدار امنیتی", True)

# هیچ برچسبی نباید از فهرست دستهٔ خودش بیرون بزند.
samples = [
    ("news", "WordPress 7.1.4 released"),
    ("news", "Critical vulnerability patched in WordPress core"),
    ("news", "گوگل سیاست تازه‌ای گذاشت"),
    ("tools", "افزونهٔ تازه معرفی شد"),
    ("tools", "آموزش ساخت سایت"),
    ("tools", "Cloudflare Workers AI"),
    ("offers", "پلن رایگان"),
    ("security", "patched XSS"),
    ("showcase", "سایت ساخته‌شده با المنتور"),
]
outside = []
for kind, text in samples:
    got = radar.classify_label(kind, text, "خبر")
    if got not in radar.LABELS[kind]:
        outside.append("{} → {}".format(kind, got))
check("برچسب: هیچ برچسبی بیرون از فهرست دسته نیست", outside, [])

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
