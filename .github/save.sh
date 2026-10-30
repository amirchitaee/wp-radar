#!/usr/bin/env bash
# Save radar state to the repo.
# Several workflows can run at the same time and push simultaneously,
# so we rebase and retry instead of failing the whole run.
set -u

mkdir -p state archive
git config user.name "radar-bot"
git config user.email "radar-bot@users.noreply.github.com"

git add -A state archive
if git diff --cached --quiet; then
  echo "no state change"
  exit 0
fi

git commit -m "radar: $(date -u +%Y-%m-%dT%H:%MZ)"

for i in 1 2 3 4 5; do
  git pull --rebase --autostash origin main >/dev/null 2>&1 || true
  if git push origin HEAD:main; then
    echo "state saved (try $i)"
    exit 0
  fi
  echo "push failed, retry $i"
  sleep 6
done

echo "state not saved after 5 tries"
exit 1
