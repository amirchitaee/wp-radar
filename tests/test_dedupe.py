"""آزمون تشخیص تکراری. اجرا: python tests/test_dedupe.py

هر مورد یک جفت تیتر واقعی‌شکل است، یا باید تکراری شناخته شود یا نه.
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402

FAILS = []
COUNT = 0


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILS.append("{} | got={} want={}".format(label, got, want))


def dup(a, b):
    return radar.same_story({"title": a}, {"title": b})


# ---------------------------------------------------------------- باید تکراری باشند
DUPES = [
    (
        "RCE وردپرس با تیتر بازنویسی‌شده",
        "WordPress 7.1.4 Patches Unauthenticated Upgrade-Window RCE; Exploitation Attempts Detected",
        "Critical RCE in WordPress 7.1.4 now exploited, patch immediately",
    ),
    (
        "یوست با تیتر بازنویسی‌شده",
        "Yoast SEO 24.5 Released",
        "Yoast SEO 24.5 brings new schema controls",
    ),
    (
        "اوپن‌ای‌آی با فعل متفاوت",
        "OpenAI releases GPT-6",
        "OpenAI launches GPT-6 with vision",
    ),
    (
        "فارسی، ترتیب واژه عوض شده",
        "وردپرس ۷.۱.۴ منتشر شد",
        "انتشار وردپرس ۷.۱.۴ با رفع آسیب‌پذیری",
    ),
    (
        "پسوند نام سایت چسبیده",
        "WordPress 6.9 Released - WP Tavern",
        "WordPress 6.9 Released | Kinsta",
    ),
    (
        "کلمهٔ اضافه در یکی",
        "Elementor 3.30 adds container queries",
        "Elementor 3.30 released with container queries support",
    ),
]

# ---------------------------------------------------------------- نباید تکراری باشند
NOT_DUPES = [
    (
        "دو نسخهٔ مختلف وردپرس",
        "WordPress 7.1.4 released",
        "WordPress 7.1.5 released",
    ),
    (
        "دو نسخهٔ مختلف المنتور",
        "Elementor 3.30 released",
        "Elementor 3.31 released",
    ),
    (
        "دو محصول مختلف از یک شرکت",
        "Yoast SEO 24.5 released",
        "Yoast WooCommerce 24.5 released",
    ),
    (
        "دو خبر بی‌ربط",
        "Kinsta launches new hosting plan",
        "Elementor 3.30 released",
    ),
    (
        "امنیت وردپرس در برابر فروشگاه دیگر",
        "WordPress security update fixes XSS",
        "Shopify launches AI store builder",
    ),
    (
        "دو افزونهٔ متفاوت",
        "WooCommerce 9.4 released",
        "Rank Math 1.0.220 released",
    ),
]


for label, a, b in DUPES:
    check("تکراری: " + label, dup(a, b), True)

for label, a, b in NOT_DUPES:
    check("جدا: " + label, dup(a, b), False)

# ---------------------------------------------------------------- تقارن
for label, a, b in DUPES + NOT_DUPES:
    check("تقارن: " + label, dup(a, b), dup(b, a))

# ---------------------------------------------------------------- حافظهٔ داستان
now = radar.now()
store = {"items": []}

radar.add_story(store, {"title": "WordPress 7.1.4 Patches RCE", "kind": "news", "link": "https://a.example/1"})
check("حافظه: داستان ثبت شد", len(store["items"]), 1)
check("حافظه: نام خاص ذخیره شد", "wordpress" in store["items"][0]["ents"], True)
check("حافظه: نسخه ذخیره شد", store["items"][0]["vers"], ["7.1.4"])

check(
    "حافظه: همان داستان از منبع دیگر گرفته می‌شود",
    radar.is_recent_story(store, {"title": "Critical RCE in WordPress 7.1.4 exploited", "kind": "news"}),
    True,
)
check(
    "حافظه: نسخهٔ بعدی گرفته نمی‌شود",
    radar.is_recent_story(store, {"title": "WordPress 7.1.5 released", "kind": "news"}),
    False,
)
check(
    "حافظه: خبر بی‌ربط گرفته نمی‌شود",
    radar.is_recent_story(store, {"title": "Elementor 3.30 released", "kind": "news"}),
    False,
)

# همان داستان در دستهٔ دیگر، تا سه روز باید گرفته شود
check(
    "حافظه: همان داستان در دستهٔ ابزار",
    radar.is_recent_story(store, {"title": "WordPress 7.1.4 patches RCE", "kind": "tools"}),
    True,
)

two_days = {"items": [dict(store["items"][0], t=(now - timedelta(days=2)).isoformat())]}
four_days = {"items": [dict(store["items"][0], t=(now - timedelta(days=4)).isoformat())]}

check(
    "حافظه: دو روز پیش، همان دسته بسته است",
    radar.is_recent_story(two_days, {"title": "WordPress 7.1.4 patches RCE", "kind": "news"}),
    True,
)
check(
    "حافظه: دو روز پیش، دستهٔ دیگر هم بسته است",
    radar.is_recent_story(two_days, {"title": "WordPress 7.1.4 patches RCE", "kind": "tools"}),
    True,
)
check(
    "حافظه: چهار روز پیش، خبر آزاد شده",
    radar.is_recent_story(four_days, {"title": "WordPress 7.1.4 patches RCE", "kind": "news"}),
    False,
)
check(
    "حافظه: چهار روز پیش، دستهٔ دیگر هم آزاد است",
    radar.is_recent_story(four_days, {"title": "WordPress 7.1.4 patches RCE", "kind": "tools"}),
    False,
)

# نمایشگاه پنجرهٔ بلندتری دارد
check(
    "حافظه: نمایشگاه بعد از چهار روز هنوز بسته است",
    radar.is_recent_story(
        {"items": [{"title": "WordPress 7.1.4 patches RCE", "kind": "showcase", "t": (now - timedelta(days=4)).isoformat()}]},
        {"title": "WordPress 7.1.4 patches RCE", "kind": "showcase"},
    ),
    True,
)

# ---------------------------------------------------------------- حذف تکراری در یک دسته
batch = [
    {"title": "WordPress 7.1.4 Patches RCE", "tier": 1, "age_hours": 2, "published": ""},
    {"title": "Critical RCE in WordPress 7.1.4 exploited", "tier": 3, "age_hours": 5, "published": ""},
    {"title": "Elementor 3.30 released", "tier": 2, "age_hours": 3, "published": ""},
]
kept = radar.dedupe_stories(batch)
check("دسته: دو مورد یکسان یکی شد", len(kept), 2)
check("دسته: شمارش منابع بالا رفت", kept[0].get("sources_count"), 2)
check("دسته: بهترین درجه نگه داشته شد", kept[0]["tier"], 1)

# ---------------------------------------------------------------- سن واقعی
item_old = {"age_hours": 1, "published": (now - timedelta(days=5)).isoformat()}
check("سن واقعی: از تاریخ انتشار", round(radar.true_age(item_old)), 120)
item_no_pub = {"age_hours": 7, "published": ""}
check("سن واقعی: بدون تاریخ انتشار", radar.true_age(item_no_pub), 7)

# ---------------------------------------------------------------- نتیجه
print("{} آزمون اجرا شد.".format(COUNT))
if FAILS:
    print("{} شکست:".format(len(FAILS)))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("همه سبز.")
