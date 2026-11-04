"""پاک‌سازی صف: ابزار و نمایشگاه تا سقف، خبر کهنه بیرون.

اجرا بدون سوییچ فقط گزارش می‌دهد. با `--apply` می‌نویسد و از نسخهٔ
قبلی در پوشهٔ backup یک کپی می‌گذارد.

    python tools/prune_queue.py
    python tools/prune_queue.py --apply

قاعده‌ها از settings.json خوانده می‌شوند: `backlog_cap` و `news_keep_h`.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402


def age_hours(item, field):
    raw = str(item.get(field) or "")
    if not raw:
        return 9999.0
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return 9999.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600


def prune(items, s):
    caps = s.get("backlog_cap") or {}
    keep_h = float(s.get("news_keep_h", 72))
    kept, dropped = [], []

    # دسته‌هایی که سقف انبار دارند: تازه‌ترها می‌مانند.
    for kind in ("tools", "showcase"):
        group = [x for x in items if (x.get("kind") or "") == kind]
        cap = int(caps.get(kind, 0) or 0)
        if cap <= 0:
            kept.extend(group)
            continue
        group.sort(key=lambda x: (x.get("published") or x.get("added") or ""), reverse=True)
        kept.extend(group[:cap])
        dropped.extend(group[cap:])

    # خبر: آپدیت افزونه همیشه می‌ماند، بقیه فقط تا سقف سن.
    for x in items:
        kind = x.get("kind") or ""
        if kind in ("tools", "showcase"):
            continue
        if x.get("plugin_update"):
            kept.append(x)
            continue
        if age_hours(x, "published") <= keep_h:
            kept.append(x)
        else:
            dropped.append(x)

    kept.sort(key=lambda x: (x.get("published") or x.get("added") or ""), reverse=True)
    return kept, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="واقعاً بنویس")
    args = ap.parse_args()

    s = radar.settings()
    q = radar.read_json(radar.QUEUE, {"v": 1, "items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    kept, dropped = prune(items, s)

    print("before :", len(items))
    print("after  :", len(kept))
    print("dropped:", len(dropped))
    kinds = {}
    for x in kept:
        kinds[x.get("kind") or "?"] = kinds.get(x.get("kind") or "?", 0) + 1
    print("kept   :", kinds)

    if not args.apply:
        print("\n(dry run — برای نوشتن --apply بده)")
        return 0

    backup_dir = ROOT / "backup"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    backup = backup_dir / "queue-before-prune-{}.json".format(stamp)
    if radar.QUEUE.exists():
        shutil.copy2(radar.QUEUE, backup)
        print("backup :", backup.relative_to(ROOT))

    radar.write_json(radar.QUEUE, {"v": 1, "items": kept})
    print("written:", radar.QUEUE.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
