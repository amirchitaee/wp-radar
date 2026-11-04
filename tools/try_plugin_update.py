#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""آزمون تک‌آیتمی آپدیت افزونه.

نسخهٔ تازه را از مخزن وردپرس می‌گیرد، آیتم را مثل موتور می‌سازد، از مدل
می‌خواهد پستش را بنویسد و خروجی را چاپ می‌کند. هیچ چیزی در صف یا وضعیت
نمی‌نویسد.

  python tools/try_plugin_update.py
  python tools/try_plugin_update.py --slug woocommerce
  python tools/try_plugin_update.py --send      # به چت شخصی می‌فرستد
"""

import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import radar  # noqa: E402


def prev_version(changelog, version):
    vers = []
    for m in radar.re.finditer(r"<h4[^>]*>(.*?)</h4>", str(changelog or ""), radar.re.S | radar.re.I):
        v = radar.re.search(r"[\d۰-۹]+(?:\.[\d۰-۹]+)+", radar.clean_text(m.group(1)))
        if v:
            vers.append(v.group(0))
    if version in vers:
        i = vers.index(version)
        if i + 1 < len(vers):
            return vers[i + 1]
    return vers[1] if len(vers) > 1 else ""


def build_item(slug, name=""):
    info = radar.plugin_feed(slug)
    if not info or not info.get("version"):
        return None
    version = info["version"]
    head, lines = radar.changelog_block(info.get("changelog"), version)
    if not lines:
        return None
    link = "https://wordpress.org/plugins/{}/".format(slug)
    return {
        "id": radar.item_id(link),
        "title": "{} {}".format(name or info.get("name") or slug, version),
        "link": link,
        "desc": "نسخهٔ {} منتشر شد. تغییرات: {}".format(version, " | ".join(lines)),
        "source": "آپدیت افزونه",
        "lang": "en",
        "tier": 1,
        "group": "wp",
        "kind": "news",
        "age_hours": 0.0,
        "published": radar.now().isoformat(timespec="seconds"),
        "added": radar.now().isoformat(timespec="seconds"),
        "plugin": {
            "name": name or info.get("name") or slug,
            "version": version,
            "prev": prev_version(info.get("changelog"), version),
            "installs": info.get("installs", 0),
            "updated": info.get("updated", ""),
        },
        "plugin_update": {
            "name": name or info.get("name") or slug,
            "slug": slug,
            "version": version,
            "prev": prev_version(info.get("changelog"), version),
            "date": head,
            "changes": lines,
            "vuln": bool(radar.VULN_RE.search(" ".join(lines))),
            "hardening": bool(radar.HARDEN_RE.search(" ".join(lines))),
        },
    }


def main():
    args = sys.argv[1:]
    slug = "elementor"
    if "--slug" in args:
        slug = args[args.index("--slug") + 1]
    send = "--send" in args
    name = ""
    if "--name" in args:
        name = args[args.index("--name") + 1]

    item = build_item(slug, name)
    if not item:
        print("no item for", slug)
        return 1

    extra = ""
    access = radar.read_optional("access.txt")
    glossary = radar.read_optional("glossary.txt")
    if access:
        extra += "فایل دسترسی (فقط از این نقل کن):\n" + access + "\n"
    if glossary:
        extra += "املای ثابت نام‌ها:\n" + glossary + "\n"

    prompt = radar.build_prompt([item], extra)
    print("slug:", slug)
    print("version:", item["plugin_update"]["prev"], "->", item["plugin_update"]["version"])
    print("vuln:", item["plugin_update"]["vuln"], "| hardening:", item["plugin_update"]["hardening"])
    print("changes:", len(item["plugin_update"]["changes"]))
    print("prompt chars:", len(radar.SYSTEM_PROMPT), "+", len(prompt))
    print("---")

    started = time.time()
    res = radar.call_models(radar.SYSTEM_PROMPT, prompt, budget=150)
    print("took {:.1f}s".format(time.time() - started))
    entry = {}
    for e in (res.get("items") or []) if isinstance(res, dict) else []:
        if str(e.get("id", "")) == item["id"]:
            entry = e
            break
    if not entry:
        items = (res.get("items") or []) if isinstance(res, dict) else []
        entry = items[0] if items else {}
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print("---")

    if radar.entry_skipped(entry):
        print("SKIPPED by model:", entry.get("why", ""))
        return 0

    label = radar.classify_label(
        "news",
        " ".join([entry.get("title", ""), entry.get("summary", ""), entry.get("details", "")]),
        entry.get("label", ""),
        title=entry.get("title", ""),
        source_title=item["title"],
    )
    ok, why = radar.quality_ok(entry.get("title", ""), entry.get("summary", ""))
    if not item["plugin_update"]["vuln"] and label in ("هشدار امنیتی", "فوری"):
        label = "آپدیت"
    print("label:", label, "| quality:", "ok" if ok else why)
    text = radar.format_post(
        label,
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("details", ""),
        entry.get("meta", ""),
        item["link"],
        radar.kind_tag_for("news", entry.get("title", "") + entry.get("details", "")),
        name=entry.get("name", ""),
    )
    print(text)
    if send:
        chat = radar.personal_chat()
        if chat:
            radar.send_to(chat, text, preview=False)
            print("[sent to personal chat]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
