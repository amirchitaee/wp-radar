"""یک اجرای واقعی، ولی به کانال پشتیبان، نه کانال اصلی.

فیدها را تازه می‌خواند، سدهای کد را مثل اجرای واقعی اعمال می‌کند،
از مدل می‌خواهد پست بنویسد، و هر نتیجه را در کانال پشتیبان می‌فرستد.
هم ردشده‌ها را می‌فرستد و هم پست‌های آماده.

هیچ چیزی در کانال اصلی نمی‌رود، در صف نوشته نمی‌شود، و آیتمی
«دیده‌شده» ثبت نمی‌شود.

اجرا: python tools/backup_preview.py --limit 5
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--kind", default="news")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--send", action="store_true", help="به کانال پشتیبان بفرست")
    args = ap.parse_args()

    chat = radar.env_clean("TG_PERSONAL")
    if args.send and not chat:
        print("کانال پشتیبان تنظیم نشده است.")
        return 1

    seen = radar.read_json(radar.SEEN, {"v": 1, "ids": {}})
    rows = radar.collect(args.kind, radar.read_json(radar.STATS, {}), seen, budget_sec=120, watch=False)
    print("از فیدها: {} آیتم".format(len(rows)))

    rows = radar.dedupe_stories(rows)
    print("بعد از یکی‌سازی: {} آیتم".format(len(rows)))

    keep, cut = [], {"فهرست": 0, "غیرخبر": 0, "کهنه": 0, "کالای فارسی": 0}
    for it in rows:
        blob = " ".join([it.get("title") or "", it.get("link") or ""])
        if radar.LIST_REPO_RE.search(blob):
            cut["فهرست"] += 1
            continue
        if radar.NON_NEWS_RE.search(blob) or radar.JUNK_NEWS_RE.search(blob):
            cut["غیرخبر"] += 1
            continue
        if radar.true_age(it) > args.hours:
            cut["کهنه"] += 1
            continue
        if radar.persian_product(it):
            cut["کالای فارسی"] += 1
            continue
        keep.append(it)
    print("بعد از سدهای کد: {} آیتم | دور ریخته: {}".format(len(keep), cut))

    keep.sort(key=lambda x: radar.code_score(x, x.get("sources_count", 1)), reverse=True)
    batch = keep[: args.limit]
    if not batch:
        print("چیزی نماند.")
        return 0

    print("به مدل می‌رود:")
    for it in batch:
        print("  - {} | {} ساعت | {}".format(
            (it.get("source") or "")[:18], round(radar.true_age(it)), (it.get("title") or "")[:60]))

    radar.fetch_full_parallel(batch)
    res = radar.call_models(
        radar.SYSTEM_PROMPT, radar.build_prompt(batch, radar.kind_hint(args.kind)), budget=240
    )
    items = res.get("items", []) if isinstance(res, dict) else (res or [])
    by_id = {x["id"]: x for x in batch}

    head = "🔎 اجرای آزمایشی روی {} خبر تازه. هیچ‌کدام در کانال اصلی منتشر نشده.".format(len(batch))
    if args.send:
        radar.send_to(chat, head, False)
    print(head)

    for entry in items:
        it = by_id.get(str(entry.get("id", "")))
        if not it and len(batch) == 1:
            it = batch[0]
        if not it:
            print("شناسهٔ ناشناس:", entry.get("id"))
            continue
        src_line = "منبع: {} · {} ساعت پیش".format(it.get("source") or "", round(radar.true_age(it)))

        if radar.entry_skipped(entry):
            body = "⛔️ رد شد\n{}\nدلیل: {}".format(src_line, entry.get("why") or "—")
            print(body)
            if args.send:
                radar.send_to(chat, radar.esc(body), False)
            continue

        for field in ("title", "summary", "meta", "name"):
            if entry.get(field):
                entry[field] = radar.fix_phrases(radar.strip_labels(entry[field]))
        if entry.get("details"):
            entry["details"] = radar.fix_phrases(radar.clean_details(entry["details"]))

        text_all = " ".join([
            it.get("title") or "", it.get("desc") or "",
            entry.get("title") or "", entry.get("summary") or "", entry.get("details") or "",
        ])
        label = radar.classify_label(
            args.kind, text_all, entry.get("label", ""), False, entry.get("title", ""), it.get("title", "")
        )
        name = radar.valid_name(entry.get("name", ""), it) or radar.auto_name(it)
        post = radar.format_post(
            label, entry.get("title", ""), entry.get("summary", ""), entry.get("details", ""),
            entry.get("meta", ""), it.get("link", ""), "#" + radar.re.sub(r"\s+", "_", label),
            it.get("published"), name=name,
            use_link=it.get("use_link", "") if radar.action_links_on() else "",
        )
        ok, why = radar.quality_ok(entry.get("title", ""), entry.get("summary", ""))
        print("\n{}\n{}\nسد کیفیت: {}".format(src_line, post, "منتشر می‌شد" if ok else "رد می‌شد: " + why))
        if args.send:
            radar.send_to(chat, radar.esc(src_line) + "\n\n" + post, True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
