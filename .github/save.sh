#!/usr/bin/env bash
set -e
mkdir -p state archive
git config user.name "radar-bot"
git config user.email "radar-bot@users.noreply.github.com"
git add state archive
if ! git diff --cached --quiet; then
  git commit -m "radar: $(date -u +%Y-%m-%dT%H:%MZ)"
  git push
fi
