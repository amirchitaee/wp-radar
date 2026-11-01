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

SENT.clear()
posted = radar.model_run("offers", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)

check("خط تولید: یک پست ساخته شد", posted, 1)
check("خط تولید: یک پیام فرستاده شد", len(SENT), 1)

body = SENT[0] if SENT else ""

check("پست: برچسب فرصت", "#فرصت_رایگان" in body, True)
check("پست: برچسب تکراری ندارد", body.count("#فرصت") == 1, True)
check("پست: بلوک نقل‌قول دارد", "<blockquote expandable>" in body, True)
check("پست: برچسب فیلد حذف شد", "نام محصول:" not in body, True)
check("پست: خط تنهاماندهٔ لاتین حذف شد", "\nCloudflare\n" not in body, True)
check("پست: لینک منبع دارد", 'href="https://example.com/cf"' in body, True)
check("پست: لینک منبع روی کلمه نشسته", '>منبع</a>' in body, True)
check("پست: آدرس منبع در متن چاپ نشده", ">https://example.com/cf<" not in body, True)
check("پست: تیتر یک خط است", body.split("\n")[0].startswith("<b>"), True)

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
check("دیوی: آموزش در دستهٔ خبر منتشر نمی‌شود", got, 0)
check("دیوی: هیچ پیام امنیتی ساخته نشد", len(SENT), 0)

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
radar.collect = lambda kind, st, seen, budget_sec=None: [dict(NO_USE)]
radar.call_models = fake_model
SENT.clear()
got = radar.model_run("tools", settings, {}, {"v": 1, "ids": {}}, "", hard_deadline=None)
check("لینک استفاده: ابزار بی‌لینک رد می‌شود", got, 0)

# ---------------------------------------------------------------- برچسب تکراری
# «ابزار: ابزار HOL Guard» دو بار یک کلمه است.
check(
    "قالب پست: برچسب در تیتر تکرار نمی‌شود",
    radar.format_post("ابزار", "ابزار HOL-Guard برای ایمن‌سازی", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0],
    "<b>🛠 ابزار: HOL-Guard برای ایمن‌سازی</b>",
)
# دونقطهٔ داخل تیتر، خط را دو دونقطه‌ای می‌کرد.
check(
    "قالب پست: دونقطهٔ تیتر خط تیره می‌شود",
    radar.format_post("ابزار", "HOL Guard: آنتی‌ویروس", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0],
    "<b>🛠 ابزار: HOL Guard — آنتی‌ویروس</b>",
)
# اما ساعت ۱۰:۳۰ باید سالم بماند.
check(
    "قالب پست: دونقطهٔ ساعت دست‌نخورده می‌ماند",
    radar.format_post("خبر", "نسخهٔ ۷ ساعت ۱۰:۳۰ منتشر شد", "خلاصه", "", "", "https://e.com/a", "").split("\n")[0],
    "<b>📰 خبر: نسخهٔ ۷ ساعت ۱۰:۳۰ منتشر شد</b>",
)

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
