"""آزمون سرتاسری خط تولید پست، با مدل تقلبی. هیچ تماسی با اینترنت ندارد.

اجرا: python tests/test_modelrun.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402

FAILS = []
COUNT = 0
SENT = []


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILS.append("{} | got={!r} want={!r}".format(label, got, want))


import json
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
radar.DRY = True
radar.DAILY = TMP / "daily.json"
radar.QUEUE = TMP / "queue.json"
radar.STORIES = TMP / "stories.json"
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
radar.alert = lambda t: None
radar.send_to = lambda chat, text, preview=True: SENT.append(text)
radar.fetch_article = lambda url, limit=2500: ("متن کامل تست", "https://example.com/use")
radar.link_alive = lambda url: True

FAKE_ROW = {
    "id": "abc123",
    "title": "Cloudflare Workers AI free daily allowance",
    "link": "https://example.com/cf",
    "desc": "free daily quota for developers",
    "source": "تست",
    "lang": "en",
    "tier": 1,
    "kind": "offers",
    "age_hours": 2.0,
    "published": radar.now().isoformat(),
    "added": radar.now().isoformat(timespec="seconds"),
    "sources_count": 1,
    "use_link": "https://example.com/use",
}

radar.collect = lambda kind, st, seen, budget_sec=None: [dict(FAKE_ROW)]


def fake_model(system, user, deadline=None, budget=None):
    rid = FAKE_ROW["id"]
    return {
        "items": [
            {
                "id": rid,
                "title": "سهمیهٔ روزانهٔ رایگان برای ساخت سایت",
                "summary": "این سرویس به تو اجازه می‌دهد مدل‌های هوش مصنوعی را روی سایتت اجرا کنی "
                "بدون اینکه سرور بخری. هر روز یک مقدار رایگان داری و با ایمیل ثبت‌نام می‌کنی.",
                "details": "نام محصول: Cloudflare\nروزانه مقدار مشخصی رایگان داری و بیشتر از آن پولی می‌شود.\n"
                "با ایمیل ثبت‌نام کن، کارت بانکی لازم نیست.\n"
                "برای کسی که سایت تازه راه انداخته و بودجه ندارد مناسب است.",
                "meta": "",
                "label": "فرصت",
                "score": 28,
                "imp": 7,
                "use": 9,
            }
        ]
    }


radar.call_models = fake_model

settings = dict(radar.settings())
settings["max_posts_per_run"] = 2
settings["caps"] = {"news": 4, "tools": 2, "offers": 2, "showcase": 1}
# آزمون نباید به ساعت واقعی وابسته باشد. اگر پنجرهٔ انتشار باز نباشد،
# پست نگه داشته می‌شود و هیچ پیامی ساخته نمی‌شود؛ آن وقت آزمون ساعت
# سه بامداد شکست می‌خورد و ساعت ده صبح سبز می‌شود.
settings["publish"] = {"enabled": False}

SENT.clear()
posted = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)

check("خط تولید: یک پست ساخته شد", posted, 1)
check("خط تولید: یک پیام فرستاده شد", len(SENT), 1)

body = SENT[0] if SENT else ""

check("پست: برچسب فرصت", "#فرصت_رایگان" in body, True)

# ---------------------------------------------------------------- زمان‌بندی
# با انتشار زمان‌بندی‌شده، پست نوشته می‌شود ولی همان لحظه به کانال نمی‌رود.
HOLD_ROW = dict(FAKE_ROW)
HOLD_ROW["id"] = "hold01"
radar.READY = TMP / "ready.json"
timed = dict(settings)
timed["publish"] = {
    "enabled": True, "start": "00:00", "end": "23:59",
    "gap_min": 0, "max_ready": 4, "max_hold_h": 30,
    "bypass_labels": ["فوری", "هشدار امنیتی"],
}
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(HOLD_ROW)]
radar.call_models = fake_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
held = radar.model_run("offers", timed, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("زمان‌بندی: پست نوشته شد ولی نرفت", (held, len(SENT)), (0, 0))
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(FAKE_ROW)]
check("پست: برچسب تکراری ندارد", body.count("#فرصت") == 1, True)
check("پست: بلوک نقل‌قول دارد", "<blockquote expandable>" in body, True)
check("پست: برچسب فیلد حذف شد", "نام محصول:" not in body, True)
check("پست: خط تنهاماندهٔ لاتین حذف شد", "\nCloudflare\n" not in body, True)
check("پست: لینک منبع دارد", 'href="https://example.com/cf"' in body, True)
check("پست: لینک منبع روی کلمه نشسته", '>منبع</a>' in body, True)
check("پست: آدرس منبع در متن چاپ نشده", ">https://example.com/cf<" not in body, True)
check("پست: تیتر یک خط است", body.split("\n")[0].startswith("<b>"), True)


# ---------------------------------------------------------------- تیتر انگلیسی
# خبری که تیترش ترجمه نشده دور ریخته نمی‌شود. یک بار دیگر از مدل
# می‌خواهیم و همان پست با تیتر فارسی می‌رود.
def english_then_fix(system, user, deadline=None, budget=None):
    if "فقط تیتر همین یک خبر" in user:
        return {"items": [{"id": FAKE_ROW["id"], "title": "سهمیهٔ روزانهٔ رایگان برای ساخت سایت"}]}
    item = dict(fake_model(system, user)["items"][0])
    item["title"] = "Cloudflare Workers AI free daily allowance"
    return {"items": [item]}


radar.call_models = english_then_fix
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("تیتر: خبر با تیتر انگلیسی دور ریخته نمی‌شود", got, 1)
check(
    "تیتر: تیتر فارسی روی پست نشست",
    bool(SENT) and "سهمیهٔ روزانهٔ رایگان برای ساخت سایت" in SENT[0],
    True,
)
check("تیتر: تیتر انگلیسی در پست نمانده", bool(SENT) and "Cloudflare Workers AI free" in SENT[0], False)


# اگر ترجمهٔ دوباره هم جواب نداد، جملهٔ اول خلاصه تیتر می‌شود و خبر
# باز هم می‌رود. دور ریختن، آخرین گزینه است.
def english_no_fix(system, user, deadline=None, budget=None):
    if "فقط تیتر همین یک خبر" in user:
        return {"items": []}
    item = dict(fake_model(system, user)["items"][0])
    item["title"] = "Cloudflare Workers AI free daily allowance"
    return {"items": [item]}


radar.call_models = english_no_fix
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("تیتر: ترجمه نشد، از خلاصه ساخته می‌شود و پست می‌رود", got, 1)
check("تیتر: پست بی‌فارسی نرفت", bool(SENT) and "Cloudflare Workers AI free" in SENT[0], False)
radar.call_models = fake_model

# ---------------------------------------------------------------- حالت‌های رد
def run_with(entry_patch, label, want_posted):
    def fake(system, user, deadline=None, budget=None):
        item = dict(fake_model(system, user)["items"][0])
        item.update(entry_patch)
        return {"items": [item]}

    radar.call_models = fake
    SENT.clear()
    got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
    check(label, got, want_posted)


run_with({"use": 2}, "رد: قابل استفاده نیست", 0)
run_with({"details": "کوتاه"}, "رد: بلوک نقل‌قول پوک", 0)
run_with({"summary": "خیلی کوتاه است."}, "رد: خلاصهٔ کوتاه", 0)
run_with({"summary": "در دنیای امروز این ابزار می‌تواند کارهای زیادی برای همه انجام دهد و برای هر کسی که سایت دارد مفید باشد."}, "رد: کلیشه", 0)

# ---------------------------------------------------------------- قدیمی رد می‌شود
old_row = dict(FAKE_ROW)
old_row["published"] = (radar.now() - radar.timedelta(days=10)).isoformat()
radar.collect = lambda kind, st, seen, budget_sec=None: [old_row]
radar.call_models = fake_model
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("رد: فرصت ده روزه", got, 0)

# ---------------------------------------------------------------- مسیر سریع
# وقتی سهم دسته تمام شده، فقط خبر خیلی مهم و خیلی تازه می‌تواند رد شود.
fast_settings = dict(settings)
fast_settings["fast_lane"] = {"score": 1, "max_age_h": 3, "per_day": 2}
fast_settings["caps"] = {"news": 4, "tools": 2, "offers": 2, "security": 2, "showcase": 1}
fast_settings["daily_cap"] = 11

radar.collect = lambda kind, st, seen, budget_sec=None: [dict(FAKE_ROW)]
radar.call_models = fake_model
radar.DAILY.write_text(
    json.dumps({"date": radar.today(), "count": 11, "kinds": {"offers": 2}}), encoding="utf-8"
)
SENT.clear()
got = radar.model_run("offers", fast_settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("مسیر سریع: سهم تمام شده ولی خبر مهم رد می‌شود", got, 1)

# اگر جای مسیر سریع تمام شده باشد، همان خبر هم رد می‌شود.
radar.DAILY.write_text(
    json.dumps({"date": radar.today(), "count": 11, "kinds": {"offers": 2}, "fast": 2}),
    encoding="utf-8",
)
SENT.clear()
got = radar.model_run("offers", fast_settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("مسیر سریع: سهمیهٔ روزانه‌اش تمام، پستی نمی‌رود", got, 0)

# ---------------------------------------------------------------- باگ‌های ۱۰ آبان
# آموزش دیوی با برچسب «هشدار امنیتی» منتشر شده بود، چون «WooCommerce»
# کلمهٔ RCE را در خود دارد و برچسب از متن کامل مقاله ساخته می‌شد.
DIVI = {
    "id": "divi01",
    "title": "How To Build A Product Grid With Divi 5’s CSS Grid Editor",
    "link": "https://example.com/divi-grid",
    "desc": "a practical guide to laying out WooCommerce products with the CSS grid editor",
    "source": "الگانت",
    "lang": "en",
    "tier": 3,
    "kind": "news",
    "age_hours": 1.0,
    "published": radar.now().isoformat(),
    "added": radar.now().isoformat(timespec="seconds"),
    "sources_count": 1,
}

radar.collect = lambda kind, st, seen, budget_sec=None: [dict(DIVI)]


def divi_model(system, user, deadline=None, budget=None):
    return {
        "items": [
            {
                "id": "divi01",
                "label": "هشدار امنیتی",
                "name": "Divi",
                "title": "ساخت جدول محصولات با ابزار جدید دیوی",
                "summary": "این راهنما نشان می‌دهد چطور با ویرایشگر شبکه‌ای، چیدمان محصولات فروشگاه را "
                "درست کنی تا در موبایل و تبلت هم مرتب بماند. برای فروشگاه‌های کوچک کاربردی است.",
                "details": "ستون‌ها را با ویرایشگر شبکه بچین.\nهر کارت را با حلقه تکرار کن.\n"
                "برای فروشگاه ووکامرس مناسب است.",
                "meta": "",
                "score": 15,
                "imp": 5,
                "use": 8,
            }
        ]
    }


radar.call_models = divi_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
got = radar.model_run("news", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
# از ۱۶ آبان: اگر در دستهٔ خبر هیچ خبر وردپرسی نباشد، جای خالی با
# آموزش پر می‌شود تا کانال خالی نماند. برچسب باید آموزش باشد، نه امنیتی.
check("دیوی: در جای خالی آموزش منتشر می‌شود", got, 1)
_divi_news = SENT[0] if SENT else ""
check("دیوی: برچسب امنیتی ندارد (خبر)", "#هشدار_امنیتی" in _divi_news, False)
check("دیوی: برچسب آموزش دارد (خبر)", "#آموزش" in _divi_news, True)

# همان آیتم در دستهٔ ابزار، برچسب آموزش می‌گیرد و نام محصول هم دارد.
DIVI_TOOLS = dict(DIVI)
DIVI_TOOLS["id"] = "divi02"
DIVI_TOOLS["kind"] = "tools"
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(DIVI_TOOLS)]


def divi_tools_model(system, user, deadline=None, budget=None):
    out = divi_model(system, user)
    out["items"][0]["id"] = "divi02"
    return out


radar.call_models = divi_tools_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
got = radar.model_run("tools", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
divi_body = SENT[0] if SENT else ""
check("دیوی: در دستهٔ ابزار پست می‌شود", got, 1)
check("دیوی: برچسب امنیتی ندارد", "#هشدار_امنیتی" in divi_body, False)
check("دیوی: برچسب آموزش دارد", "#آموزش" in divi_body, True)
check("دیوی: نام محصول در تیتر هست", "Divi" in (divi_body.split("\n")[0] if divi_body else ""), True)

# همان آیتم با نمرهٔ کاربرد پایین. برچسب آموزش نباید راه فرار از سد
# «قابل استفاده» باشد؛ وگرنه مدل با عوض کردن برچسب، ابزار ضعیف را رد می‌کند.
DIVI_WEAK = dict(DIVI_TOOLS)
DIVI_WEAK["id"] = "divi03"
DIVI_WEAK["title"] = "How To Build A Pricing Table With Divi 5’s Module Editor"
DIVI_WEAK["link"] = "https://example.com/divi-pricing"
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(DIVI_WEAK)]


def divi_weak_model(system, user, deadline=None, budget=None):
    out = divi_tools_model(system, user)
    out["items"][0]["id"] = "divi03"
    out["items"][0]["use"] = 3
    return out


radar.call_models = divi_weak_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
got = radar.model_run("tools", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("دیوی: ابزار کم‌کاربرد با برچسب آموزش رد می‌شود", got, 0)

# بازی تخته‌ای رایگان نباید در کانال وردپرس منتشر شود.
GAME = dict(FAKE_ROW)
GAME.update(
    {
        "id": "game01",
        "title": "Show HN: Play Nightmare – the 1991 VHS board game, with a TV and phones",
        "link": "https://example.com/game",
        "desc": "a free board game you play with your TV and phones",
    }
)
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(GAME)]


def game_model(system, user, deadline=None, budget=None):
    return {
        "items": [
            {
                "id": "game01",
                "label": "فرصت",
                "name": "Nightmare",
                "title": "بازی تخته‌ای رایگان با تم نوستالژیک",
                "summary": "یک بازی رایگان که با تلویزیون و تلفن بازی می‌شود و برای دورهمی ساخته شده است.",
                "details": "رایگان است و کارت نمی‌خواهد.\nبا تلویزیون و تلفن کار می‌کند.",
                "meta": "",
                "score": 5,
                "imp": 3,
                "use": 9,
            }
        ]
    }


radar.call_models = game_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("بازی: منتشر نمی‌شود", got, 0)
check("بازی: هیچ پیامی ساخته نشد", len(SENT), 0)

# «وضعیت دسترسی: نامشخص» فیلد پوک است و کل پست را رد می‌کند.
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(FAKE_ROW)]


def hollow_model(system, user, deadline=None, budget=None):
    item = dict(fake_model(system, user)["items"][0])
    item["details"] = "وضعیت دسترسی: نامشخص.\nبا ایمیل ثبت‌نام کن و سهمیه را بگیر.\nکارت بانکی لازم نیست."
    return {"items": [item]}


radar.call_models = hollow_model
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("پوک: وضعیت نامشخص پست نمی‌شود", got, 0)

# ---------------------------------------------------------------- لینک استفاده
# پستی که لینک استفاده دارد، باید در متن لینک استفاده را هم نشان بدهد.
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(FAKE_ROW)]
radar.call_models = fake_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
got = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
body_use = SENT[0] if SENT else ""
check("لینک استفاده: پست ساخته شد", got, 1)
check("لینک استفاده: در متن هست", 'href="https://example.com/use"' in body_use, True)
check("لینک استفاده: خود آدرس دیده می‌شود", ">https://example.com/use</a>" in body_use, True)
check("لینک استفاده: خط اقدام دارد", "ثبت‌نام" in body_use, True)

# ابزار بدون لینک استفاده رد می‌شود.
NO_USE = dict(FAKE_ROW)
NO_USE["id"] = "nou01"
NO_USE["kind"] = "tools"
NO_USE["use_link"] = ""
# سبد ابزار حالا محصول بی‌ربط را رد می‌کند؛ این آیتم باید به سایت یا
# تولید محتوا مربوط باشد تا سناریو به سد درست برسد، نه به سد سبد.
NO_USE["title"] = "Cloudflare Workers AI for WordPress content sites"
NO_USE["desc"] = "free AI writing quota for your website"
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(NO_USE)]


def no_use_model(system, user, deadline=None, budget=None):
    # شناسه باید با آیتم دسته یکی باشد، وگرنه آزمون به‌جای سد لینک،
    # رفتار «شناسهٔ ناشناس» را می‌سنجد.
    item = fake_model(system, user)["items"][0]
    item["id"] = "nou01"
    return {"items": [item]}


radar.call_models = no_use_model
SENT.clear()
got = radar.model_run("tools", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
# خط اقدام و لینک استفاده خاموش است (action_links: false). پس ابزار
# بی‌لینک استفاده رد نمی‌شود و خود لینک منبع کافی است. آزمون قبلی سبز
# بود چون شناسهٔ مدل با شناسهٔ آیتم نمی‌خواند، نه چون این سد وجود داشت.
check("لینک استفاده: با خط اقدام خاموش، ابزار بی‌لینک می‌ماند", got, 1)

# ---------------------------------------------------------------- برچسب تکراری
# «ابزار: ابزار HOL Guard» دو بار یک کلمه است.
def plain(text):
    """علامت‌های نامرئی جهت متن را برمی‌دارد تا مقایسهٔ متنی ساده بماند."""
    return text.replace("\u2066", "").replace("\u2069", "")


check(
    "قالب پست: برچسب در تیتر تکرار نمی‌شود",
    plain(radar.format_post("ابزار", "ابزار HOL-Guard برای ایمن‌سازی", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0]),
    "<b>🛠 ابزار: HOL-Guard برای ایمن‌سازی</b>",
)
# دونقطهٔ داخل تیتر، خط را دو دونقطه‌ای می‌کرد.
check(
    "قالب پست: دونقطهٔ تیتر خط تیره می‌شود",
    plain(radar.format_post("ابزار", "HOL Guard: آنتی‌ویروس", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0]),
    "<b>🛠 ابزار: HOL Guard — آنتی‌ویروس</b>",
)

# متا جای داده است، نه جای صفت. «open source» داده نیست و یک خط تنها
# آخر کادر می‌ساخت.
_meta = plain(radar.format_post("ابزار", "ابزار تست", "خلاصهٔ تست", "خط یک\nخط دو", "open source", "https://e.com/a", ""))
check("قالب پست: متای بی‌عدد دور ریخته می‌شود", "open source" in _meta, False)
_meta2 = plain(radar.format_post("ابزار", "ابزار تست", "خلاصهٔ تست", "خط یک\nخط دو", "نسخه ۲.۴", "https://e.com/a", ""))
check("قالب پست: متای نسخه‌دار می‌ماند", "نسخه ۲.۴" in _meta2, True)
# اما ساعت ۱۰:۳۰ باید سالم بماند.
check(
    "قالب پست: دونقطهٔ ساعت دست‌نخورده می‌ماند",
    radar.format_post("خبر", "نسخهٔ ۷ ساعت ۱۰:۳۰ منتشر شد", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0],
    "<b>📰 خبر: نسخهٔ ۷ ساعت ۱۰:۳۰ منتشر شد</b>",
)

# ---------------------------------------------------------------- خط تازه
# مدل گاهی جای خط تازه، بک‌اسلش و n می‌فرستد و کتیشن یک خط می‌شود.
check(
    "قالب پست: خط تازهٔ فراری درست می‌شود",
    radar.trim_details("یک\\nدو\\nسه"),
    "یک\nدو\nسه",
)

# ---------------------------------------------------------------- قرارداد تازه
# رد کردن یک راه دارد: فیلد skip. نمره ابزار رد کردن نیست، فقط ترتیب
# انتشار را می‌گوید. قبلاً نمرهٔ صفر هم معنی رد داشت و مدل هر بار یکی
# از دو راه را می‌گرفت؛ نتیجه‌اش رفتار تصادفی بود.
NEWS_ROW = {
    "id": "news01",
    "title": "المنتور سازندهٔ هدر با جملهٔ فارسی اضافه کرد",
    "link": "https://example.com/elementor",
    "desc": "المنتور قابلیت ساخت چیدمان هدر با هوش مصنوعی را اضافه کرد. روی سایت‌های موجود با آپدیت معمولی می‌آید.",
    "full": "المنتور نسخهٔ تازه‌ای داد که سازندهٔ هدر با هوش مصنوعی را اضافه می‌کند. فقط هدر و فوتر را می‌سازد.",
    "source": "تست",
    "lang": "fa",
    "tier": 1,
    "kind": "news",
    "age_hours": 2.0,
    "published": radar.now().isoformat(),
    "added": radar.now().isoformat(timespec="seconds"),
    "sources_count": 1,
}


def skip_model(system, user, deadline=None, budget=None):
    return {
        "items": [
            {
                "id": "news01",
                "skip": True,
                "why": "خبر بورس است و به ساخت سایت ربط ندارد",
                "title": "",
                "summary": "",
                "details": "",
                "meta": "",
                "label": "خبر",
                "score": 0,
                "imp": 0,
                "use": 0,
            }
        ]
    }


radar.collect = lambda kind, st, seen, budget_sec=None: [dict(NEWS_ROW)]
radar.call_models = skip_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
skipped_run = radar.model_run("news", dict(settings), {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("رد مدل: هیچ پستی نمی‌رود", (skipped_run, len(SENT)), (0, 0))
check("رد مدل: skip رشته‌ای هم رد است", radar.entry_skipped({"skip": "true"}), True)
check("رد مدل: بدون skip رد نیست", radar.entry_skipped({}), False)


def zero_score_model(system, user, deadline=None, budget=None):
    return {
        "items": [
            {
                "id": "news01",
                "skip": False,
                "label": "آپدیت",
                "name": "Elementor",
                "title": "سازندهٔ هدر Elementor حالا با یک جملهٔ فارسی چیدمان می‌سازد",
                "summary": "المنتور ابزار تازه‌ای دارد که با یک جملهٔ فارسی چیدمان هدر می‌سازد. برای کسی که با قالب آماده کار می‌کند، چیدن دستی بلوک‌ها تمام می‌شود.",
                "details": "فقط هدر و فوتر را می‌سازد، نه کل صفحه.\nبا آپدیت معمولی می‌آید، ولی قبلش بکاپ بگیر.",
                "meta": "نسخهٔ ۴.۲",
                "score": 0,
                "imp": 6,
                "use": 8,
                "why": "المنتور ساخت چیدمان با هوش مصنوعی اضافه کرده و کار دستی را کم می‌کند.",
            }
        ]
    }


radar.collect = lambda kind, st, seen, budget_sec=None: [dict(NEWS_ROW)]
radar.call_models = zero_score_model
radar.DAILY.write_text(json.dumps({"date": radar.today(), "count": 0, "kinds": {}}), encoding="utf-8")
SENT.clear()
zero_run = radar.model_run("news", dict(settings), {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("نمرهٔ صفر: پست را رد نمی‌کند", (zero_run, len(SENT)), (1, 1))

# ---------------------------------------------------------------- ارقام فارسی
# صاحب کانال ارقام فارسی می‌خواهد. «7.1.4» و «100,000» باید فارسی شوند،
# ولی «GPT-4o» و «v2.3» نباید، چون رقم داخل نام لاتین است.
check("ارقام: نسخهٔ لاتین فارسی می‌شود", radar.fa_digits_text("نسخه 7.1.4"), "نسخه ۷.۱.۴")
check("ارقام: عدد بزرگ فارسی می‌شود", radar.fa_digits_text("100,000 تومان"), "۱۰۰,۰۰۰ تومان")
check("ارقام: نام لاتین دست نمی‌خورد", radar.fa_digits_text("GPT-4o و v2.3"), "GPT-4o و v2.3")
check(
    "قالب پست: ارقام تیتر فارسی می‌شوند",
    "۷.۱.۴" in radar.format_post("خبر", "نسخهٔ 7.1.4 منتشر شد", "خلاصه", "", "", "https://e.com/a", ""),
    True,
)

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
