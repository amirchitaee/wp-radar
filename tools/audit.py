"""گزارش سلامت سیستم: پرامپت، تنظیمات، کد، و صف.

هر تغییر در تنظیمات یا پرامپت باید بعدش این را اجرا کند. یک عدد که
با جای دیگری نمی‌خواند، همان‌جا قرمز می‌شود.

اجرا:
    python tools/audit.py
    python tools/audit.py --tests
"""
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402

PROMPT_CAP = 7000
FAILS = []
OKS = 0


def check(label, cond, value=""):
    global OKS
    if cond:
        OKS += 1
        print("  OK    {} {}".format(label, value))
    else:
        FAILS.append("{} {}".format(label, value))
        print("  FAIL  {} {}".format(label, value))


def warn(label, cond, value=""):
    """صف دادهٔ زنده است، نه تنظیم. ایرادش با اجرای بعدی پاک می‌شود."""
    print("  {}  {} {}".format("OK   " if cond else "WARN ", label, value))
    return cond


def section(title):
    print()
    print(title)


def load_settings():
    with open(ROOT / "settings.json", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    s = load_settings()
    p = radar.SYSTEM_PROMPT

    section("پرامپت")
    check("حجم زیر سقف", len(p) < PROMPT_CAP, "{}/{}".format(len(p), PROMPT_CAP))
    all_labels = set()
    for v in radar.LABELS.values():
        all_labels |= set(v)
    listed = [x.strip() for x in re.search(r"label: یکی از این‌ها: (.+?)\.", p).group(1).split("،")]
    check("برچسب‌های پرامپت در کد معتبرند", set(listed) <= all_labels, "، ".join(listed))
    check("برچسب مردهٔ فرصت نیست", "فرصت" not in p)
    rules = re.findall(r"^قاعدهٔ (اول|دوم|سوم|چهارم|پنجم|ششم|هفتم)", p, re.M)
    check("قاعده‌ها پیوسته‌اند", len(rules) == 7 and len(set(rules)) == 7, len(rules))
    check("قاعدهٔ نمایشگاه هست", "قاعدهٔ هفتم — نمایشگاه" in p)
    check("قاعدهٔ اول به هفتم ارجاع می‌دهد", "با قاعدهٔ هفتم سنجیده می‌شود" in p)

    section("تنظیمات")
    caps = s.get("caps") or {}
    daily = int(s.get("daily_cap", 0))
    check("جمع سقف دسته‌ها برابر سقف روز", sum(int(v or 0) for v in caps.values()) == daily,
          "{} == {}".format(sum(int(v or 0) for v in caps.values()), daily))
    active = [k for k, v in caps.items() if int(v or 0) > 0]
    # امنیت مسیر خودش را دارد و از سد ورود دسته‌ها رد نمی‌شود؛ پس سهم
    # ورود نمی‌خواهد و سقفش را خودش با بودجهٔ روز می‌سنجد.
    intake = s.get("intake_per_day") or {}
    need_intake = [k for k in active if k != "security"]
    check("هر دستهٔ فعال سهم ورود دارد", all(k in intake for k in need_intake),
          "، ".join(need_intake))
    check("ورود هر دسته از سقف انتشارش کمتر نیست",
          all(int(intake.get(k, 0)) >= int(caps[k]) for k in need_intake),
          " ".join("{}:{}/{}".format(k, intake.get(k, 0), caps[k]) for k in need_intake))
    backlog = s.get("backlog_cap") or {}
    check("انبار هر دسته جا برای یک روز دارد",
          all(int(backlog.get(k, 0)) >= int(caps[k]) for k in active),
          " ".join("{}:{}/{}".format(k, backlog.get(k, 0), caps[k]) for k in active))
    check("کف هوش مصنوعی از سقفش بیشتر نیست",
          int(s.get("ai_floor_per_day", 0)) <= int(s.get("ai_cap_per_day", 0)),
          "{} <= {}".format(s.get("ai_floor_per_day"), s.get("ai_cap_per_day")))
    check("سقف هوش مصنوعی از ورودش بیشتر نیست",
          int(s.get("ai_cap_per_day", 0)) <= int(s.get("ai_intake_per_day", 0)),
          "{} <= {}".format(s.get("ai_cap_per_day"), s.get("ai_intake_per_day")))
    fill = s.get("ai_fill") or {}
    if fill.get("enabled", True):
        check("سقف جای خالی از سهم پایه کمتر نیست",
              int(fill.get("max", 0)) >= int(s.get("ai_cap_per_day", 0)),
              "{} >= {}".format(fill.get("max"), s.get("ai_cap_per_day")))
        check("سقف جای خالی از ورود هوش مصنوعی بیشتر نیست",
              int(fill.get("max", 0)) <= int(s.get("ai_intake_per_day", 0)),
              "{} <= {}".format(fill.get("max"), s.get("ai_intake_per_day")))
        pub = s.get("publish") or {}
        after = str(fill.get("after", "18:00"))
        start = str(pub.get("start", "08:00"))
        end = str(pub.get("end", "23:30"))
        check("ساعت جای خالی داخل پنجرهٔ انتشار است",
              start <= after < end, "{} <= {} < {}".format(start, after, end))
    check("آستانهٔ رد دست‌کم دو است",
          int(s.get("reject_max", 0) or 0) >= 2, s.get("reject_max"))
    chunk = int(s.get("chunk_size", 3))
    check("اندازهٔ دسته از سبد بیشتر نیست",
          chunk <= int(s.get("max_items_per_call", 0)),
          "{} <= {}".format(chunk, s.get("max_items_per_call")))
    # بار هر تماس مدل: پرامپت + متن هر آیتم + خروجی. اگر این از توان
    # لایه‌های رایگان بگذرد، مدل جواب نیمه‌کاره می‌دهد.
    load = len(p) + chunk * 2400 + int(s.get("model_max_tokens", 3000))
    check("بار هر تماس مدل در حد توانش است", load < 20000,
          "{} نویسه ≈ {} توکن".format(load, load // 3))
    check("نگه‌داری خبر با سد سن خبر یکی است",
          float(s.get("news_keep_h", 0)) == radar.max_age_for("news", s),
          s.get("news_keep_h"))
    check("سد امنیت با تنظیمش یکی است",
          radar.max_age_for("security", s) == float(s.get("security_max_age_h", 0)),
          s.get("security_max_age_h"))
    # انبار باید در طول سد سن خالی شود، وگرنه آیتم کهنه در صف می‌ماند.
    for kind in ("tools", "showcase"):
        if kind not in backlog or int(backlog[kind]) <= 0:
            continue
        days = radar.max_age_for(kind, s) / 24.0
        room = days * int(caps.get(kind, 0))
        check("سد سن {} با انبار و سقفش می‌خواند".format(kind), room >= int(backlog[kind]),
              "{:.1f} روز × {} = {:.0f} >= {}".format(days, caps.get(kind), room, backlog[kind]))
    auto_src = inspect.getsource(radar.auto_run)
    check("دستهٔ خاموش در نوبت‌بندی نیست",
          all(k not in auto_src for k in caps if int(caps[k] or 0) == 0),
          "، ".join(k for k in caps if int(caps[k] or 0) == 0))

    section("کد")
    try:
        subprocess.check_call([sys.executable, "-m", "py_compile", str(ROOT / "radar.py")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("radar.py کامپایل می‌شود", True)
    except subprocess.CalledProcessError:
        check("radar.py کامپایل می‌شود", False)
    need = [k for k in active if k != "security"]
    check("راهنمای دسته برای دسته‌های فعال هست",
          all(k in radar.KIND_HINT for k in need), "، ".join(need))
    for fn in ("tool_fit", "max_age_for", "ai_floor_promote", "is_recent_story", "add_story",
               "ai_allowance", "ai_narrow", "bump_reject", "pool_open"):
        check("تابع {} هست".format(fn), hasattr(radar, fn))
    check("is_recent_story استثنای شناسه دارد",
          "ignore_id" in inspect.signature(radar.is_recent_story).parameters)

    section("صف")
    q = radar.read_json(radar.QUEUE, {"items": []}).get("items") or []
    kinds = {}
    stale = 0
    off = 0
    for x in q:
        k = x.get("kind") or "news"
        kinds[k] = kinds.get(k, 0) + 1
        if not x.get("plugin_update") and radar.queue_age(x) > radar.max_age_for(k, s):
            stale += 1
        if k == "tools" and not radar.tool_fit(x):
            off += 1
    warn("صف آیتم کهنه ندارد", stale == 0, stale)
    warn("صف ابزار بی‌ربط ندارد", off == 0, off)
    print("        صف: {} آیتم — {}".format(
        len(q), "، ".join("{} {}".format(v, k) for k, v in sorted(kinds.items()))))

    if "--tests" in sys.argv:
        section("آزمون‌ها")
        for name in ("test_pipeline.py", "test_modelrun.py", "test_dedupe.py"):
            out = subprocess.run([sys.executable, str(ROOT / "tests" / name)],
                                 capture_output=True, text=True).stdout
            green = "همه سبز" in out
            count = re.search(r"(\d+) آزمون", out)
            check(name, green, count.group(1) if count else "?")

    print()
    if FAILS:
        print("{} مورد ایراد:".format(len(FAILS)))
        for f in FAILS:
            print("  -", f)
        return 1
    print("همه‌چیز سازگار است. {} بررسی.".format(OKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
