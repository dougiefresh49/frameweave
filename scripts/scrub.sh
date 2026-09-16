#!/bin/bash
# scrub.sh: fail if any tracked or untracked file in this repo contains a term from
# the out-of-repo term list. The list is FRAMEWEAVE_SCRUB_TERMS from the environment
# or .env, default ~/.config/frameweave/scrub-terms.txt. The list must live outside
# the repo and is never committed. Case-insensitive, fixed strings, one term per line.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -z "${FRAMEWEAVE_SCRUB_TERMS:-}" ] && [ -f "$ROOT/.env" ]; then
  FRAMEWEAVE_SCRUB_TERMS="$(grep -E '^FRAMEWEAVE_SCRUB_TERMS=' "$ROOT/.env" | tail -1 | cut -d= -f2-)"
fi
LIST="${FRAMEWEAVE_SCRUB_TERMS:-$HOME/.config/frameweave/scrub-terms.txt}"
LIST="${LIST/#\~/$HOME}"
[ -f "$LIST" ] || { echo "scrub: term list not found at $LIST" >&2; exit 2; }
case "$(cd "$(dirname "$LIST")" && pwd -P)/" in
  "$ROOT"/*) echo "scrub: the term list must live outside the repo ($LIST)" >&2; exit 2 ;;
esac
cd "$ROOT"
hits="$(git ls-files --cached --others --exclude-standard -z \
  | xargs -0 grep -I -i -n -F -f "$LIST" -- 2>/dev/null || true)"
if [ -n "$hits" ]; then
  echo "scrub: forbidden terms found:" >&2
  echo "$hits" >&2
  exit 1
fi
# Public-readiness: no IPv4 address anywhere in tracked text (a recorded fixture once carried a home
# IP inside a caption URL, caught in review of PR #32). Version strings have three parts, so an
# address needs all four octets to match.
ips="$(git ls-files --cached --others --exclude-standard -z \
  | grep -z -v -E '^(uv\.lock)$' \
  | xargs -0 grep -I -n -E '(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)' -- 2>/dev/null \
  | grep -v -E '127\.0\.0\.1|0\.0\.0\.0|example|placeholder' || true)"
if [ -n "$ips" ]; then
  echo "scrub: IPv4 address found (replace with a placeholder):" >&2
  echo "$ips" >&2
  exit 1
fi
echo "scrub: clean ($(git ls-files --cached --others --exclude-standard | wc -l | tr -d ' ') files)"
