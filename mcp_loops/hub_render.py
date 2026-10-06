"""CSP-safe rendering for the doc-shaped hub capabilities (Round B2).

Specs (C2) and Liked-Results (C3) both take an on-disk file under a Project's
``loopyard/<dataDir>/`` and RENDER it in the dashboard — markdown or (arbitrary,
possibly hand-authored) HTML — without letting that content reach out to the
network or script the dashboard. Two independent guards, belt-and-braces:

* a tiny, dependency-free **markdown → sanitized HTML** renderer (headings,
  emphasis, inline + fenced code, links with a scheme allowlist, lists,
  blockquotes, rules, paragraphs) — every text run HTML-escaped, so no markup in
  the source can inject an element;
* every rendered payload is a COMPLETE standalone document carrying a strict
  ``<meta http-equiv="Content-Security-Policy">`` (``default-src 'none'`` +
  ``style-src 'unsafe-inline'`` + ``img-src data:``) so even a raw-HTML spec can
  fetch nothing. The dashboard drops the document into a ``sandbox`` iframe via
  ``srcdoc`` (no ``allow-scripts``), which independently neutralizes any script.

Pure + deterministic: text in, ``{kind, title, document}`` out. No filesystem,
no server, no clock — same house style as :mod:`mcp_loops.loopyard_import`.
"""

from __future__ import annotations

import re
from typing import Any

# The one strict policy every rendered document carries. No default source, no
# scripts, styles inline-only (we ship our own <style>), images only as data:
# URIs. A raw-HTML spec that tries to <script src>, fetch(), or load a remote
# <img> gets nothing — before the iframe sandbox even applies.
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'")

# Link schemes we allow through render_markdown. Anything else (javascript:,
# data:, vbscript:, …) becomes an inert <span> — no href at all.
_SAFE_SCHEME_RE = re.compile(r"^(?:https?:|mailto:|#|/|\./|\.\./)", re.IGNORECASE)

_HTML_EXTS = {".html", ".htm"}
_MD_EXTS = {".md", ".markdown", ".mdown"}

# ── inline / block markdown regexes ───────────────────────────────────────────
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_ULIST_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_RULE_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)")


def esc(s: Any) -> str:
    """HTML-escape a text run (the ONE escaper; used for every emitted text)."""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def classify(name: str) -> str:
    """Extension → render kind: ``md`` | ``html`` | ``text`` (the safe fallback
    for anything else — rendered verbatim in a <pre>)."""
    low = str(name or "").lower()
    dot = low.rfind(".")
    ext = low[dot:] if dot >= 0 else ""
    if ext in _HTML_EXTS:
        return "html"
    if ext in _MD_EXTS:
        return "md"
    return "text"


# ── inline markdown → safe HTML (operates on ALREADY-ESCAPED text) ────────────
def _render_inline(text: str) -> str:
    """Inline spans on one already-HTML-escaped line: `code`, links, **bold**,
    *italic*. Code is matched FIRST and its content re-escaped-safe so emphasis
    markers inside code are left literal."""
    out: list[str] = []
    pos = 0
    for m in _CODE_RE.finditer(text):
        out.append(_emphasis(text[pos:m.start()]))
        out.append("<code>" + m.group(1) + "</code>")
        pos = m.end()
    out.append(_emphasis(text[pos:]))
    return "".join(out)


def _emphasis(text: str) -> str:
    """Links + bold + italic on a code-free, already-escaped run."""
    def link(m: "re.Match[str]") -> str:
        label, href = m.group(1), m.group(2)
        # href arrives HTML-escaped; test the scheme on the unescaped-enough form.
        raw = href.replace("&amp;", "&")
        if _SAFE_SCHEME_RE.match(raw):
            return f'<a href="{href}" rel="noopener noreferrer">{label}</a>'
        return f"<span>{label}</span>"   # unsafe scheme → inert, no href
    text = _LINK_RE.sub(link, text)
    text = _BOLD_RE.sub(lambda m: "<strong>" + (m.group(1) or m.group(2)) + "</strong>", text)
    text = _ITALIC_RE.sub(lambda m: "<em>" + (m.group(1) or m.group(2)) + "</em>", text)
    return text


def render_markdown(text: str) -> str:
    """A small, dependency-free markdown → safe HTML FRAGMENT renderer.

    Handles ATX headings, unordered/ordered lists, blockquotes, fenced (``` /
    ~~~) and indented code, horizontal rules, and paragraphs, with inline code /
    links (scheme-allowlisted) / bold / italic. EVERY text run is HTML-escaped
    before any markup is emitted, so nothing in ``text`` can inject an element.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html: list[str] = []
    para: list[str] = []
    list_kind = ""   # "ul" | "ol" | ""
    i = 0

    def flush_para() -> None:
        if para:
            html.append("<p>" + _render_inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            html.append(f"</{list_kind}>")
            list_kind = ""

    while i < len(lines):
        raw = lines[i]

        fence = _FENCE_RE.match(raw)
        if fence:
            flush_para(); close_list()
            marker = fence.group(1)[0]
            buf: list[str] = []
            i += 1
            while i < len(lines) and not re.match(rf"^\s*{re.escape(marker)}{{3,}}\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1   # consume closing fence (or EOF)
            html.append("<pre><code>" + esc("\n".join(buf)) + "</code></pre>")
            continue

        if not raw.strip():
            flush_para(); close_list()
            i += 1
            continue

        if _RULE_RE.match(raw):
            flush_para(); close_list()
            html.append("<hr>")
            i += 1
            continue

        h = _HEADING_RE.match(raw)
        if h:
            flush_para(); close_list()
            level = len(h.group(1))
            html.append(f"<h{level}>" + _render_inline(esc(h.group(2))) + f"</h{level}>")
            i += 1
            continue

        q = _QUOTE_RE.match(raw)
        if q:
            flush_para(); close_list()
            quote: list[str] = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quote.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            html.append("<blockquote>" + _render_inline(esc(" ".join(quote))) + "</blockquote>")
            continue

        ul = _ULIST_RE.match(raw)
        ol = _OLIST_RE.match(raw)
        if ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if list_kind != want:
                close_list()
                html.append(f"<{want}>")
                list_kind = want
            item = (ul or ol).group(1)
            html.append("<li>" + _render_inline(esc(item)) + "</li>")
            i += 1
            continue

        close_list()
        para.append(esc(raw.strip()))
        i += 1

    flush_para(); close_list()
    return "\n".join(html)


# ── framing: a complete, self-contained, network-free document ────────────────
_DOC_STYLE = """
  :root { color-scheme: dark light; }
  body { font: 14px/1.6 system-ui, -apple-system, sans-serif; margin: 0;
         padding: 18px 20px; background: #0f1115; color: #e6e6e6; }
  h1,h2,h3,h4,h5,h6 { line-height: 1.25; margin: 1.2em 0 .5em; }
  h1 { font-size: 1.5em; } h2 { font-size: 1.28em; } h3 { font-size: 1.12em; }
  a { color: #7fb2ff; }
  code { background: #171a21; padding: 1px 5px; border-radius: 4px;
         font-family: ui-monospace, Menlo, monospace; font-size: .92em; }
  pre { background: #12151b; border: 1px solid #232833; border-radius: 8px;
        padding: 12px 14px; overflow: auto; }
  pre code { background: none; padding: 0; }
  blockquote { border-left: 3px solid #2a2f3a; margin: .8em 0; padding: .1em 14px;
               color: #b9c1d0; }
  hr { border: 0; border-top: 1px solid #232833; margin: 1.4em 0; }
  ul, ol { padding-left: 1.5em; }
  img { max-width: 100%; }
  table { border-collapse: collapse; } td, th { border: 1px solid #232833; padding: 4px 8px; }
"""


def frame_document(body_html: str, *, title: str = "") -> str:
    """Wrap a rendered HTML fragment in a complete, strict-CSP, offline document
    — safe to serve as an iframe ``srcdoc``."""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{_CSP}\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><style>{_DOC_STYLE}</style></head>"
        f"<body>{body_html}</body></html>"
    )


def _inject_csp(raw_html: str) -> str:
    """Ensure a raw-HTML document carries our strict CSP meta. If it has a
    ``<head>`` we splice the meta in right after it; otherwise we wrap the whole
    payload in a framing document body. Defence-in-depth on top of the iframe
    sandbox the dashboard applies."""
    meta = f"<meta http-equiv=\"Content-Security-Policy\" content=\"{_CSP}\">"
    m = re.search(r"<head[^>]*>", raw_html, flags=re.IGNORECASE)
    if m:
        return raw_html[:m.end()] + meta + raw_html[m.end():]
    # No <head>: treat the payload as body content inside our own framed doc so it
    # still gets the meta + a charset. (The sandbox is still the primary guard.)
    return frame_document(raw_html, title="")


def render_file(name: str, text: str) -> dict[str, Any]:
    """Render a loopyard file → ``{kind, title, document}`` where ``document`` is
    a complete, CSP-carrying HTML string safe to hand an iframe ``srcdoc``.

    * ``md``   → our sanitized markdown renderer, framed.
    * ``html`` → the raw document with a strict CSP meta spliced in (the iframe
      sandbox neutralizes scripts; the CSP blocks any network fetch).
    * ``text`` → shown verbatim in a <pre>, framed.
    """
    kind = classify(name)
    title = str(name or "")
    if kind == "html":
        return {"kind": "html", "title": title, "document": _inject_csp(text or "")}
    if kind == "md":
        return {"kind": "md", "title": title,
                "document": frame_document(render_markdown(text or ""), title=title)}
    return {"kind": "text", "title": title,
            "document": frame_document("<pre>" + esc(text or "") + "</pre>", title=title)}
