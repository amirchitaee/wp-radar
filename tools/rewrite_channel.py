"""بازنویسی پست‌های کانال با لحن تازه.

ورودی: فایل channel.json که tools/scan_channel.py ساخته است.
هر پست را از روی خود مقالهٔ منبع دوباره می‌نویسد و در کانال ویرایش می‌کند.

  python tools/rewrite_channel.py --limit 1 --dry
  python tools/rewrite_channel.py --limit 1 --yes
  python tools/rewrite_channel.py --yes
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402

DONE = radar.STATE / "rewrite.json"

LABEL_KIND = {
    "خبر": "news",
    "آپدیت": "news",
    "آموزش": "news",
    "فوری": "security",
    "هشدار امنیتی": "security",
    "ابزار": "tools",
    "پلاگین تازه": "tools",
    "فرصت": "offers",
    "نمایشگاه": "showcase",
}


def kind_of(text):
    first = (text or "").split("\n")[0]
    m = None
    for name, kind in LABEL_KIND.items():
        if name in first:
            m = kind
            break
    return m or "news"


def source_link(row):
    links = [l for l in (row.get("links") or []) if "t.me" not in l]
    return links[-1] if links else ""


def title_of(text):
    first = (text or "").split("\n")[0]
    return first.split(":", 1)[1].strip() if ":" in first else first.strip()


def rewrite(row):
    link = source_link(row)
    if not link:
        return "", "لینک منبع ندارد"
    kind = kind_of(row["text"])
    title = title_of(row["text"])
    try:
        full, _use = radar.fetch_article(link, limit=3200, hint=title)
    except Exception:
        full = ""
    item = {
        "id": "m{}".format(row["id"]),
        "source": "کانال",
        "lang": "en",
        "title": title,
        "desc": row["text"][:600],
        "full": full,
        "link": link,
        "kind": kind,
    }
    res = radar.call_models(
        radar.SYSTEM_PROMPT, radar.build_prompt([item], radar.kind_hint(kind)), budget=150
    )
    entries = res.get("items", []) if isinstance(res, dict) else []
    if not entries:
        return "", "مدل جواب نداد"
    entry = entries[0]
    for field in ("title", "summary", "meta", "name"):
        if entry.get(field):
            entry[field] = radar.fix_phrases(radar.strip_labels(entry[field]))
    entry["details"] = radar.fix_phrases(radar.clean_details(entry.get("details") or ""))
    label = radar.classify_label(
        kind, row["text"], entry.get("label", ""), False, entry.get("title", ""), title
    )
    name = radar.valid_name(entry.get("name", ""), item) or radar.auto_name(item)
    tag = radar.kind_tag_for(kind, entry.get("title", "") + " " + entry.get("summary", ""))
    label_tag = "#" + label.replace(" ", "_")
    tags = label_tag if label_tag == tag else label_tag + " " + tag
    text = radar.format_post(
        label,
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("details", ""),
        entry.get("meta", ""),
        link,
        tags,
        None,
        name=name,
    )
    return text, ""


def edit(row, text):
    method = "editMessageCaption" if row.get("media") else "editMessageText"
    key = "caption" if row.get("media") else "text"
    radar.http_post_json(
        "https://api.telegram.org/bot{}/{}".format(radar.TG_TOKEN, method),
        {"chat_id": radar.TG_CHAT, "message_id": row["id"], key: text[:4000], "parse_mode": "HTML"},
        timeout=30,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="channel.json")
    ap.add_argument("--skip", default="50,51,53,54,74,77")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    go = args.yes and not args.dry

    rows = json.loads(Path(args.file).read_text(encoding="utf-8"))["items"]
    skip = {int(x) for x in args.skip.split(",") if x.strip().isdigit()}
    rows = [r for r in rows if r["id"] not in skip and (r["text"] or "").strip()]
    done = radar.read_json(DONE, {"ids": []})["ids"] if DONE.exists() else []
    rows = [r for r in rows if r["id"] not in done]
    if args.limit:
        rows = rows[: args.limit]
    print("بازنویسی:", len(rows), "پست")

    ok = 0
    for row in rows:
        try:
            text, why = rewrite(row)
        except Exception as exc:
            print(row["id"], "خطا:", str(exc)[:100])
            continue
        if not text:
            print(row["id"], "رد شد:", why)
            continue
        print("\n--- {} ---".format(row["id"]))
        print(text)
        if go:
            try:
                edit(row, text)
                ok += 1
                done.append(row["id"])
                radar.write_json(DONE, {"ids": done})
                print("  ویرایش شد:", row["id"])
            except urllib.error.HTTPError as exc:
                print("  نشد:", row["id"], exc.read().decode()[:120])
            except Exception as exc:
                print("  نشد:", row["id"], str(exc)[:100])
            time.sleep(1)
    print("\nویرایش‌شده:", ok, "از", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
