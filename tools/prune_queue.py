"""پاک‌سازی صف: ابزار و نمایشگاه تا سقف، خبر کهنه بیرون.

منطقش داخل خود موتور است (`radar.prune_queue`) و خودکار هم روزی یک بار
اجرا می‌شود. این ابزار برای وقتی است که می‌خواهی همین حالا ببینی یا دستی
تمیزش کنی.

    python tools/prune_queue.py
    python tools/prune_queue.py --apply

پیش‌فرض فقط گزارش است. با `--apply` می‌نویسد و از نسخهٔ قبلی در پوشهٔ
backup یک کپی می‌گذارد.
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="واقعاً بنویس")
    args = ap.parse_args()

    q = radar.read_json(radar.QUEUE, {"v": 1, "items": []})
    items = q.get("items", []) if isinstance(q, dict) else list(q)
    kinds = {}
    for x in items:
        kinds[x.get("kind") or "?"] = kinds.get(x.get("kind") or "?", 0) + 1
    print("before :", len(items), kinds)

    if args.apply and radar.QUEUE.exists():
        backup_dir = ROOT / "backup"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / "queue-before-prune-{}.json".format(datetime.now().strftime("%Y-%m-%d"))
        shutil.copy2(radar.QUEUE, backup)
        print("backup :", backup.relative_to(ROOT))

    before, after = radar.prune_queue(apply=args.apply)
    print("after  :", after, "(dropped {})".format(before - after))
    if not args.apply:
        print("\n(dry run — برای نوشتن --apply بده)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
