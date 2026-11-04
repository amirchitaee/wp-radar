"""آزمون بقیهٔ موتور: نگهبان، سهمیهٔ روزانه، دروازهٔ کیفیت، سن، اولویت موضوعی.

اجرا: python tests/test_pipeline.py
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402

# نسخهٔ اصلی این دو تابع را نگه می‌داریم. آزمون‌های بعدی جایشان نسخهٔ
# ساختگی می‌گذارند تا شبکه نخوانند؛ ولی آزمون فهرست فایل باید فایل
# واقعی را ببیند.
REAL_LOAD_SOURCES = radar.load_sources
REAL_LOAD_PLUGIN_WATCH = radar.load_plugin_watch

FAILS = []
COUNT = 0


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILS.append("{} | got={!r} want={!r}".format(label, got, want))


# ---------------------------------------------------------------- جابه‌جایی دسته
# وقتی فیدی از یک دسته به دستهٔ دیگر می‌رود، آیتم‌های قدیمی همان فید هم
# باید بروند؛ وگرنه یک خبر تا چند روز در دستهٔ ابزار می‌ماند و آن‌جا
# به‌خاطر «ابزار نبودن» رد می‌شود. این تست باید قبل از هر جای‌گزینی
# load_sources بیاید.
_qsave = radar.QUEUE.read_text(encoding="utf-8") if radar.QUEUE.exists() else "{}"
radar.QUEUE.write_text(
    json.dumps({"v": 1, "items": [{
        "id": "m1", "title": "t", "link": "https://example.com/x", "desc": "",
        "source": "وردپرس", "kind": "tools", "added": radar.now().isoformat(timespec="seconds"),
    }]}, ensure_ascii=False),
    encoding="utf-8",
)
_was_dry = radar.DRY
radar.DRY = False
_q, _ = radar.queue_merge([], {"queue_days": 7})
radar.DRY = _was_dry
check("دسته: آیتم فید جابه‌جاشده دنبال فید می‌رود", _q["items"][0]["kind"], "news")
radar.QUEUE.write_text(_qsave, encoding="utf-8")

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

# «یک مهندس می‌گوید...» یعنی هیچ؛ اسم باید در تیتر باشد.
vague = "این مهندس در مقاله‌ای تازه گفته ترس از بیکاری با هوش مصنوعی همان ترس قدیمی بازرگان‌هاست و کار بیشتر می‌شود."
ok, why = radar.quality_ok("یک مهندس می‌گوید هوش مصنوعی شغل‌ها را نمی‌خورد", vague)
check("کیفیت: سوژهٔ مبهم در تیتر رد می‌شود", ok, False)
ok, why = radar.quality_ok("ونکاتش رائو می‌گوید هوش مصنوعی شغل‌ها را نمی‌خورد", vague)
check("کیفیت: تیتر با اسم واقعی قبول می‌شود", ok, True)
ok, why = radar.quality_ok("به گفتهٔ یک شرکت، ابزار تازه‌ای برای ساخت سایت آمده است", vague)
check("کیفیت: «به گفتهٔ یک شرکت» هم رد می‌شود", ok, False)

# ---------------------------------------------------------------- کالای فارسی
# فهرست رقیب خالی است؛ قاعدهٔ کلی کار می‌کند: هر قالب و افزونهٔ فارسی.
check(
    "کالای فارسی: قالب با دامنهٔ ایرانی گرفته می‌شود",
    radar.persian_product({"title": "قالب فروشگاهی تازه", "link": "https://shop.example.ir/theme"}),
    True,
)
check(
    "کالای فارسی: منبع فارسی با حرف کالا گرفته می‌شود",
    radar.persian_product(
        {"title": "افزونهٔ فرم تماس منتشر شد", "link": "https://fa.example.com/p/1", "lang": "fa"}
    ),
    True,
)
check(
    "کالای فارسی: قالب خارجی گرفته نمی‌شود",
    radar.persian_product({"title": "قالب تازه برای وردپرس", "link": "https://wptavern.com/x"}),
    False,
)
check(
    "کالای فارسی: خبر غیرکالایی از دامنهٔ ایرانی رد نمی‌شود",
    radar.persian_product({"title": "کنفرانس وردپرس ایران برگزار شد", "link": "https://news.example.ir/x"}),
    False,
)

# برچسب دسته نباید کورکورانه بچسبد: خبر انویدیا با #وردپرس رفت.
check("برچسب: خبر وردپرس", radar.kind_tag_for("news", "وردپرس ۶.۹ منتشر شد"), "#وردپرس")
check("برچسب: خبر هوش مصنوعی", radar.kind_tag_for("news", "انویدیا کتابخانه‌ای تازه منتشر کرد"), "#هوش_مصنوعی")
check("برچسب: ابزار", radar.kind_tag_for("tools", "هر چه"), "#هوش_مصنوعی")

# ربط ساختگی: جمله‌ای که مدل به خبر می‌چسباند تا به کانال مربوط شود.
_fake = "این تصمیم برای کسانی که با این ابزارها سایت می‌سازند مهم است."
check(
    "ربط: خبر گلدمن با جملهٔ ساختگی رد می‌شود",
    radar.relevance_ok(_fake, "گلدمن ساکس دسترسی معامله‌گران را قطع کرد"),
    False,
)
check(
    "ربط: همان جمله وقتی منبع از سایت گفته، قبول است",
    radar.relevance_ok(_fake, "قالب تازه برای ساخت سایت وردپرس"),
    True,
)
check("ربط: پست سالم دست‌نخورده می‌ماند", radar.relevance_ok("قالب تازه‌ای منتشر شد.", "هر چیز"), True)

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
_real_is_recent_story = radar.is_recent_story
radar.is_recent_story = lambda store, it: False
radar.load_sources = lambda kind: [{"name": "آزمون", "url": "x", "lang": "en", "tier": 1}]
radar.call_models = lambda sysp, prompt, **kw: {"items": []}

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
    return radar.classify_label(kind, text, model_label, urgent, title=text)


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
check("برچسب: قالب تازه آپدیت نمی‌شود", lab("news", theme, "پلاگین تازه"), "خبر")
check("برچسب: «نسخهٔ فارسی» آپدیت نمی‌سازد", lab("news", theme, "خبر"), "خبر")

# قالب، پلاگین نیست. این انتظار قبلاً «پلاگین تازه» بود و همان شد که
# قالب رایگان Basin با برچسب «پلاگین تازه» در کانال منتشر شد.
check("برچسب: قالب هرگز پلاگین تازه نیست", lab("news", theme, "پلاگین تازه"), "خبر")
check(
    "برچسب: افزونه هنوز پلاگین تازه است",
    lab("news", "افزونهٔ فرم ساز تازه برای وردپرس منتشر شد", "پلاگین تازه"),
    "پلاگین تازه",
)
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

# ---------------------------------------------------------------- بودجهٔ جمع‌آوری
# یک منبع کند نباید کل اجرا را بخورد و وقت مدل را بگیرد.
fetched = []
radar.load_sources = lambda kind: [
    {"name": "s{}".format(i), "url": "u{}".format(i), "lang": "en", "tier": 1} for i in range(6)
]


def slow_fetch(src, st):
    fetched.append(src["url"])
    time.sleep(0.05)
    return []


radar.fetch_source = slow_fetch
rows = radar.collect("news", {}, {"v": 1, "ids": {}}, budget_sec=0.12, watch=False)
check("جمع‌آوری: با بودجهٔ کم همهٔ منابع خوانده نمی‌شوند", len(fetched) < 6, True)
check("جمع‌آوری: دست‌کم یک منبع خوانده می‌شود", len(fetched) >= 1, True)

# ---------------------------------------------------------------- باگ‌های ۱۰ آبان
# «WooCommerce» کلمهٔ RCE را در خود دارد. نباید هشدار امنیتی بسازد.
check("هشدار: WooCommerce آسیب‌پذیری نیست", radar.is_advisory("activate WooCommerce and add products"), False)
check("هشدار: RCE واقعی شناخته می‌شود", radar.is_advisory("Critical RCE in the login form"), True)
check("هشدار: CVE واقعی شناخته می‌شود", radar.is_advisory("CVE-2026-1234 in core"), True)

divi_title = "How To Build A Product Grid With Divi 5’s CSS Grid Editor"
divi_text = (
    divi_title
    + " یک راهنمای عملی برای چیدمان محصولات فروشگاهی. با Loop Builder برای ۱۹ محصول "
    "WooCommerce تکرارش کن. نسخهٔ جدید این قالب را نصب کن."
)
check("برچسب: آموزش دیوی هشدار امنیتی نمی‌گیرد", lab("news", divi_text, "هشدار امنیتی"), "آموزش")
check("برچسب: آموزش دیوی پلاگین تازه نمی‌گیرد", lab("news", divi_text, "پلاگین تازه"), "آموزش")
check("برچسب: تیتر چطور = آموزش", lab("news", "چطور سایت را سریع‌تر کنیم", "خبر"), "آموزش")

# فرصت بی‌ربط نباید رد شود.
check("فرصت: بازی تخته‌ای رد می‌شود", bool(radar.OFFER_JUNK.search("Play Nightmare – the 1991 VHS board game")), True)
check("فرصت: موضوع هوش مصنوعی قبول است", bool(radar.OFFER_TOPIC.search("free daily quota for developers")), True)
check("فرصت: فیلم و موسیقی رد می‌شود", bool(radar.OFFER_JUNK.search("free music album download")), True)

# «نامشخص» فیلد پوک است.
check("پوک: وضعیت نامشخص رد می‌شود", bool(radar.HOLLOW_RE.search("وضعیت دسترسی: نامشخص")), True)
check("پوک: متن سالم رد نمی‌شود", bool(radar.HOLLOW_RE.search("با ایمیل ثبت‌نام کن و اعتبار را بگیر")), False)

# نام محصول: ساختگی رد، واقعی قبول، و اگر مدل نداد از تیتر برداشته می‌شود.
row = {"title": "Cloudflare Workers AI free daily allowance", "desc": "free quota"}
check("نام: نام ساختگی رد می‌شود", radar.valid_name("OpenAI Turbo Max", row), "")
check("نام: نام واقعی قبول می‌شود", radar.valid_name("Cloudflare", row), "Cloudflare")
check("نام: از تیتر منبع برداشته می‌شود", radar.auto_name(row), "Cloudflare")
check("نام: واژهٔ رایج اسم نیست", radar.auto_name({"title": "Show HN: something"}), "")
check("نام: تیتر بی‌لاتین نامی ندارد", radar.auto_name({"title": "سایت تازه برای ساخت فروشگاه"}), "")

# سقف منبع حالا برای هر دسته جداست.
news_rows = [{"source": "الگانت", "title": "a"}, {"source": "الگانت", "title": "b"}, {"source": "x", "title": "c"}]
check("سقف منبع: خبر یک پست از هر منبع", len(radar.cap_per_source(news_rows, 1)), 2)
check("سقف منبع: ابزار دو پست از هر منبع", len(radar.cap_per_source(news_rows, 2)), 3)

# ---------------------------------------------------------------- لینک استفاده
# لینک خاص‌تر باید برنده شود، نه کوتاه‌ترین. قبلاً برای یک قالب، ریشهٔ
# سایت به‌جای صفحهٔ خود قالب انتخاب می‌شد.
_HTML = (
    '<a href="https://wordpress.org/">root</a>'
    '<a href="https://twitter.com/x">tw</a>'
    '<a href="https://wordpress.org/themes/basin/">theme</a>'
    '<a href="https://wordpress.org/themes/">list</a>'
)
check(
    "لینک استفاده: صفحهٔ قالب بر ریشه ترجیح داده می‌شود",
    radar.extract_use_link("https://example.com/blog/basin", _HTML),
    "https://wordpress.org/themes/basin/",
)
check(
    "لینک استفاده: مخزن گیت‌هاب بر سازمان ترجیح داده می‌شود",
    radar.extract_use_link(
        "https://example.com/x",
        '<a href="https://github.com/acme">o</a><a href="https://github.com/acme/tool">r</a>',
    ),
    "https://github.com/acme/tool",
)

# ---------------------------------------------------------------- نام تکراری
# «WordPress — وردپرس» تکرار است و نباید ساخته شود.
check(
    "قالب پست: نام تکراری چسبانده نمی‌شود",
    radar.format_post(
        "آپدیت", "وردپرس ۷.۱.۴ منتشر شد", "خلاصه", "جزئیات", "", "", "", None, name="WordPress"
    ).split("\n")[0],
    "<b>🔄 آپدیت: وردپرس ۷.۱.۴ منتشر شد</b>",
)

# ---------------------------------------------------------------- لینک تبلیغاتی
# یک بار لینک نت‌لیفای با پارامتر ردیابی به‌عنوان «جایی که ابزار را
# می‌گیری» به مخاطب نشان داده شد. لینک پشتیبان، لینک استفاده نیست.
check(
    "لینک استفاده: دامنهٔ پشتیبان انتخاب نمی‌شود",
    radar.extract_use_link(
        "https://example.com/blog/theme",
        '<a href="https://wordpress.org/themes/basin/">t</a>'
        '<a href="https://www.netlify.com/?utm_source=link&utm_medium=parter">n</a>',
    ),
    "https://wordpress.org/themes/basin/",
)
check(
    "لینک استفاده: اگر فقط لینک تبلیغاتی بود، خالی برمی‌گردد",
    radar.extract_use_link(
        "https://example.com/blog/x",
        '<a href="https://www.netlify.com/?utm_source=link">n</a>'
        '<a href="https://vercel.com/partner">v</a>',
    ),
    "",
)

# ---------------------------------------------------------------- خبرنما
# دو پستی که کاربر به‌حق به آن‌ها ایراد گرفت، هر دو گزارش داخلی تیم
# توسعه بودند: «به‌روزرسانی هفتگی» و «خلاصهٔ جلسه». این‌ها نباید به
# مدل برسند، چه در تیتر باشند چه در آدرس.
def non_news(text):
    return bool(radar.NON_NEWS_RE.search(text) or radar.JUNK_NEWS_RE.search(text))


for bad in (
    "WP Feature Notifications — Weekly Update",
    "Presence API — Meeting Summary",
    "Dev Chat Agenda for November 5",
    "Core Team Update: What We Shipped This Week",
    "Call for Testing: New Media Modal",
    "Performance Team Bi-Weekly Chat",
    "Plugin Review Team Changes",
    "https://make.wordpress.org/core/2026/11/01/weekly-update/",
    "https://make.wordpress.org/core/2026/11/01/meeting-summary/",
):
    check("خبرنما رد می‌شود: " + bad[:44], non_news(bad), True)

for good in (
    "WordPress 7.1.4 Released",
    "WordPress 7.2 Beta 1",
    "Gutenberg 20.3 Released",
    "Security Release: WordPress 6.9.2",
    "WordPress 7.1.4 Patches Critical RCE",
    "Divi 5.2 Adds AI Layout Builder",
    "Cloudflare Workers AI Adds Free Tier",
    "Introducing the Block Bindings API",
):
    check("خبر واقعی رد نمی‌شود: " + good[:44], non_news(good), False)

# ---------------------------------------------------------------- حرف خودش
# یک بار مدل در خلاصهٔ پست نوشت «این مطلب ارتباطی با وردپرس یا ساخت
# سایت ندارد» و باز هم پست شد. نمره‌اش قابل اعتماد نبود، حرفش هست.
def offtopic(text):
    return bool(radar.OFFTOPIC_SELF_RE.search(text))


for said in (
    "این مطلب ارتباطی با وردپرس یا ساخت سایت ندارد.",
    "موضوع کاملاً خارج از حوزه وردپرس و توسعه وب فارسی است.",
    "این خبر بی‌ربط است.",
    "به کار مخاطب ربطی ندارد.",
):
    check("حرف خود مدل جدی گرفته می‌شود: " + said[:34], offtopic(said), True)

for said in (
    "قالب به‌صورت رایگان در مخزن وردپرس موجود است.",
    "این نسخه دو آسیب‌پذیری را می‌بندد.",
    "افزونه با وردپرس ۶.۹ سازگار است.",
    "ظاهر مدرنی دارد و بدون تنظیمات پیچیده شخصی می‌شود.",
):
    check("متن سالم بی‌دلیل رد نمی‌شود: " + said[:34], offtopic(said), False)

# ---------------------------------------------------------------- عدد از مدل
# مدل جای عدد رشته می‌گذارد. یک رشته نباید کل اجرا را از کار بیندازد.
check("عدد: رشتهٔ عددی", radar.num("12"), 12.0)
check("عدد: رشتهٔ نامعتبر", radar.num("بالا"), 0.0)
check("عدد: خالی", radar.num(None), 0.0)
check("عدد: عدد اعشاری", radar.num(7.5), 7.5)

# ---------------------------------------------------------------- فهرست پیوند
# مخزن awesome فهرست جمع می‌کند، ابزار نمی‌سازد. یک بار چنین فهرستی در
# دستهٔ ابزار پست شد و خود مدل نوشت «این یک لیست مرجعی است، نه ابزاری».
for repo in (
    "https://github.com/reacher-z/Awesome-REA",
    "Awesome-REA: مجموعه ابزارها",
    "awesome-mcp-servers",
):
    check("فهرست پیوند رد می‌شود: " + repo[:40], bool(radar.LIST_REPO_RE.search(repo)), True)

check(
    "فهرست پیوند: عبارت معمولی رد نمی‌شود",
    bool(radar.LIST_REPO_RE.search("Awesome new AI tool from Google")),
    False,
)

check(
    "حرف خود مدل: لیست مرجعی ابزار نیست",
    bool(radar.NOT_A_TOOL_SELF_RE.search("این یک لیست مرجعی است، نه ابزاری برای اجرا.")),
    True,
)
check(
    "حرف خود مدل: ابزار واقعی رد نمی‌شود",
    bool(radar.NOT_A_TOOL_SELF_RE.search("این ابزار دستورات شل را بررسی می‌کند.")),
    False,
)

# ---------------------------------------------------------------- طول کتیشن
check(
    "کتیشن: بیش از چهار خط کوتاه می‌شود",
    radar.trim_details("\n".join("نکته {}".format(i) for i in range(1, 9))),
    "نکته 1\nنکته 2\nنکته 3\nنکته 4 …",
)
check("کتیشن: سه خط دست‌نخورده می‌ماند", radar.trim_details("یک\nدو\nسه"), "یک\nدو\nسه")

# ---------------------------------------------------------------- متن مدل
# صفحهٔ مخزن گیت‌هاب با نوار سایت شروع می‌شود. اگر همان به مدل برسد،
# مدل هیچ پستی نمی‌نویسد و کانال خالی می‌ماند. این تست همان شکست را
# می‌گیرد: استخراج باید از بلوک article بخواند.
GITHUB_PAGE = (
    "<html><body>"
    "<p>You signed in with another tab or window. Reload to refresh your session.</p>"
    "<nav>Branches Tags Go to file Code</nav>"
    "<article><p>Magic turns plain English into a working full-stack app.</p>"
    "<p>It runs on your own hardware with zero lock-in.</p></article>"
    "<footer>Terms Privacy</footer></body></html>"
)


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_get = radar.http_get
radar.http_get = lambda url, timeout=15: GITHUB_PAGE.encode("utf-8")
text, use = radar.fetch_article("https://github.com/acme/magic")
check("متن: نوار سایت به مدل نمی‌رسد", "You signed in" in text, False)
check("متن: متن اصلی مقاله می‌آید", "full-stack app" in text, True)
check("متن: صفحهٔ محصول لینک استفاده نمی‌سازد", use, "")

# اگر متن کامل با این حال پوسته بود، توضیح فید جای آن را می‌گیرد.
row = {"full": "You signed in with another tab or window. " * 20, "desc": "توضیح کوتاه فید."}
check("متن: پوسته جایش را به توضیح فید می‌دهد", radar.item_text(row), "توضیح کوتاه فید.")

# لینک استفاده نباید از خودش دربیاید. قبلاً اولین لینک خارجی برگردانده
# می‌شد و پست به پروفایل XFN و ریشهٔ وردپرس رسیده بود.
check(
    "لینک: هیچ لینک اختراعی ساخته نمی‌شود",
    radar.extract_use_link(
        "https://the-decoder.com/x",
        '<a href="https://gmpg.org/xfn/11">x</a><a href="https://v0.wordpress.com">y</a>',
    ),
    "",
)
check(
    "لینک: صفحهٔ محصول همچنان پیدا می‌شود",
    radar.extract_use_link(
        "https://wptavern.com/basin",
        '<a href="https://wordpress.org/themes/basin/">t</a>',
    ),
    "https://wordpress.org/themes/basin/",
)
radar.http_get = _real_get

# ---------------------------------------------------------------- بازگویی
# خلاصه‌ای که فقط تیتر را بلندتر می‌کند بازگویی است؛ خلاصه‌ای که واقعیت
# تازه می‌آورد نه. تفاوتش در واژه‌های محتوایی است، نه در شمار واژه‌ها.
ok, why = radar.quality_ok(
    "Magic با یک جمله، اپ کامل می‌سازد",
    "Magic جایگزین متن‌باز لاوابل و رپلیت است که با توضیح انگلیسی، اپ کامل با دیتابیس و رابط کاربری می‌سازد.",
)
check("بازگویی: خلاصهٔ پرمحتوا رد نمی‌شود", ok, True)

# ---------------------------------------------------------------- موضوع ابزار
check("موضوع: بازی هکرنیوز ابزار کانال نیست", bool(radar.BUILDER_TOPIC.search(
    "Show HN: Play Nightmare – the 1991 VHS board game")), False)
check("موضوع: آموزش دیوی ابزار است", bool(radar.BUILDER_TOPIC.search(
    "How To Build A Product Grid With Divi 5 CSS Grid Editor")), True)

# ---------------------------------------------------------------- لینک به نام
# مقاله‌ای دربارهٔ یک مدل تازه، لینک اقدامش صفحهٔ مدل دیگری شد که فقط در
# متن نامش آمده بود. آدرس باید با نام خود محصول بخواند.
check(
    "لینک: نام محصول در آدرس اولویت دارد",
    radar.extract_use_link(
        "https://www.marktechpost.com/x/hydrogen",
        '<a href="https://huggingface.co/zai-org/GLM-5.3">g</a>'
        '<a href="https://huggingface.co/DeepPrinciple/Hydrogen-1.0">h</a>',
        "Hydrogen-1.0 با ۷۵۳ میلیارد پارامتر منتشر شد",
    ),
    "https://huggingface.co/DeepPrinciple/Hydrogen-1.0",
)
check(
    "لینک: محصول دیگر جای محصول اصلی نمی‌نشیند",
    radar.extract_use_link(
        "https://www.marktechpost.com/x/hydrogen",
        '<a href="https://huggingface.co/zai-org/GLM-5.3">g</a>',
        "Hydrogen-1.0 منتشر شد",
    ),
    "",
)
check(
    "لینک: مخزن بی‌ربط رد می‌شود",
    radar.extract_use_link(
        "https://example.com/post",
        '<a href="https://github.com/ax/apk.sh">a</a>',
        "HOL-Guard — ابزار امنیتی",
    ),
    "",
)
check(
    "لینک: مخزن هم‌نام پذیرفته می‌شود",
    radar.extract_use_link(
        "https://example.com/post",
        '<a href="https://github.com/some/hol-guard">a</a>',
        "HOL-Guard — ابزار امنیتی",
    ),
    "https://github.com/some/hol-guard",
)
# صفحهٔ گزارش تخلف و شکایت، جای محصول نیست. یک بار خط «جزئیات بیشتر»
# به آدرس گزارش تخلف وردپرس رسید، چون در فوتر صفحه بود.
check(
    "لینک: صفحهٔ گزارش تخلف انتخاب نمی‌شود",
    radar.extract_use_link(
        "https://example.com/post",
        '<a href="https://github.com/report/hol-guard">a</a>'
        '<a href="https://github.com/some/hol-guard">b</a>',
        "HOL-Guard — ابزار امنیتی",
    ),
    "https://github.com/some/hol-guard",
)

# ---------------------------------------------------------------- یک داستان
# دو نشریه یک رویداد را با واژه‌های مختلف بازنویسی می‌کنند. نام‌های خاص
# یکی است، پس داستان یکی است.
check(
    "داستان: بازنویسی دو نشریه یکی است",
    radar.same_story(
        {"title": "New York Times and other outlets sue web browser Brave over alleged content scraping"},
        {"title": "The New York Times sues Brave web browser for AI scraping and bypassing paywalls"},
    ),
    True,
)
check(
    "داستان: دو خبر جدا از یک شرکت یکی نیست",
    radar.same_story(
        {"title": "Google launches Gemini 4 Carbon for long engineering tasks"},
        {"title": "Google updates Gemini in Workspace with new sidebar"},
    ),
    False,
)
# سیزدهم آبان یک اطلاعیهٔ CISA دربارهٔ وردپرس دو بار پست شد، سه ثانیه
# فاصله. دو تیتر واژه‌هایشان یکی نبود، ولی مرجعشان یکی بود.
check(
    "داستان: دو تیتر با مرجع CISA یکی است",
    radar.same_story(
        {"title": "CISA flags WordPress RCE flaw as actively exploited in attacks"},
        {"title": "CISA Confirms Hackers Exploiting WordPress Core Flaw During Software Upgrades"},
    ),
    True,
)
check(
    "داستان: دو CVE مختلف یکی نیست",
    radar.same_story(
        {"title": "CVE-2026-1111 hits LiteSpeed Cache"},
        {"title": "CVE-2026-2222 hits Rank Math"},
    ),
    False,
)

# ---------------------------------------------------------------- یک CVE
# یک حفره با چند مقاله و چند تیتر، هنوز یک حفره است.
check("CVE: شناسه یک‌شکل می‌شود", radar.cve_ids("CVE‑2026‑111497"), {"CVE-2026-111497"})
check(
    "CVE: حفرهٔ تکراری با تیتر تازه رد می‌شود",
    _real_is_recent_story(
        {"items": [{"t": radar.now().isoformat(timespec="seconds"),
                    "title": "WordPress doesn't ship Brotli. It's still exposed",
                    "kind": "security", "cves": ["CVE-2026-111497"]}]},
        {"title": "HTTP2Shell: What the Critical Brotli Vulnerability Means",
         "desc": "یک حفرهٔ بحرانی در بروتلی، شناسه CVE-2026-111497",
         "kind": "security"},
    ),
    True,
)

# ---------------------------------------------------------------- صفحهٔ گردآوری
check(
    "گردآوری ماهانه: صفحهٔ «اخبار ماه» خبر نیست",
    bool(radar.NON_NEWS_RE.search("The latest AI news we announced in October 2026")),
    True,
)
check(
    "گردآوری ماهانه: خبر واقعی رد نمی‌شود",
    bool(radar.NON_NEWS_RE.search("WordPress 6.9 released with new features")),
    False,
)

# ---------------------------------------------------------------- برچسب
# تولد یک محصول تازه خبر است، نه آپدیت. قبلاً هر «۱.۰» در متن کافی بود.
check(
    "برچسب: انتشار مدل تازه خبر است نه آپدیت",
    radar.classify_label(
        "news",
        "مدل علمی Hydrogen-1.0 با وزن باز منتشر شد. مجوز MIT دارد.",
        "",
        False,
        "مدل علمی Hydrogen-1.0 منتشر شد",
    ),
    "خبر",
)
check(
    "برچسب: نسخهٔ تازهٔ افزونهٔ موجود آپدیت است",
    radar.classify_label(
        "news", "افزونهٔ جت‌پک به‌روزرسانی شد و نسخهٔ ۱۲.۴ منتشر شد.", "", False
    ),
    "آپدیت",
)
check(
    "برچسب: کلمهٔ بحرانی به‌تنهایی فوری نمی‌سازد",
    radar.classify_label("security", "آسیب‌پذیری بحرانی Brotli در وردپرس کشف شد.", "", False),
    "هشدار امنیتی",
)

# ---------------------------------------------------------------- ساعت انتشار
# کانال برای مخاطب ایرانی است؛ پست ساعت سه بامداد دیده نمی‌شود. متن
# شب نوشته می‌شود و صبح با فاصله منتشر می‌شود.
_pub = {"caps": {"news": 4}, "daily_cap": 11, "max_posts_per_run": 4,
        "publish": {"enabled": True, "start": "08:00", "end": "23:30",
                    "gap_min": 70, "max_ready": 2, "max_hold_h": 30}}
check("انتشار: سه بامداد تعطیل است",
      radar.in_publish_window(_pub, datetime(2026, 11, 3, 3, 0, tzinfo=radar.TZ)), False)
check("انتشار: ده صبح باز است",
      radar.in_publish_window(_pub, datetime(2026, 11, 3, 10, 0, tzinfo=radar.TZ)), True)
check("انتشار: باز شدن بعدی فردا صبح است",
      radar.next_window_start(_pub, datetime(2026, 11, 3, 23, 45, tzinfo=radar.TZ)).strftime("%m-%d %H:%M"),
      "11-04 08:00")

_hold_dir = tempfile.mkdtemp()
radar.READY = Path(_hold_dir) / "ready.json"
radar.QUEUE = Path(_hold_dir) / "queue.json"
radar.DAILY = Path(_hold_dir) / "daily.json"
radar.SCHED = Path(_hold_dir) / "schedule.json"
radar.STORIES = Path(_hold_dir) / "stories.json"
radar.og_image = lambda url: ""
_hold_item = {"id": "hold-1", "title": "خبر آزمون", "link": "https://example.com/x",
              "kind": "news", "source": "آزمون"}
radar.write_json(radar.QUEUE, {"v": 1, "items": [dict(_hold_item)]})
radar.write_json(radar.DAILY, {"date": radar.today(), "count": 0, "kinds": {}})
check("انتشار: نگه‌داشتن انجام شد",
      radar.hold_ready(_hold_item, "خبر", "<b>متن آزمون</b>", 80, "news"), True)
check("انتشار: آیتم علامت نگه‌داشته گرفت", _hold_item.get("held"), True)
# برای آزمون خودِ انتشار، پنجره‌ای می‌سازیم که همین حالا باز باشد؛
# وگرنه آزمون به ساعت واقعی وابسته می‌شود.
_pub_now = dict(_pub)
_pub_now["publish"] = {"enabled": True, "start": "00:00", "end": "23:59",
                       "gap_min": 0, "max_ready": 2, "max_hold_h": 30}
check("انتشار: یک پست از نگه‌داشته رفت",
      radar.publish_ready(_pub_now, {}, {"v": 1, "ids": {}}), 1)
check("انتشار: انبار نگه‌داشته خالی شد",
      radar.read_json(radar.READY, {"items": []})["items"], [])
check("انتشار: پست در کانال ثبت شد", sent[-1] if sent else None, "<b>متن آزمون</b>")

# ---------------------------------------------------------------- جهت متن
# نام لاتین حذف نمی‌شود؛ فقط در یک واحد جدا ایزوله می‌شود تا در جملهٔ
# راست‌به‌چپ جابه‌جا نشود. قبلاً «Goldman» و «Sachs» دو واحد جدا بودند
# و در نمایش برعکس می‌شدند.
_bidi_out = radar.bidi("خبر: Goldman Sachs دسترسی به Claude Code را قطع کرد")
check("جهت: نام دو بخشی یک واحد است", "\u2066Goldman Sachs\u2069" in _bidi_out, True)
check("جهت: نام محصول جدا ایزوله می‌شود", "\u2066Claude Code\u2069" in _bidi_out, True)
check("جهت: فارسی دست نمی‌خورد", "دسترسی به" in _bidi_out, True)

# ---------------------------------------------------------------- ربط ساختگی
# خبر پول و دادگاه، اگر ربطی به سایت نداشته باشد، به کار مخاطب نمی‌آید.
_goldman = "Goldman Sachs has cut off most of its employees from AI coding tools"
check("مخاطب: خبر بانک و معامله‌گر رد می‌شود",
      bool(radar.OFF_AUDIENCE_RE.search(_goldman)) and not radar.SITE_TOPIC_RE.search(_goldman), True)
_wp = "WordPress plugin maker sues hosting company over trademark"
check("مخاطب: خبر حقوقی مرتبط با وردپرس رد نمی‌شود",
      bool(radar.OFF_AUDIENCE_RE.search(_wp)) and not radar.SITE_TOPIC_RE.search(_wp), False)
check("مخاطب: انتشار مدل تازه رد نمی‌شود",
      bool(radar.OFF_AUDIENCE_RE.search("Deep Principle releases Hydrogen-1.0")), False)

# ---------------------------------------------------------------- اصطلاح
# اصطلاح، نام است. ترجمهٔ لفظ‌به‌لفظش در فارسی معنا ندارد؛ خودش می‌ماند.
check(
    "اصطلاح: ترجمهٔ لفظ‌به‌لفظ به خود اصطلاح برمی‌گردد",
    radar.fix_phrases("کارمندان به «کدنویسی با حس و حال» متوسل شدند"),
    "کارمندان به «vibe coding» متوسل شدند",
)
check("اصطلاح: منبع باز به open source برمی‌گردد",
      radar.fix_phrases("این مدل منبع باز است"), "این مدل open source است")
check("اصطلاح: متن‌باز هم همین‌طور",
      radar.fix_phrases("یک ابزار متن‌باز"), "یک ابزار open source")
check("اصطلاح: متن سالم دست نمی‌خورد",
      radar.fix_phrases("این افزونه سایت را سریع می‌کند"), "این افزونه سایت را سریع می‌کند")

# ---------------------------------------------------------------- قرارداد پرامپت
# پرامپت در فایل جدا زندگی می‌کند. این سدها نمی‌گذارند تناقض‌های قدیمی
# بی‌صدا برگردند: دو راه رد کردن، برچسب مرده، و پرامپتِ دوازده هزار نویسه‌ای.
check("پرامپت: از فایل خوانده می‌شود", len(radar.SYSTEM_PROMPT) > 1000, True)
check("پرامپت: کوتاه مانده", len(radar.SYSTEM_PROMPT) < 7000, True)
check("پرامپت: نمرهٔ صفر ابزار رد نیست", "score صفر" in radar.SYSTEM_PROMPT, False)
check("پرامپت: برچسب مردهٔ فرصت حذف شده", "فرصت" in radar.SYSTEM_PROMPT, False)
check("پرامپت: فیلد skip هست", "skip" in radar.SYSTEM_PROMPT, True)
check("پرامپت: قاعدهٔ سهم بیست درصد حذف شده", "بیست درصد" in radar.SYSTEM_PROMPT, False)
check("پرامپت: هوش مصنوعی به سازندهٔ سایت محدود شده",
      "فقط وقتی به ساختن یا ادارهٔ سایت مربوط باشد" in radar.SYSTEM_PROMPT, True)
check("پرامپت: ارقام فارسی خواسته شده", "ارقام فارسی" in radar.SYSTEM_PROMPT, True)
check("پرامپت: نمونه دربارهٔ ساخت سایت است", "هدر" in radar.SYSTEM_PROMPT, True)
check("دستهٔ مردهٔ فرصت راهنمای پرامپت ندارد", "offers" in radar.KIND_HINT, False)
check("پرامپت: مقالهٔ فهرستی صریحاً رد شده", "مقالهٔ فهرستی" in radar.SYSTEM_PROMPT, True)
# یک بار به‌غلط نوشتم «ابزار کارت‌دار حذف نشود». خواستهٔ صاحب کانال حذف بود.
check("پرامپت: ابزار کارت‌دار رد می‌شود", "کارت بانکی بین‌المللی" in radar.SYSTEM_PROMPT, True)
check("پرامپت: دیگر نمی‌گوید کارت‌دار حذف نشود", "حذفش نکن" in radar.SYSTEM_PROMPT, False)

# ---------------------------------------------------------------- سد فهرست
# سد قبلی فقط «awesome-» را می‌گرفت و یک «۷ Best WordPress MCP Servers»
# از آن رد شد و در آزمون واقعی پست شد.
for _t in (
    "7 Best WordPress MCP Servers and Plugins in 2026 (We Tested Them All)",
    "Top 10 WooCommerce Themes for 2026",
    "The 12 best page builders, ranked",
    "بهترین افزونه‌های وردپرس برای فروشگاه",
    "راهنمای خرید هاست وردپرس",
    "فهرست بهترین قالب‌های خبری",
    "awesome-wordpress",
):
    check("سد فهرست: رد می‌شود | " + _t[:40], bool(radar.LIST_REPO_RE.search(_t)), True)

for _t in (
    "WordPress 7.1.4",
    "Elementor adds an AI header builder",
    "WooCommerce 9.4 released with faster checkout",
    "How to speed up your WordPress site",
    "یک حفرهٔ امنیتی در ووکامرس وصله شد",
):
    check("سد فهرست: دست نمی‌خورد | " + _t[:40], bool(radar.LIST_REPO_RE.search(_t)), False)

# ---------------------------------------------------------------- سقف ورودی AI
# سقف انتشار جلوی وارد شدن به صف را نمی‌گرفت و صف هشتاد و سه درصد AI شد.
_ai_room = radar.ai_intake_room()
check("سقف ورودی AI: عدد برمی‌گرداند", isinstance(_ai_room, int), True)
check("سقف ورودی AI: منفی نمی‌شود", _ai_room >= 0, True)

# ---------------------------------------------------------------- سقف انبار
# فید ابزار هر بار چهل آیتم می‌آورد و با سقف یک پست در روز، صد و پنجاه
# روز صف می‌ساخت. سقف انبار جلوی همین را می‌گیرد.
_real_count, _real_today = radar.queue_kind_count, radar.added_today
_rows = [
    {"id": "a", "published": "2026-11-04T10:00:00", "kind": "tools"},
    {"id": "b", "published": "2026-11-04T11:00:00", "kind": "tools"},
    {"id": "c", "published": "2026-11-04T12:00:00", "kind": "tools"},
]
radar.queue_kind_count = lambda kind: 8
radar.added_today = lambda kind: 0
check("سقف انبار: صف پر است، هیچ‌کدام وارد نمی‌شود",
      radar.trim_intake("tools", _rows, {"backlog_cap": {"tools": 8}}), [])
radar.queue_kind_count = lambda kind: 6
_kept = radar.trim_intake("tools", _rows, {"backlog_cap": {"tools": 8}})
check("سقف انبار: فقط جای خالی پر می‌شود", len(_kept), 2)
check("سقف انبار: تازه‌ترها می‌مانند", [x["id"] for x in _kept], ["c", "b"])
radar.queue_kind_count = lambda kind: 0
check("سقف روزانه: بیشتر از سهم روز وارد نمی‌شود",
      len(radar.trim_intake("tools", _rows, {"intake_per_day": {"tools": 1}})), 1)
radar.added_today = lambda kind: 1
check("سقف روزانه: سهم مصرف‌شده چیزی اضافه نمی‌کند",
      radar.trim_intake("tools", _rows, {"intake_per_day": {"tools": 1}}), [])
check("سقف: بی‌تنظیم دست نمی‌زند", len(radar.trim_intake("tools", _rows, {})), 3)
radar.queue_kind_count, radar.added_today = _real_count, _real_today

# ---------------------------------------------------------------- وردپرس اول
# فید عمومی هوش مصنوعی روزی چهل آیتم می‌دهد. اگر تنها معیار تازه‌بودن
# باشد، صف و سبد مدل را پر می‌کند و خبر وردپرسی به نوبت نمی‌رسد.
check("اولویت: خبر وردپرس رتبهٔ صفر",
      radar.wp_first({"title": "WordPress 7.2 beta is out"}), 0)
check("اولویت: خبر هوش مصنوعی که وردپرس دارد هم رتبهٔ صفر",
      radar.wp_first({"title": "OpenAI tool now writes WordPress plugins", "group": "ai"}), 0)
check("اولویت: هوش مصنوعی عمومی رتبهٔ دو",
      radar.wp_first({"title": "Anthropic warns it is close to RSI", "group": "ai"}), 2)
check("اولویت: فید وردپرسی بی‌کلیدواژه رتبهٔ یک",
      radar.wp_first({"title": "Modern CSS is getting complex", "source": "سی‌اس‌اس‌تریکس"}), 1)
_wp_rows = [
    {"id": "ai1", "title": "Nvidia cuts China business", "group": "ai",
     "published": "2026-11-04T12:00:00", "kind": "news"},
    {"id": "wp1", "title": "WooCommerce 10.2 released",
     "published": "2026-11-04T08:00:00", "kind": "news"},
]
check("اولویت: چیدمان، وردپرس را جلو می‌آورد",
      [x["id"] for x in radar.wp_first_sort(_wp_rows)], ["wp1", "ai1"])
_rc2, _rt2 = radar.queue_kind_count, radar.added_today
radar.queue_kind_count = lambda kind: 0
radar.added_today = lambda kind: 0
check("اولویت: سقف ورود جای وردپرس را به هوش مصنوعی نمی‌دهد",
      [x["id"] for x in radar.trim_intake("news", _wp_rows, {"backlog_cap": {"news": 1}})],
      ["wp1"])
radar.queue_kind_count, radar.added_today = _rc2, _rt2
# بعد از قانون «وردپرس اول»، قسمت هفتگی پادکست تاورن سر صف می‌نشست.
check("اولویت: پادکست خبر نیست",
      bool(radar.PODCAST_RE.search("https://wptavern.com/podcast/240-martin-schmidt")), True)
check("اولویت: تیتر شماره‌دار پادکست هم گرفته می‌شود",
      bool(radar.PODCAST_RE.search("#240 – Martin Schmidt on Partnerships")), True)
check("اولویت: خبر عادی با قاعدهٔ پادکست گرفته نمی‌شود",
      bool(radar.PODCAST_RE.search("WordPress 7.2 beta 4 is out")), False)
# مقاله‌ای که در خلاصه‌اش «این قسمت» دارد ممکن است خبر واقعی باشد.
# سد پادکست فقط تیتر و آدرس را می‌بیند، نه خلاصه را.
check("اولویت: خلاصهٔ «در این قسمت» به تنهایی خبر را حذف نمی‌کند",
      bool(radar.PODCAST_RE.search(
          "On this episode of Agency Action, Matt Medeiros joins Kurt von Ahnen")), False)

# ---------------------------------------------------------------- کف روزانهٔ AI
# با قانون «وردپرس اول»، خبر گروه ai ته صف می‌رفت و با برش ده‌تایی سبد
# هیچ‌وقت به مدل نمی‌رسید. کف، آیتم را بعد از اولین وردپرس می‌آورد.
_real_read_json = radar.read_json
radar.read_json = lambda path, default=None: (
    {"items": []} if path == radar.READY else default
)
_floor_rows = [
    {"id": "wp1", "title": "WordPress 7.2 beta is out", "kind": "news",
     "published": "2026-11-04T08:00:00"},
    {"id": "wp2", "title": "WooCommerce 10.2 released", "kind": "news",
     "published": "2026-11-04T07:00:00"},
    {"id": "ai1", "title": "Nvidia cuts China business", "group": "ai",
     "kind": "news", "published": "2026-11-04T12:00:00"},
]
check("کف AI: آیتم هوش مصنوعی بعد از اولین وردپرس می‌آید",
      [x["id"] for x in radar.ai_floor_promote(_floor_rows, {"ai_floor_per_day": 1}, {"kinds": {}})],
      ["wp1", "ai1", "wp2"])
check("کف AI: سهم امروز رفته، دست نمی‌زند",
      [x["id"] for x in radar.ai_floor_promote(_floor_rows, {"ai_floor_per_day": 1}, {"kinds": {"ai": 1}})],
      ["wp1", "wp2", "ai1"])
check("کف AI: بی‌تنظیم دست نمی‌زند",
      [x["id"] for x in radar.ai_floor_promote(_floor_rows, {}, {"kinds": {}})],
      ["wp1", "wp2", "ai1"])
check("کف AI: آیتم گروه ai در سبد نیست، خطا نمی‌دهد",
      [x["id"] for x in radar.ai_floor_promote(_floor_rows[:2], {"ai_floor_per_day": 1}, {"kinds": {}})],
      ["wp1", "wp2"])
radar.read_json = _real_read_json

# ---------------------------------------------------------------- خط نسخه
# «نسخهٔ ۷.۱.۴» و «بتای ۲» آخر کادر می‌ماندند و نکته نبودند.
check("جزئیات: خط فقط-نسخه حذف می‌شود",
      radar.clean_details("یک نکتهٔ واقعی.\nنسخهٔ ۷.۱.۴"), "یک نکتهٔ واقعی.")
check("جزئیات: خط فقط-بتا حذف می‌شود",
      radar.clean_details("یک نکتهٔ واقعی.\nبتای ۲"), "یک نکتهٔ واقعی.")
# خطی که فقط شناسهٔ CVE است، تکرار حرف بالاست و کادر را شلوغ می‌کند.
check("جزئیات: خط فقط-CVE حذف می‌شود",
      radar.version_only_line("\u2066CVE-2026-119977\u2069"), True)
check("جزئیات: CVE با ارقام فارسی هم حذف می‌شود",
      radar.version_only_line("CVE‑۲۰۲۶‑۱۱۹۹۷۷"), True)
check("جزئیات: جملهٔ دارای CVE دست نمی‌خورد",
      radar.version_only_line("این حفره با شناسه CVE-2026-119977 وصله شد"), False)
check("جزئیات: خط نسخه‌دار واقعی می‌ماند",
      "نسخهٔ ۷.۱.۴ امنیتی" in radar.clean_details("یک نکته.\nنسخهٔ ۷.۱.۴ امنیتی"), True)
# «PageSpeed ۷۰ به ۹۹» از یک مقالهٔ تحلیلی برداشته شد: عدد داشت، ولی
# نه می‌گفت کدام سایت، نه چرا. شکل داده داشت و داده نبود.
check("جزئیات: خط فقط-عدد و اسم لاتین حذف می‌شود",
      radar.meta_fragment("PageSpeed ۷۰ به ۹۹"), True)
check("جزئیات: همان خط با علامت ایزوله هم حذف می‌شود",
      radar.meta_fragment("\u2066PageSpeed\u2069 ۷۰ به ۹۹"), True)
check("جزئیات: متای نسخه دست نمی‌خورد",
      radar.meta_fragment("از ۴.۳.۵ به ۴.۳.۶"), False)
check("جزئیات: متای قیمت دست نمی‌خورد",
      radar.meta_fragment("قیمت ۴۹ دلار در ماه"), False)
check("جزئیات: خط ترکیبی فارسی و لاتین دست نمی‌خورد",
      radar.meta_fragment("با WP-CLI نصب می‌شود"), False)
check("جزئیات: خط بی‌معنی از کادر حذف می‌شود",
      radar.clean_details("روابط با مشتری مهم است.\nPageSpeed ۷۰ به ۹۹"), "روابط با مشتری مهم است.")
# متای فقط-نسخه آخر کادر یک خط تنها می‌ساخت، در حالی که تیتر همان نسخه را داشت.
check("قالب پست: متای تکراری نسخه حذف می‌شود",
      "نسخهٔ ۷.۱.۴" in radar.format_post("هشدار امنیتی", "وردپرس ۷.۱.۴ منتشر شد", "خلاصه", "نکته",
                                       "نسخهٔ ۷.۱.۴", "https://e.com/a", ""),
      False)
check("قالب پست: متای نسخهٔ متفاوت می‌ماند",
      "نسخهٔ ۹.۹" in radar.format_post("خبر", "خبر تست", "خلاصه", "نکته",
                                     "نسخهٔ ۹.۹", "https://e.com/a", ""),
      True)

# ---------------------------------------------------------------- برچسب بتا
# «وردپرس ۷.۲ بتای ۲» با برچسب هشدار امنیتی پست شد، چون متنش از اصلاحیه
# حرف می‌زد. بتا هشدار امنیتی نیست.
_sec_text = "وردپرس ۷.۱.۴ برای رفع چند مشکل امنیتی منتشر شده و همه را به آپدیت فوری توصیه کرده."
check("برچسب: نسخهٔ امنیتی هشدار می‌گیرد",
      radar.classify_label("news", _sec_text, "", False, "وردپرس ۷.۱.۴", "WordPress 7.1.4"),
      "هشدار امنیتی")
check("برچسب: بتا هشدار امنیتی نمی‌گیرد",
      radar.classify_label("news", _sec_text, "", False, "وردپرس ۷.۲ بتای ۲", "WordPress 7.2 Beta 2"),
      "آپدیت")

# ---------------------------------------------------------------- رصد نسخهٔ افزونه
# مخزن وردپرس تنها منبعی است که آپدیت امنیتی را همان روز می‌آورد.
_CH = (
    "<h4>4.3.6 &#8211; 2026-11-02</h4>\n<ul>\n"
    "<li>Fix: Improved code security enforcement in navigation links</li>\n"
    "<li>Fix: Atomic styles trigger unnecessary background operations</li>\n"
    "</ul>\n<h4>4.3.5 &#8211; 2026-10-13</h4>\n<ul>\n<li>Fix: Old thing</li>\n</ul>\n"
)
_head, _lines = radar.changelog_block(_CH, "4.3.6")
check("رصد نسخه: بلوک همان نسخه جدا می‌شود", len(_lines), 2)
check("رصد نسخه: عنوان بلوک تاریخ دارد", "4.3.6" in _head, True)
check("رصد نسخه: تقویت امنیت، آسیب‌پذیری نیست",
      bool(radar.VULN_RE.search(" ".join(_lines))), False)
check("رصد نسخه: تقویت امنیت شناخته می‌شود",
      bool(radar.HARDEN_RE.search(" ".join(_lines))), True)
check("رصد نسخه: آسیب‌پذیری واقعی شناخته می‌شود",
      bool(radar.VULN_RE.search("Fixed an XSS vulnerability in the form widget")), True)
check("رصد نسخه: نسخهٔ ناشناخته به بالاترین بلوک می‌افتد",
      len(radar.changelog_block(_CH, "9.9.9")[1]), 2)
check("رصد نسخه: فهرست خالی خطا نمی‌دهد", radar.changelog_block("", "1.0"), ("", []))

# اولین بار نسخه ثبت می‌شود و پستی نمی‌رود. بار دوم که نسخه عوض شود،
# یک آیتم ساخته می‌شود.
radar.load_plugin_watch = lambda: [{"slug": "elementor", "name": "Elementor", "min_installs": 0}]
radar.plugin_feed = lambda slug: {
    "name": "Elementor Website Builder", "version": "4.3.6", "installs": 10000000,
    "updated": "2026-11-02", "changelog": _CH,
}
radar.read_json = lambda path, default=None: (
    {"v": 1, "seen": {"elementor": {"version": "4.3.5"}}} if str(path).endswith("plugins.json") else default
)
_rows = radar.plugin_watch("news", {"v": 1, "ids": {}}, commit=False)
check("رصد نسخه: نسخهٔ تازه یک آیتم می‌سازد", len(_rows), 1)
check("رصد نسخه: آیتم در دستهٔ خبر می‌آید", _rows[0]["kind"] if _rows else "", "news")
check("رصد نسخه: نام نمایشی در تیتر است",
      _rows[0]["title"] if _rows else "", "Elementor 4.3.6")
check("رصد نسخه: نسخهٔ قبلی همراه آیتم است",
      _rows[0]["plugin_update"]["prev"] if _rows else "", "4.3.5")
check("رصد نسخه: تغییرها به مدل می‌رسند",
      len(_rows[0]["plugin_update"]["changes"]) if _rows else 0, 2)
check("رصد نسخه: متن مدل خط‌به‌خط است",
      "\n- " in radar.item_text(_rows[0]) if _rows else False, True)

radar.read_json = lambda path, default=None: (
    {"v": 1, "seen": {"elementor": {"version": "4.3.6"}}} if str(path).endswith("plugins.json") else default
)
check("رصد نسخه: نسخهٔ تکراری آیتم نمی‌سازد",
      radar.plugin_watch("news", {"v": 1, "ids": {}}, commit=False), [])

radar.read_json = lambda path, default=None: (
    {"v": 1, "seen": {}} if str(path).endswith("plugins.json") else default
)
check("رصد نسخه: اولین بار پست نمی‌سازد",
      radar.plugin_watch("news", {"v": 1, "ids": {}}, commit=False), [])

# ---------------------------------------------------------------- تیتر و برچسب
check("تیتر: برچسب ترکیبی یکجا برداشته می‌شود",
      radar.strip_lead_label("آپدیت امنیتی Elementor ۴.۳.۶ منتشر شد", "هشدار امنیتی"),
      "Elementor ۴.۳.۶ منتشر شد")
check("تیتر: «ابزار جدید گوگل» شکسته نمی‌شود",
      radar.strip_lead_label("ابزار جدید گوگل برای ساخت سایت", "ابزار"),
      "ابزار جدید گوگل برای ساخت سایت")
check("تیتر: برچسب تکراری با دونقطه برداشته می‌شود",
      radar.strip_lead_label("خبر: ووکامرس ۱۱.۲ آمد", "خبر"), "ووکامرس ۱۱.۲ آمد")
check("تیتر: تیتری که برچسب ندارد دست نمی‌خورد",
      radar.strip_lead_label("ووکامرس ۱۱.۲ آمد", "خبر"), "ووکامرس ۱۱.۲ آمد")

# خطی که با لاتین شروع شود، در تلگرام به چپ می‌چسبد و خواندنش سخت می‌شود.
check("جهت: خط با لاتین شروع نمی‌شود",
      radar.rtl_guard("Elementor ۴.۳.۶ آمد")[0], "\u200f")
check("جهت: خط فارسی دست نمی‌خورد",
      radar.rtl_guard("المنتور ۴.۳.۶ آمد")[0], "ا")
check("جهت: خط دوم هم جدا سنجیده می‌شود",
      radar.rtl_guard("یک خط\nElementor آمد").count("\u200f"), 1)

# ---------------------------------------------------------------- فیلتر واژه
# فید امنیتی عمومی بدون فیلتر، خبر آسیب‌پذیری هر محصول دیگری را هم می‌آورد.
_sec = {s["name"]: s for s in REAL_LOAD_SOURCES("security")}
check("فیلتر واژه: فید عمومی واژهٔ اجباری دارد",
      _sec.get("بلپینگ کامپیوتر", {}).get("must"), "wordpress")
check("فیلتر واژه: فید تخصصی واژهٔ اجباری ندارد",
      _sec.get("وردفنس", {}).get("must", ""), "")
check("فیلتر واژه: منابع بدون فیلد هم خوانده می‌شوند",
      len(REAL_LOAD_SOURCES("news")) > 10, True)

# ---------------------------------------------------------------- فهرست افزونه
_watch = REAL_LOAD_PLUGIN_WATCH()
check("فهرست افزونه: بیست‌و‌دو افزونه زیر نظر است", len(_watch), 22)
check("فهرست افزونه: نامک تکراری نیست",
      len({c["slug"] for c in _watch}), len(_watch))
check("فهرست افزونه: کمینهٔ نصب عدد است",
      all(isinstance(c["min_installs"], int) for c in _watch), True)

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
