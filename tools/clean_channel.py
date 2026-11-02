"""پاک کردن تکراری‌های کانال و بازنویسی پست‌های باقی‌مانده.

تلگرام به ربات اجازه نمی‌دهد تاریخچهٔ کانال را بخواند. ربات می‌تواند
بفرستد، ویرایش کند و پاک کند، ولی نمی‌تواند ببیند چه چیزی آن‌جاست.
پس ورودی لازم است: یا فایل اکسپورت تلگرام دسکتاپ، یا فهرست شناسه‌ها.

  python tools/clean_channel.py --export result.json --dry
  python tools/clean_channel.py --export result.json --yes
  python tools/clean_channel.py --export result.json --rewrite --dry --limit 3
  python tools/clean_channel.py --ids 1201,1202 --yes

بدون --yes هیچ چیزی پاک یا ویرایش نمی‌شود. پیش‌فرض، گزارش است.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402

TAG_RE = re.compile(r"<[^>]+>")
HREF_RE = re.compile(r'href="([^"]+)"')


def plain(text):
    return html.unescape(TAG_RE.sub(" ", str(text or ""))).strip()


def export_text(msg):
    """متن پیام در اکسپورت تلگرام گاهی رشته است و گاهی فهرست تکه‌ها."""
    raw = msg.get("text")
    if isinstance(raw, str):
        return raw
    out = []
    for part in raw or []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            out.append(part.get("text") or "")
    return "".join(out)


def load_export(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    for msg in data.get("messages") or []:
        if msg.get("type") != "message":
            continue
        text = export_text(msg)
        if not text:
            continue
        hrefs = [h for h in HREF_RE.findall(text) if "t.me" not in h]
        rows.append(
            {
                "id": int(msg["id"]),
                "date": str(msg.get("date") or ""),
                "text": text,
                "plain": plain(text),
                "link": hrefs[-1] if hrefs else "",
                "media": bool(msg.get("photo") or msg.get("media_type")),
            }
        )
    rows.sort(key=lambda r: r["id"])
    return rows


def title_of(row):
    """خط اول پست، تیتر است: «📰 خبر: ...»"""
    first = row["plain"].split("\n")[0]
    return re.sub(r"^\S+\s+\S+:\s*", "", first).strip()


def norm(text):
    return re.sub(r"[^\w\u0600-\u06FF]+", " ", str(text or "").lower()).strip()


def duplicate_groups(rows):
    """دو پست تکراری‌اند اگر لینک منبعشان یکی باشد، یا تیترشان یکی باشد."""
    groups, by_key = [], {}
    for row in rows:
        keys = []
        if row["link"]:
            keys.append("l:" + row["link"].rstrip("/"))
        t = norm(title_of(row))
        if len(t) > 20:
            keys.append("t:" + t)
        hit = next((by_key[k] for k in keys if k in by_key), None)
        if hit is None:
            hit = []
            groups.append(hit)
        hit.append(row)
        for k in keys:
            by_key[k] = hit
    return [g for g in groups if len(g) > 1]


def api(method, payload):
    return radar.http_post_json(
        "https://api.telegram.org/bot{}/{}".format(radar.TG_TOKEN, method),
        payload,
        timeout=30,
    )


def delete(ids, go):
    for mid in ids:
        if not go:
            print("  می‌رفت:", mid)
            continue
        try:
            api("deleteMessage", {"chat_id": radar.TG_CHAT, "message_id": mid})
            print("  پاک شد:", mid)
        except Exception as exc:
            print("  نشد:", mid, str(exc)[:120])


def rewrite_text(row, kind="news"):
    """متن تازه با همان لحن کانال، از خود مقالهٔ منبع."""
    if not row["link"]:
        return ""
    title = title_of(row)
    try:
        full = radar.fetch_article(row["link"], limit=3200, hint=title)
    except Exception:
        full = ""
    item = {
        "id": "m{}".format(row["id"]),
        "source": "کانال",
        "lang": "en",
        "title": title,
        "desc": row["plain"][:600],
        "full": full,
        "link": row["link"],
        "kind": kind,
    }
    res = radar.call_models(radar.SYSTEM_PROMPT, radar.build_prompt([item], radar.kind_hint(kind)), budget=150)
    entries = res.get("items", []) if isinstance(res, dict) else []
    if not entries:
        return ""
    entry = entries[0]
    for field in ("title", "summary", "meta", "name"):
        if entry.get(field):
            entry[field] = radar.fix_phrases(radar.strip_labels(entry[field]))
    entry["details"] = radar.fix_phrases(radar.clean_details(entry.get("details") or ""))
    label = radar.classify_label(
        kind,
        row["plain"],
        entry.get("label", ""),
        False,
        entry.get("title", ""),
        item["title"],
    )
    name = radar.valid_name(entry.get("name", ""), item) or radar.auto_name(item)
    tags = "#" + re.sub(r"\s+", "_", label)
    return radar.format_post(
        label,
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("details", ""),
        entry.get("meta", ""),
        row["link"],
        tags,
        None,
        name=name,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    ap.add_argument("--ids", default="")
    ap.add_argument("--rewrite", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    go = args.yes and not args.dry

    rows = load_export(args.export) if args.export else []
    if args.ids:
        wanted = [int(x) for x in re.findall(r"\d+", args.ids)]
        rows = [r for r in rows if r["id"] in wanted] if rows else [
            {"id": i, "text": "", "plain": "", "link": "", "date": "", "media": False}
            for i in wanted
        ]

    if not rows:
        print("ورودی‌ای نبود. یا --export بده یا --ids.")
        return 1

    print("پیام‌ها:", len(rows))
    groups = duplicate_groups(rows)
    doomed = []
    print("\nتکراری‌ها:")
    for g in groups:
        keep = g[0]
        print("  -", title_of(keep)[:60])
        for row in g[1:]:
            print("      پاک شود:", row["id"], row["date"][:10])
            doomed.append(row["id"])
    if not doomed:
        print("  چیزی پیدا نشد.")

    if doomed:
        print("\nپاک کردن {} پیام:".format(len(doomed)))
        delete(doomed, go)

    alive = [r for r in rows if r["id"] not in doomed]
    if args.rewrite:
        todo = [r for r in alive if r["link"]]
        if args.limit:
            todo = todo[: args.limit]
        print("\nبازنویسی {} پست:".format(len(todo)))
        for row in todo:
            text = rewrite_text(row)
            if not text:
                print("  رد شد:", row["id"])
                continue
            print("\n--- {} ---".format(row["id"]))
            print(text)
            if not go:
                continue
            try:
                if row.get("media"):
                    api(
                        "editMessageCaption",
                        {"chat_id": radar.TG_CHAT, "message_id": row["id"], "caption": text[:1000], "parse_mode": "HTML"},
                    )
                else:
                    api(
                        "editMessageText",
                        {"chat_id": radar.TG_CHAT, "message_id": row["id"], "text": text[:4000], "parse_mode": "HTML"},
                    )
                print("  ویرایش شد:", row["id"])
            except Exception as exc:
                print("  نشد:", row["id"], str(exc)[:140])
    return 0


if __name__ == "__main__":
    sys.exit(main())
