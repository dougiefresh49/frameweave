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
echo "scrub: clean ($(git ls-files --cached --others --exclude-standard | wc -l | tr -d ' ') files)"
