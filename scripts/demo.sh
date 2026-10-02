#!/usr/bin/env bash
# Usage: scripts/demo.sh <skill> [runs] [variant]
# Runs the same review prompt without and with the plugin, counts how many runs
# mention the skill-specific signal, and records everything in docs/demos/<skill>.md.
# With a variant (for example "hard") it uses fixtures/<skill>.<variant>.md and .signal
# and writes docs/demos/<skill>.<variant>.md.
set -euo pipefail

skill="${1:?usage: demo.sh <skill> [runs] [variant]}"
runs="${2:-3}"
variant="${3:-}"
name="$skill${variant:+.$variant}"
root="$(cd "$(dirname "$0")/.." && pwd)"
fixture="$root/fixtures/$name.md"
signal="$(cat "$root/fixtures/$name.signal")"
out="$root/docs/demos/$name.md"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

prompt="Review this code change as a senior backend engineer. List concrete problems only, most serious first.

$(cat "$fixture")"

run_arm() { # $1 = baseline | with
  if [ "$1" = "baseline" ]; then
    (cd "$work" && claude -p --disable-slash-commands "$prompt")
  else
    (cd "$work" && claude -p --plugin-dir "$root" "$prompt")
  fi
}

declare -i base_hits=0 with_hits=0
base_last="" with_last=""
for i in $(seq "$runs"); do
  b="$(run_arm baseline)"; w="$(run_arm with)"
  if printf '%s' "$b" | grep -Eiq "$signal"; then base_hits+=1; fi
  if printf '%s' "$w" | grep -Eiq "$signal"; then with_hits+=1; fi
  base_last="$b"; with_last="$w"
done

{
  echo "<!-- result: baseline=$base_hits with=$with_hits runs=$runs -->"
  echo "# Demo: $name"
  echo
  echo "Signal (regex, case-insensitive): \`$signal\`"
  echo
  echo "| Arm | Runs mentioning the signal |"
  echo "|---|---|"
  echo "| Without the plugin | $base_hits / $runs |"
  echo "| With the plugin | $with_hits / $runs |"
  echo
  echo "## Code reviewed"
  echo
  cat "$fixture"
  echo
  echo "<details><summary>Last output without the plugin</summary>"
  echo
  echo "$base_last"
  echo
  echo "</details>"
  echo
  echo "<details><summary>Last output with the plugin</summary>"
  echo
  echo "$with_last"
  echo
  echo "</details>"
} > "$out"

echo "$name: baseline $base_hits/$runs, with $with_hits/$runs -> $out"
