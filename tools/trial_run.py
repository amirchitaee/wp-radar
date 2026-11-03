"""اجرای آزمایشی روی خبرهای واقعی، بدون انتشار و بدون دست زدن به state.

چه کاری می‌کند:
- از صف موجود چند خبر تازه برمی‌دارد (خواندن فقط).
- متن کامل مقاله را می‌آورد.
- با همان پرامپت و همان مدل اجرای واقعی، یک بار صدا می‌زند.
- خروجی خام و پست آمادهٔ تلگرام را چاپ می‌کند، با نتیجهٔ سدهای کد.

هیچ چیزی در کانال نمی‌رود، هیچ آیتمی «دیده‌شده» ثبت نمی‌شود،
و صف هم تغییر نمی‌کند.

اجرا: python tools/trial_run.py --limit 3
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--kind", default="news")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--raw", action="store_true", help="خروجی خام مدل هم چاپ شود")
    ap.add_argument("--match", default="", help="فقط آیتم‌هایی که این کلمه در تیتر یا توضیحشان هست")
    ap.add_argument("--live", action="store_true", help="از فیدها تازه بخوان، نه از صف")
    args = ap.parse_args()

    kind = args.kind
    if args.live:
        # خواندن تازه از فیدها. collect چیزی در صف نمی‌نویسد؛ فقط برمی‌گرداند.
        seen = radar.read_json(radar.SEEN, {"v": 1, "ids": {}})
        rows = radar.collect(kind, radar.read_json(radar.STATS, {}), seen, budget_sec=120)
        print("از فیدها: {} آیتم تازه خوانده شد".format(len(rows)))
    else:
        queue = radar.read_json(radar.QUEUE, {"items": []})
        rows = queue.get("items", []) if isinstance(queue, dict) else list(queue)

    pool = []
    for it in rows:
        if (it.get("kind") or "") != kind:
            continue
        blob = " ".join([it.get("title") or "", it.get("link") or ""])
        if radar.NON_NEWS_RE.search(blob) or radar.JUNK_NEWS_RE.search(blob):
            continue
        if radar.LIST_REPO_RE.search(blob):
            continue
        if radar.true_age(it) > args.hours:
            continue
        if args.match:
            hay = " ".join([it.get("title") or "", it.get("desc") or "", it.get("source") or ""]).lower()
            if args.match.lower() not in hay:
                continue
        pool.append(it)

    pool.sort(key=lambda x: radar.code_score(x, x.get("sources_count", 1)), reverse=True)
    batch = pool[: args.limit]
    if not batch:
        print("آیتم تازه‌ای در صف نبود. با --hours بیشتر دوباره بزن.")
        return 1

    print("=" * 70)
    print("{} خبر از صف، دستهٔ «{}»، بدون انتشار".format(len(batch), radar.KIND_FA.get(kind, kind)))
    print("=" * 70)

    radar.fetch_full_parallel(batch)
    prompt = radar.build_prompt(batch, radar.kind_hint(kind))

    print("پرامپت سیستم: {} نویسه".format(len(radar.SYSTEM_PROMPT)))
    print("پرامپت کاربر: {} نویسه".format(len(prompt)))
    print("")

    res = radar.call_models(radar.SYSTEM_PROMPT, prompt, budget=240)
    items = res.get("items", []) if isinstance(res, dict) else (res or [])

    if args.raw:
        print("--- خروجی خام ---")
        print(json.dumps(res, ensure_ascii=False, indent=2)[:6000])
        print("")

    by_id = {x["id"]: x for x in batch}
    for entry in items:
        # آیتمی که شناسه‌اش در این دسته نیست دور ریخته می‌شود، نه این که
        # به آیتم اول بچسبد؛ وگرنه لینک و تیتر یک خبر دیگر نمایش داده می‌شود.
        it = by_id.get(str(entry.get("id", "")))
        if not it and len(batch) == 1:
            it = batch[0]
        if not it:
            print("-" * 70)
            print("شناسهٔ ناشناس در خروجی مدل: {}".format(entry.get("id")))
            continue
        print("-" * 70)
        print("منبع: {}".format(it.get("title", "")[:90]))
        print("لینک: {}".format(it.get("link", "")))

        if radar.entry_skipped(entry):
            print("نتیجه: مدل رد کرد")
            print("دلیل: {}".format(entry.get("why") or "—"))
            continue

        for field in ("title", "summary", "meta", "name"):
            if entry.get(field):
                entry[field] = radar.fix_phrases(radar.strip_labels(entry[field]))
        if entry.get("details"):
            entry["details"] = radar.fix_phrases(radar.clean_details(entry["details"]))

        text_all = " ".join(
            [
                it.get("title") or "",
                it.get("desc") or "",
                entry.get("title") or "",
                entry.get("summary") or "",
                entry.get("details") or "",
            ]
        )
        label = radar.classify_label(
            kind, text_all, entry.get("label", ""), False, entry.get("title", ""), it.get("title", "")
        )
        name = radar.valid_name(entry.get("name", ""), it) or radar.auto_name(it)
        body = radar.format_post(
            label,
            entry.get("title", ""),
            entry.get("summary", ""),
            entry.get("details", ""),
            entry.get("meta", ""),
            it.get("link", ""),
            "#" + radar.re.sub(r"\s+", "_", label),
            it.get("published"),
            name=name,
            use_link="",
        )

        ok, why = radar.quality_ok(entry.get("title", ""), entry.get("summary", ""))
        verdict = "منتشر می‌شد" if ok else "رد می‌شد: " + why
        print("برچسب: {} | نمره: {} | اهمیت: {} | کاربرد: {}".format(
            label, entry.get("score"), entry.get("imp"), entry.get("use")))
        print("سد کیفیت: {}".format(verdict))
        print("")
        print(body)
        print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
