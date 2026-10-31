#!/usr/bin/env bash
# Save radar state to the repo.
# Several workflows can run at the same time and push simultaneously,
# so we rebase and retry instead of failing the whole run.
# اگر ذخیره واقعا انجام نشود، این اسکریپت خطا می‌دهد تا جاب قرمز شود؛
# تیک سبز روی ذخیرهٔ ناقص همان چیزی بود که باعث پست تکراری می‌شد.
set -u

mkdir -p state archive
git config user.name "radar-bot"
git config user.email "radar-bot@users.noreply.github.com"

git add -A state archive
if git diff --cached --quiet; then
  echo "no state change"
  exit 0
fi

git commit -m "radar: $(date -u +%Y-%m-%dT%H:%MZ)" || {
  echo "STATE NOT SAVED: commit failed"
  exit 1
}

for i in 1 2 3 4 5; do
  git pull --rebase --autostash origin main >/dev/null 2>&1 || true
  if git push origin HEAD:main >/dev/null 2>&1; then
    git fetch origin main >/dev/null 2>&1 || true
    if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main 2>/dev/null)" ] && [ -z "$(git status --porcelain)" ]; then
      echo "state saved (try $i)"
      exit 0
    fi
    echo "push ok but state not confirmed, retry $i"
  else
    echo "push failed, retry $i"
  fi
  sleep 5
done

echo "STATE NOT SAVED after 5 tries"
exit 1
