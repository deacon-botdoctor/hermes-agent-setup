#!/usr/bin/env sh
# Render docs/drop-in-guide.md to docs/drop-in-guide.pdf.
# Requires pandoc (3.x) and WeasyPrint (60+). Run from the repository root.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
pandoc "$here/drop-in-guide.md" \
  --from gfm+pipe_tables --to html5 --standalone \
  --metadata title="The Drop-In Guide" \
  --css "$here/drop-in-guide.css" \
  --output "$tmp/drop-in-guide.html"
# Pandoc adds its own title block; the Markdown already opens with the title.
sed -i.bak -e '/<header id="title-block-header">/,/<\/header>/d' "$tmp/drop-in-guide.html"
weasyprint --base-url "$here" "$tmp/drop-in-guide.html" "$here/drop-in-guide.pdf"
echo "wrote $here/drop-in-guide.pdf"
