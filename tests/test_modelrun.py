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
radar.fetch_article_text = lambda url, limit=2500: "متن کامل تست"
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

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
