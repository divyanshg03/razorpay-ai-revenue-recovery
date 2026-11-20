"""Render README.md into the public results page, `site/index.html`.

    python scripts/render_site.py            # rewrite site/index.html
    python scripts/render_site.py --check    # fail if the page is stale (used by the tests)

## Why the page is the README and nothing else

The honesty rule is that no figure is ever typed. The README already obeys it: every number
sits inside a block `scripts/render_docs.py` generates from the artifacts, and the tests fail
if one goes stale. A separate page with its own copy would be a second place for a number to
drift. So the page has no content of its own — it is the README, converted — and `--check`
fails the build whenever the two disagree.

The converter is stdlib and handles exactly the Markdown the README uses: headings, paragraphs,
bullet and numbered lists with indented continuations, tables, blockquotes, fenced code,
Mermaid diagrams, rules, and inline code, bold, italic and links. Raw HTML in the source is
escaped, never passed through. Relative links point at the file on GitHub, because the page
is hosted apart from the repository it describes.
"""

from __future__ import annotations

import argparse
import html
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
README = REPO / "README.md"
OUT = REPO / "site" / "index.html"

GITHUB = "https://github.com/divyanshg03/razorpay-ai-revenue-recovery"
#: Relative links resolve against the default branch, which is what the page describes.
BLOB = f"{GITHUB}/blob/main/"

#: Pinned: an unpinned CDN script is code the page runs that nobody reviewed.
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

_LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def slug(text: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces to hyphens."""
    plain = re.sub(r"`|\*\*|\*|\[([^\]]*)\]\([^)]*\)", r"\1", text).lower()
    plain = re.sub(r"[^\w\- ]", "", plain)
    return plain.replace(" ", "-")


def _href(url: str) -> str:
    if url.startswith(("http://", "https://", "#", "mailto:")):
        return url
    return BLOB + url.lstrip("./")


def inline(text: str) -> str:
    """Escape first, then apply inline marks. Code spans are lifted out so nothing inside
    them is formatted."""
    spans: list[str] = []

    def lift(m: re.Match) -> str:
        spans.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lift, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  lambda m: f'<a href="{html.escape(_href(html.unescape(m.group(2))))}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def _table(rows: list[str]) -> str:
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    head, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head)
           + "</tr></thead>", "<tbody>"]
    out += ["<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body]
    out += ["</tbody>", "</table>"]
    return "\n".join(out)


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("<!--"):
            while "-->" not in lines[i]:
                i += 1
            i += 1
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            body = html.escape("\n".join(code))
            if lang == "mermaid":
                out.append(f'<pre class="mermaid">{body}</pre>')
            else:
                out.append(f"<pre><code>{body}</code></pre>")
            continue

        if m := _HEADING.match(line):
            level, text = len(m.group(1)), m.group(2)
            out.append(f'<h{level} id="{slug(text)}">{inline(text)}</h{level}>')
            i += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue

        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{convert(chr(10).join(quote))}</blockquote>")
            continue

        if m := _LIST_ITEM.match(line):
            ordered = m.group(2)[0].isdigit()
            items: list[str] = []
            while i < len(lines) and (m := _LIST_ITEM.match(lines[i])) and not m.group(1):
                text = [m.group(3)]
                i += 1
                # Indented lines continue the item; a blank line followed by an indent does too.
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or (
                        not lines[i].strip() and i + 1 < len(lines)
                        and lines[i + 1].startswith((" ", "\t")))):
                    text.append(lines[i].strip())
                    i += 1
                items.append(f"<li>{inline(' '.join(t for t in text if t))}</li>")
                while i < len(lines) and not lines[i].strip() and i + 1 < len(lines) \
                        and (n := _LIST_ITEM.match(lines[i + 1])) and not n.group(1) \
                        and n.group(2)[0].isdigit() == ordered:
                    i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>")
            continue

        para = []
        while i < len(lines) and lines[i].strip() and not (
                _HEADING.match(lines[i]) or _LIST_ITEM.match(lines[i])
                or lines[i].strip().startswith(("|", ">", "```", "<!--"))):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


STYLE = """
:root { color-scheme: light dark; --fg: #1f2328; --bg: #ffffff; --muted: #59636e;
        --line: #d1d9e0; --code: #f6f8fa; --link: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6edf3; --bg: #0d1117; --muted: #9198a1; --line: #3d444d;
          --code: #151b23; --link: #4493f8; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 32px 20px 80px; }
.banner { font-size: 14px; color: var(--muted); border: 1px solid var(--line);
          border-radius: 6px; padding: 10px 14px; margin-bottom: 24px; }
h1, h2 { border-bottom: 1px solid var(--line); padding-bottom: .3em; }
h1 { font-size: 2em; } h2 { font-size: 1.5em; margin-top: 2em; }
a { color: var(--link); }
code { font: 85% ui-monospace, SFMono-Regular, Consolas, monospace; background: var(--code);
       padding: .15em .35em; border-radius: 4px; }
pre { background: var(--code); padding: 14px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
pre.mermaid { background: none; text-align: center; }
table { border-collapse: collapse; display: block; overflow-x: auto; margin: 16px 0; }
th, td { border: 1px solid var(--line); padding: 6px 12px; text-align: left; vertical-align: top; }
thead th { background: var(--code); }
blockquote { margin: 0; padding: 0 1em; color: var(--muted); border-left: 4px solid var(--line); }
hr { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
"""


def render(md: str) -> str:
    title = next((m.group(2) for line in md.splitlines() if (m := _HEADING.match(line))),
                 "README")
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        # An empty inline icon: otherwise every visit requests /favicon.ico and logs a 404.
        '<link rel="icon" href="data:,">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{STYLE}</style>",
        "</head>",
        "<body>",
        "<main>",
        '<div class="banner">Generated from <a href="' + GITHUB + '">the repository</a>\'s '
        "README by <code>scripts/render_site.py</code>. Every figure comes from "
        "<code>results/</code>; this page has no content of its own.</div>",
        convert(md),
        "</main>",
        f'<script src="{MERMAID}"></script>',
        "<script>mermaid.initialize({ startOnLoad: true, theme: "
        "matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default' });</script>",
        "</body>",
        "</html>",
        "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if site/index.html is out of date")
    args = ap.parse_args()

    page = render(README.read_text(encoding="utf-8"))
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    if current == page:
        print("up to date  site/index.html")
        return 0
    if args.check:
        print("STALE       site/index.html\n\nThe page disagrees with README.md. Run: "
              "python scripts/render_site.py", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8", newline="\n")
    print("rewritten   site/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
