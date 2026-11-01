# -*- coding: utf-8 -*-
"""همان خبر، همان پرامپت، چند مدل. مقایسه با چشم، نه با حدس.

اجرا: python tests/model_faceoff.py
خروجی در tests/faceoff.txt ذخیره می‌شود تا بعد هم بشود نگاه کرد.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402

MODELS = [
    ("amd", "DeepSeek-V4-Flash"),
    ("amd", "DeepSeek-V4.1-Flash"),
    ("amd", "Gemma-4-31B-IT"),
    ("amd", "Qwen3.8-27B"),
    ("groq", "openai/gpt-oss-120b"),
]

STORY = "hol-guard"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faceoff.txt")


def story():
    q = json.load(open(radar.STATE / "queue.json", encoding="utf-8"))
    for x in q.get("items") or []:
        if STORY in (x.get("link") or ""):
            x["use_link"] = ""
            return x
    raise SystemExit("story not found: " + STORY)


def endpoint(name):
    for p in radar.model_providers():
        if p["name"] == name:
            return p
    raise SystemExit("provider not configured: " + name)


def main():
    item = story()
    prompt = radar.build_prompt([item], radar.kind_hint("tools"))
    lines = []

    for prov_name, model in MODELS:
        prov = endpoint(prov_name)
        t0 = time.time()
        lines.append("=" * 60)
        lines.append("{} / {}".format(prov_name, model))
        try:
            data = radar.http_post_json(
                prov["base"] + "/chat/completions",
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": radar.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.35,
                },
                headers={"Authorization": "Bearer " + prov["key"]},
                timeout=150,
            )
            content = radar.response_content(data, prov_name, model)
            res = radar.extract_json(content)
        except Exception as exc:
            lines.append("FAILED after {:.0f}s: {}".format(time.time() - t0, str(exc)[:200]))
            lines.append("")
            print("\n".join(lines[-3:]))
            continue

        items = res.get("items") or []
        lines.append("{:.0f} ثانیه، {} آیتم".format(time.time() - t0, len(items)))
        for e in items:
            lines.append(
                "score={} imp={} use={} | why: {}".format(
                    e.get("score"), e.get("imp"), e.get("use"), str(e.get("why"))[:160]
                )
            )
            lines.append("")
            lines.append(
                radar.format_post(
                    e.get("label") or "ابزار",
                    e.get("title", ""),
                    e.get("summary", ""),
                    e.get("details", ""),
                    e.get("meta", ""),
                    item.get("link", ""),
                    "",
                    None,
                    name=e.get("name", ""),
                    use_link="",
                )
            )
        lines.append("")
        print("\n".join(lines[-6:]))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("saved:", OUT)


if __name__ == "__main__":
    main()
