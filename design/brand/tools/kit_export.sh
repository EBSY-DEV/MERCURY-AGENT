#!/usr/bin/env bash
# Export the Mercury Agent kit from design/brand/final into design/brand/kit.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${LOGO_PY:-$HOME/.cache/logo-tools/bin/python}"  # venv with cairosvg, fonttools, uharfbuzz
SKILL_SCRIPTS="${LOGO_SKILL_SCRIPTS:-$HOME/.claude/skills/logo-design/scripts}"
E="$PY $SKILL_SCRIPTS/export_variants.py"
T="Mercury Agent"; K=kit
rm -rf $K/{symbol,lockup,wordmark,app-icon,web,motion}; mkdir -p $K/{symbol,lockup,wordmark,app-icon,web,motion}
$E final/mercury-symbol.svg --out-dir $K/symbol --title "$T" --only black mono --mono "#6D53D3" --png 64 256 512 1024 2048 >/dev/null
$E final/mercury-symbol-reversed.svg --out-dir $K/symbol --name mercury-symbol --title "$T" --only white --png 64 256 512 1024 2048 >/dev/null
$E final/mercury-symbol-small.svg --out-dir $K/symbol --title "$T" --only black white mono --mono "#6D53D3" --png 16 24 32 48 >/dev/null
for L in horizontal stacked; do
  $E final/mercury-lockup-$L.svg --out-dir $K/lockup --title "$T" --only black mono --mono "#6D53D3" --png 600 1200 2400 >/dev/null
done
$E final/mercury-wordmark.svg --out-dir $K/wordmark --title "$T" --only black white mono --mono "#6D53D3" --png 600 1200 2400 >/dev/null
$E final/mercury-symbol.svg --out-dir $K/web --title "$T" --only favicon --web-icons --favicon-source final/mercury-symbol-small.svg --icon-bg "#6D53D3" >/dev/null
$PY tools/kit_post.py
