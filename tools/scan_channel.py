"""خواندن تاریخچهٔ کانال با ربات، برای پیدا کردن تکراری‌ها.

تلگرام به ربات اجازهٔ خواندن تاریخچه نمی‌دهد، ولی یک راه هست: هر پیام را
به چت پشتیبان فوروارد می‌کنیم، متنش را از پاسخ می‌خوانیم و همان نسخهٔ
فوروارد را فوراً پاک می‌کنیم. این‌طور شناسه و متن همهٔ پست‌ها را داریم.

  python tools/scan_channel.py --out channel.json
  python tools/scan_channel.py --out channel.json --max 400
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402

BOX = radar.env_clean("TG_PERSONAL") or radar.env_clean("TG_ADMIN")


def api(method, payload, tries=5):
    url = "https://api.telegram.org/bot{}/{}".format(radar.TG_TOKEN, method)
    for i in range(tries):
        try:
            return radar.http_post_json(url, payload, timeout=30)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                wait = 3
                try:
                    wait = int(json.loads(body)["parameters"]["retry_after"]) + 1
                except Exception:
                    pass
                time.sleep(wait)
                continue
            raise RuntimeError(body[:160])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # خط شبکه، نه خط تلگرام. با VPN نروژ این مدام پیش می‌آید.
            if i == tries - 1:
                raise RuntimeError("network: " + str(exc)[:100])
            time.sleep(2 + i * 2)
    raise RuntimeError("rate limited")


def read_one(mid):
    """متن پیام با شناسهٔ داده‌شده، یا None اگر وجود نداشته باشد.

    پیام سرویسی (مثل «کانال ساخته شد») وجود دارد ولی فوروارد نمی‌شود.
    آن را «هست ولی خالی» حساب می‌کنیم، وگرنه جست‌وجوی بیشترین شناسه
    اشتباه می‌شود.
    """
    try:
        res = api("forwardMessage", {"chat_id": BOX, "from_chat_id": radar.TG_CHAT, "message_id": mid})
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg or "MESSAGE_ID_INVALID" in msg:
            return None
        if "can't be forwarded" in msg:
            return {"id": mid, "date": None, "text": "", "media": False, "service": True}
        raise
    msg = res.get("result") or {}
    text = msg.get("text") or msg.get("caption") or ""
    ents = msg.get("entities") or msg.get("caption_entities") or []
    links = []
    for e in ents:
        if e.get("type") == "text_link" and e.get("url"):
            links.append(e["url"])
        elif e.get("type") == "url":
            part = text[e.get("offset", 0): e.get("offset", 0) + e.get("length", 0)]
            if part.startswith("http"):
                links.append(part)
    copy_id = msg.get("message_id")
    if copy_id:
        try:
            api("deleteMessage", {"chat_id": BOX, "message_id": copy_id})
        except Exception:
            pass
    return {
        "id": mid,
        "date": msg.get("date"),
        "text": text,
        "links": links,
        "media": bool(msg.get("photo") or msg.get("document") or msg.get("video")),
    }


def find_max(limit=200000):
    lo, hi = 1, 1
    while hi < limit and read_one(hi) is not None:
        lo = hi
        hi *= 2
    hi = min(hi, limit)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if read_one(mid) is not None:
            lo = mid
        else:
            hi = mid
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="channel.json")
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--gap", type=int, default=15)
    args = ap.parse_args()

    print("چت پشتیبان:", "set" if BOX else "MISSING")
    # شناسه‌ها پیوسته نیستند: پیام سرویسی و پست پاک‌شده فاصله می‌اندازند.
    # پس پشت سر هم می‌خوانیم و وقتی چند شناسهٔ پشت‌هم خالی بود، یعنی ته
    # کانال رسیده‌ایم.
    rows, missing, miss_run = [], 0, 0
    for mid in range(args.start, args.max + 1):
        got = read_one(mid)
        if got is None:
            missing += 1
            miss_run += 1
            if miss_run >= args.gap:
                print("  ته کانال در شناسهٔ", mid)
                break
            continue
        miss_run = 0
        rows.append(got)
        if len(rows) % 20 == 0:
            print("  خوانده شد:", len(rows), "| آخرین شناسه:", mid)
    print("پیام‌ها:", len(rows), "خالی:", missing)
    Path(args.out).write_text(json.dumps({"items": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("نوشته شد:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
