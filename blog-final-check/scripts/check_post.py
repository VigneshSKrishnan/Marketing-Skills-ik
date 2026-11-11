#!/usr/bin/env python3
"""Automatic pre-publish checks for one ImageKit blog post.

Reads a Markdoc post (src/content/blog/<slug>.mdoc) from a local clone of the
blog repo and prints pass / fail / warn / skip results for every [Auto] check
of the blog refresh checklist. It never modifies the post.

Usage:
    python3 check_post.py <path/to/post.mdoc> [--keyword "target keyword"]
                          [--base-ref origin/dev] [--no-network] [--json]

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import difflib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SKILL_DIR, "data")
SITE = "https://imagekit.io"
TITLE_MAX = 60
META_DESC_MAX = 155
SLUG_MAX_WORDS = 6
MIN_IMAGES = 3
MIN_NEW_INTERNAL_LINKS = 3
HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 blog-final-check"
)

# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

SECTIONS = {
    "content": "1. Content",
    "seo": "2. On-page SEO",
    "links": "3. Links",
    "trust": "5. Readability and trust",
    "publishing": "6. Publishing",
}


class Results:
    def __init__(self):
        self.items = []

    def add(self, section, check, status, message, line=None, fix=None):
        self.items.append(
            {
                "section": section,
                "check": check,
                "status": status,
                "line": line,
                "message": message,
                "fix": fix,
            }
        )


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def parse_scalar(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw)
        except ValueError:
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [parse_scalar(x) for x in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            try:
                return json.loads(raw)
            except ValueError:
                return inner
        return inner.replace("''", "'")
    return raw


def parse_front_matter(lines):
    """Return (fields, field_lines, body_start_index). Minimal YAML subset."""
    fields, field_lines = {}, {}
    if not lines or lines[0].strip() != "---":
        return fields, field_lines, 0
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return fields, field_lines, 0
    key = None
    for i in range(1, end):
        line = lines[i]
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            val = m.group(2)
            field_lines[key] = i + 1
            if val.strip() in ("", ">", "|", ">-", "|-"):
                fields[key] = "" if val.strip() == "" else ""
            else:
                fields[key] = parse_scalar(val)
        elif key and re.match(r"^\s+-\s+", line):
            item = parse_scalar(re.sub(r"^\s+-\s+", "", line))
            if not isinstance(fields.get(key), list):
                fields[key] = []
            fields[key].append(item)
        elif key and line.startswith((" ", "\t")) and isinstance(fields.get(key), str):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields, field_lines, end + 1


def parse_date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def body_lines_without_code(lines, start):
    """Yield (line_no, text) for body lines outside fenced code blocks."""
    in_fence = False
    for i in range(start, len(lines)):
        text = lines[i]
        if re.match(r"^\s*(```|~~~)", text):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield i + 1, text


LINK_RE = re.compile(r"(?<!!)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"([^\"]*)\")?\s*\)")
IMG_HTML_RE = re.compile(r"<img\b[^>]*>", re.I)
A_HTML_RE = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
ATTR_RE = re.compile(r"\b([\w-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")


def html_attrs(tag):
    return {k.lower(): v[1:-1] for k, v in ATTR_RE.findall(tag)}


def extract_bracketed(text, start):
    """Given text[start] == '[', return the substring up to the matching ']'."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def strip_markup(text):
    """Turn a line of Markdoc/Markdown into visible plain text."""
    text = re.sub(r"\{%.*?%\}", " ", text)
    text = IMG_MD_RE.sub(" ", text)
    text = IMG_HTML_RE.sub(" ", text)
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text)
    text = re.sub(r"[*_`]+", "", text)
    text = text.replace("\\", "")
    return text


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------

DAM_PHRASE = "digital asset management"


def light_stem(word):
    """Very small stemmer: organizing / organized / organizes / organize -> organiz."""
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("es") and word[-3] in "sxz":
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def norm_tokens(text, stem=True):
    text = text.lower().replace("’", "'")
    text = re.sub(r"\bdigital[\s-]+asset[\s-]+management\b", " dam ", text)
    text = re.sub(r"\bdams\b", " dam ", text)
    text = re.sub(r"\bvs\.?\b|\bversus\b", " vs ", text)
    text = re.sub(r"'s\b", "", text)
    toks = re.findall(r"[a-z0-9]+", text)
    return [light_stem(t) for t in toks] if stem else toks


def plain_tokens(text):
    """Lowercase words only; ignores punctuation such as 'vs.' or hyphens."""
    text = re.sub(r"\bvs\.?(?![a-z])|\bversus\b", " vs ", text.lower().replace("’", "'"))
    return re.findall(r"[a-z0-9]+", text)


def contains_seq(hay, needle):
    if not needle:
        return False
    n = len(needle)
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def keyword_match(text, keyword):
    """Return 'exact', 'variant' or None."""
    if not text or not keyword:
        return None
    t = re.sub(r"[\s\-_]+", " ", text.lower())
    k = re.sub(r"[\s\-_]+", " ", keyword.lower()).strip()
    if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t):
        return "exact"
    if contains_seq(plain_tokens(text), plain_tokens(keyword)):
        return "exact"
    if contains_seq(norm_tokens(text), norm_tokens(keyword)):
        return "variant"
    return None


def anchor_matches(anchor, keyword):
    """Anchor text is the keyword or a close variation of it."""
    a, k = norm_tokens(anchor), norm_tokens(keyword)
    if not a or not k:
        return False
    if contains_seq(a, k):
        return True
    # all keyword words present (any order), allowing small filler words
    if set(k) <= set(a):
        return True
    # allow one dropped connector word ("for", "of", "and", "in", "the")
    fillers = {"for", "of", "and", "in", "the", "a", "to", "with"}
    k2 = [w for w in k if w not in fillers]
    a2 = [w for w in a if w not in fillers]
    return bool(k2) and contains_seq(a2, k2)


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------


def blog_path_key(path):
    """Normalise '/blog/<slug>/' style keys."""
    path = path.strip()
    if not path:
        return ""
    parsed = urllib.parse.urlparse(path)
    p = parsed.path if parsed.scheme or path.startswith("/") else "/" + path
    p = p.split("#")[0]
    if not p.endswith("/"):
        p += "/"
    return p.lower()


def load_keywords():
    path = os.path.join(DATA_DIR, "keywords.csv")
    kw = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("url") or "").strip()
            keyword = (row.get("keyword") or "").strip()
            if url and keyword:
                kw[blog_path_key(url)] = keyword
    return kw


def load_killed():
    path = os.path.join(DATA_DIR, "killed-posts.txt")
    out = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(blog_path_key(line))
    return out


def load_authors(repo_root):
    path = os.path.join(repo_root, "src", "data", "authors.ts")
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    m = re.search(r"export\s+const\s+authors[^=]*=\s*\{", text)
    if not m:
        return None
    # walk the object, collecting keys at depth 1
    keys, depth, i = set(), 1, m.end()
    key_re = re.compile(r"\s*[\"']?([\w.-]+)[\"']?\s*:\s*\{")
    while i < len(text) and depth > 0:
        c = text[i]
        if c in "\"'":
            q = c
            j = i + 1
            while j < len(text) and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            if depth == 1:
                km = key_re.match(text, i)
                if km:
                    keys.add(km.group(1))
            i = j + 1
            continue
        if depth == 1 and (c.isalpha() or c == "_"):
            km = key_re.match(text, i)
            if km:
                keys.add(km.group(1))
                i = km.end() - 1
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return keys


# --------------------------------------------------------------------------
# Extraction of links and images
# --------------------------------------------------------------------------


def extract_links(lines, start):
    links = []
    for ln, text in body_lines_without_code(lines, start):
        for m in LINK_RE.finditer(text):
            links.append({"line": ln, "anchor": strip_markup(m.group(1)).strip(), "href": m.group(2)})
        for m in A_HTML_RE.finditer(text):
            links.append({"line": ln, "anchor": strip_markup(m.group(2)).strip(), "href": m.group(1)})
        for m in re.finditer(r"<(https?://[^>\s]+)>", text):
            links.append({"line": ln, "anchor": m.group(1), "href": m.group(1)})
    return links


def extract_images(lines, start):
    images = []
    for ln, text in body_lines_without_code(lines, start):
        for m in IMG_MD_RE.finditer(text):
            images.append({"line": ln, "kind": "markdown", "alt": m.group(1), "src": m.group(2)})
        for m in IMG_HTML_RE.finditer(text):
            attrs = html_attrs(m.group(0))
            images.append(
                {"line": ln, "kind": "html", "alt": attrs.get("alt"), "src": attrs.get("src", "")}
            )
        for gm in re.finditer(r"\{%\s*gallery\b", text):
            idx = text.find("images=", gm.end())
            if idx < 0:
                continue
            arr_start = text.find("[", idx)
            arr = extract_bracketed(text, arr_start) if arr_start >= 0 else None
            items = []
            if arr:
                try:
                    items = json.loads(arr)
                except ValueError:
                    items = [
                        {"src": s, "alt": a}
                        for s, a in re.findall(r"\"src\"\s*:\s*\"([^\"]*)\".*?\"alt\"\s*:\s*\"([^\"]*)\"", arr)
                    ]
            for it in items:
                if isinstance(it, dict):
                    images.append(
                        {"line": ln, "kind": "gallery", "alt": it.get("alt"), "src": it.get("src", "")}
                    )
    return images


def classify_href(href):
    """Return (kind, normalised_url). kind in blog|site|external|anchor|mailto|other."""
    href = href.strip()
    low = href.lower()
    if low.startswith("#"):
        return "anchor", href
    if low.startswith(("mailto:", "tel:")):
        return "mailto", href
    if low.startswith("//"):
        href = "https:" + href
        low = href.lower()
    if low.startswith("/"):
        url = SITE + href
        path = urllib.parse.urlparse(url).path.lower()
        return ("blog" if path.startswith("/blog/") else "site"), url
    if re.match(r"^https?://", low):
        host = urllib.parse.urlparse(href).netloc.lower()
        if host in ("imagekit.io", "www.imagekit.io"):
            path = urllib.parse.urlparse(href).path.lower()
            return ("blog" if path.startswith("/blog/") else "site"), href
        return "external", href
    return "other", href


NON_POST_BLOG_PREFIXES = ("tag", "author", "page", "rss.xml")


def blog_slug(url):
    path = urllib.parse.urlparse(url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "blog":
        return None
    if parts[1].lower() in NON_POST_BLOG_PREFIXES:
        return None
    return urllib.parse.unquote(parts[1])


# --------------------------------------------------------------------------
# Image file-name heuristics
# --------------------------------------------------------------------------

GENERIC_NAME_RE = re.compile(
    r"^(image|img|pic|picture|photo|screenshot|screen[-_ ]?shot|untitled|unnamed|download|"
    r"file|copy|frame|group|artboard|asset|banner|graphic|capture|dsc|dscn|pxl|gopr|whatsapp[-_ ]?image)"
    r"[\s_\-.()0-9]*(copy)?[\s_\-.()0-9]*$",
    re.I,
)
CAMERA_RE = re.compile(r"^(img|dsc|dscn|pxl|mvimg|gopr)[_-]?\d{3,}", re.I)
IK_SUFFIX_RE = re.compile(r"_[A-Za-z0-9\-]{8,11}$")  # ImageKit upload suffix e.g. _mUBcYig7G
VOWEL_WORD_RE = re.compile(r"^[a-z]{3,}$")


def looks_random(token):
    if len(token) < 10:
        return False
    has_digit = any(c.isdigit() for c in token)
    has_upper = any(c.isupper() for c in token[1:])
    has_lower = any(c.islower() for c in token)
    if re.fullmatch(r"[0-9a-f]{12,}", token.lower()):
        return True
    return has_digit and (has_upper or has_lower) and len(token) >= 10 or (has_upper and has_lower and len(token) >= 14)


def judge_filename(src):
    """Return (ok, reason, name)."""
    path = urllib.parse.urlparse(src).path
    name = urllib.parse.unquote(os.path.basename(path))
    if not name:
        return False, "no file name in URL", name
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    stem_clean = IK_SUFFIX_RE.sub("", stem) if re.search(r"[a-z]{3}", stem.lower()) else stem
    if GENERIC_NAME_RE.match(stem_clean) or CAMERA_RE.match(stem_clean):
        return False, "generic file name", name
    # e.g. "Screenshot-2023-06-28-at-10.44.46", "Screen Shot 2020-01-01 at 9.15 AM", "image-20240515-091936"
    gm = re.match(r"^(screen[\s_-]?shot|screenshot|image|img|photo|untitled|unnamed|capture)(.*)$", stem_clean, re.I)
    if gm and not re.search(r"[a-z]{3,}", re.sub(r"\b(at|am|pm|copy)\b", " ", gm.group(2).lower())):
        return False, "generic file name", name
    tokens = []
    for t in re.split(r"[\s_\-.()+]+", stem_clean):
        if not t:
            continue
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", t)
        # treat camelCase as separate words only when it splits cleanly into words
        if len(parts) > 1 and "".join(parts) == t and all(len(x) >= 2 or x.isdigit() for x in parts) \
                and sum(len(x) for x in parts if x.isalpha() and re.search(r"[aeiouy]", x.lower())) >= len(t) * 0.7:
            tokens.extend(parts)
        else:
            tokens.append(t)
    # a "word" is lowercase, Capitalised or a short ALL-CAPS token; mixed case like "lsipYGHO" is not
    words = [t for t in tokens if VOWEL_WORD_RE.match(t.lower()) and re.search(r"[aeiouy]", t.lower())
             and (t.islower() or t.istitle() or (t.isupper() and len(t) <= 8))]
    random_tokens = [t for t in tokens if looks_random(t)]
    non_word_chars = sum(len(t) for t in tokens if t not in words and not t.isdigit())
    if len(stem_clean) >= 30 and len(words) < 2 and non_word_chars > 0.5 * sum(len(t) for t in tokens):
        return False, "looks like a random string", name
    if random_tokens and len(words) < 2:
        return False, "looks like a random string", name
    if not words:
        return False, "has no descriptive words", name
    if len(words) == 1 and len(tokens) == 1 and len(words[0]) <= 6:
        return False, "only one short word; describe what the image shows", name
    return True, "", name


def judge_alt(alt, keyword, src):
    """Return (status, reason). status: pass|fail|warn."""
    if alt is None or not alt.strip():
        return "fail", "missing alt text"
    a = alt.strip()
    if re.search(r"\.(png|jpe?g|gif|webp|svg|avif|bmp|tiff?)$", a, re.I):
        return "fail", "alt text is a file name"
    base = os.path.basename(urllib.parse.urlparse(src or "").path)
    if base and re.sub(r"\.[^.]+$", "", base).lower() == a.lower():
        return "fail", "alt text is the file name"
    if re.fullmatch(r"[\w\-]+", a) and ("_" in a or "-" in a):
        return "fail", "alt text looks like a file name or slug"
    if keyword and norm_tokens(a) == norm_tokens(keyword):
        return "fail", "alt text is just the keyword"
    if re.fullmatch(r"(image|img|picture|photo|screenshot|graphic|banner|icon|logo)\s*\d*", a, re.I):
        return "fail", "alt text is a generic word"
    if len(a.split()) < 3:
        return "warn", "alt text is very short; describe what the image shows"
    return "pass", ""


# --------------------------------------------------------------------------
# HTTP checks
# --------------------------------------------------------------------------


def make_ssl_context():
    ctx = ssl.create_default_context()
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        f = os.environ.get(env)
        if f and os.path.exists(f):
            try:
                ctx.load_verify_locations(cafile=f)
            except (ssl.SSLError, OSError):
                pass
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_status(url, ctx):
    """Follow redirects manually. Returns dict with status, final_url, error, network."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=ctx), _NoRedirect()
    )
    current = url
    for _ in range(10):
        status, location, err = None, None, None
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(
                current,
                method=method,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en"},
            )
            try:
                with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                    status = resp.status
                    location = None
            except urllib.error.HTTPError as e:
                status = e.code
                location = e.headers.get("Location") if e.headers else None
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as e:
                reason = getattr(e, "reason", e)
                return {"status": None, "final_url": current, "error": str(reason), "network": True}
            # Some servers reject HEAD; retry with GET
            if method == "HEAD" and status in (400, 403, 404, 405, 406, 429, 500, 501, 502, 503):
                continue
            break
        if status in (301, 302, 303, 307, 308) and location:
            current = urllib.parse.urljoin(current, location)
            continue
        return {"status": status, "final_url": current, "error": None, "network": False}
    return {"status": None, "final_url": current, "error": "too many redirects", "network": False}


def same_page(a, b):
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    ha = pa.netloc.lower().removeprefix("www.")
    hb = pb.netloc.lower().removeprefix("www.")
    return ha == hb and pa.path.rstrip("/").lower() == pb.path.rstrip("/").lower()


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


def git(args, cwd):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if out.returncode != 0:
        return None, out.stderr.strip()
    return out.stdout, None


# --------------------------------------------------------------------------
# Main checks
# --------------------------------------------------------------------------


def run(args):
    res = Results()
    post_path = os.path.abspath(args.post)
    if not os.path.isfile(post_path):
        print(f"ERROR: post file not found: {args.post}", file=sys.stderr)
        sys.exit(2)

    post_dir = os.path.dirname(post_path)
    top, err = git(["rev-parse", "--show-toplevel"], post_dir)
    repo_root = top.strip() if top else None
    if not repo_root:
        # Fall back to walking up to a folder that contains src/content/blog
        d = post_dir
        while d != os.path.dirname(d):
            if os.path.isdir(os.path.join(d, "src", "content", "blog")):
                repo_root = d
                break
            d = os.path.dirname(d)
    blog_dir = os.path.join(repo_root, "src", "content", "blog") if repo_root else post_dir
    rel_path = os.path.relpath(post_path, repo_root) if repo_root else args.post

    raw = open(post_path, encoding="utf-8").read()
    lines = raw.splitlines()
    fm, fm_lines, body_start = parse_front_matter(lines)
    slug = re.sub(r"\.(mdoc|md|mdx)$", "", os.path.basename(post_path))
    post_key = f"/blog/{slug}/"
    live_url = f"{SITE}/blog/{slug}/"
    today = dt.date.today()
    current_year = today.year

    keywords = load_keywords()
    killed = load_killed()

    keyword = args.keyword or keywords.get(post_key.lower())
    keyword_source = "--keyword argument" if args.keyword else ("data/keywords.csv" if keyword else None)

    title = fm.get("title") or ""
    description = fm.get("description") or ""
    meta_title = fm.get("meta_title") or ""
    meta_description = fm.get("meta_description") or ""
    eff_meta_title = (meta_title or title).strip()
    eff_meta_desc = (meta_description or description).strip()

    body = list(body_lines_without_code(lines, body_start))
    headings = []
    for ln, text in body:
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", text)
        if m:
            headings.append({"line": ln, "level": len(m.group(1)), "text": strip_markup(m.group(2)).strip()})

    # ---------------------------------------------------------------- content
    year_re = re.compile(r"(?<![\d/.\-#])(20\d{2})(?![\d/+%x×,]|\s*(?:px|kb|mb|gb|ms)\b)", re.I)
    old_year = lambda y: 2000 <= int(y) < current_year  # noqa: E731

    for field in ("title", "meta_title", "description", "meta_description"):
        val = fm.get(field)
        if isinstance(val, str):
            yrs = [y for y in year_re.findall(val) if old_year(y)]
            if yrs:
                res.add(
                    "content", "Old year references", "fail",
                    f"`{field}` mentions {', '.join(sorted(set(yrs)))}: \"{val.strip()}\"",
                    fm_lines.get(field),
                    f"Update to {current_year} or remove the year.",
                )
    body_year_hits = 0
    for ln, text in body:
        visible = URL_RE.sub(" ", strip_markup(text))
        visible = re.sub(r"\{%.*?%\}", " ", visible)
        yrs = [y for y in year_re.findall(visible) if old_year(y)]
        if not yrs:
            continue
        body_year_hits += 1
        is_heading = re.match(r"^\s{0,3}#{1,6}\s", text) is not None
        snippet = strip_markup(text).strip()
        if len(snippet) > 160:
            pos = snippet.find(yrs[0])
            snippet = ("…" if pos > 70 else "") + snippet[max(0, pos - 70) : pos + 70] + "…"
        res.add(
            "content", "Old year references", "fail" if is_heading else "warn",
            f"{'Heading' if is_heading else 'Body'} mentions {', '.join(sorted(set(yrs)))}: \"{snippet}\"",
            ln,
            f"Update to {current_year} if it describes the present; keep it only if it is a historical date.",
        )
    if not any(i["check"] == "Old year references" for i in res.items):
        res.add("content", "Old year references", "pass", f"No years before {current_year} in title, meta or body.")

    # ---------------------------------------------------------------- SEO: keyword
    first_words = []
    first_words_end_line = None
    for ln, text in body:
        if re.match(r"^\s{0,3}#{1,6}\s", text):
            pass  # headings count as body words too
        words = strip_markup(text).split()
        if words:
            first_words.extend(words)
            first_words_end_line = ln
        if len(first_words) >= 100:
            break
    first100 = " ".join(first_words[:100])

    if not keyword:
        res.add(
            "seo", "Target keyword", "skip",
            f"{post_key} is not in data/keywords.csv, so the keyword checks could not run. "
            "Ask the writer for the target keyword and re-run with --keyword.",
        )
    else:
        def kw_check(check, where, text, line, fix):
            m = keyword_match(text, keyword)
            if m == "exact":
                res.add("seo", check, "pass", f"Keyword \"{keyword}\" is in {where}.", line)
            elif m == "variant":
                res.add(
                    "seo", check, "warn",
                    f"Only a variant of \"{keyword}\" is in {where} (e.g. plural or DAM / digital asset management).",
                    line, fix,
                )
            else:
                res.add("seo", check, "fail", f"Keyword \"{keyword}\" is missing from {where}.", line, fix)

        kw_check("Keyword in title", f"the title (\"{title}\")", title, fm_lines.get("title"),
                 f"Work \"{keyword}\" into the title.")
        kw_check("Keyword in description", f"the description (\"{description}\")", description,
                 fm_lines.get("description"), f"Work \"{keyword}\" into `description`.")
        kw_check("Keyword in first 100 words", "the first 100 words of the body", first100,
                 body[0][0] if body else None,
                 f"Use \"{keyword}\" in the opening paragraph (first 100 words end around line {first_words_end_line}).")
        kw_check("Keyword in meta title",
                 f"the meta title ({'`meta_title`' if meta_title else '`title`, since `meta_title` is empty'}: \"{eff_meta_title}\")",
                 eff_meta_title, fm_lines.get("meta_title") or fm_lines.get("title"),
                 f"Add \"{keyword}\" to {'`meta_title`' if meta_title else '`title` (or set `meta_title`)'}.")
        kw_check("Keyword in meta description",
                 f"the meta description ({'`meta_description`' if meta_description else '`description`, since `meta_description` is empty'}: \"{eff_meta_desc}\")",
                 eff_meta_desc, fm_lines.get("meta_description") or fm_lines.get("description"),
                 f"Add \"{keyword}\" to {'`meta_description`' if meta_description else '`description` (or set `meta_description`)'}.")

        slug_tokens = norm_tokens(slug.replace("_", "-"))
        kw_tokens = norm_tokens(keyword)
        if contains_seq(slug_tokens, kw_tokens) or set(kw_tokens) <= set(slug_tokens):
            res.add("seo", "Keyword in URL slug", "pass", f"Slug `{slug}` contains the keyword.")
        else:
            res.add(
                "seo", "Keyword in URL slug", "warn",
                f"Slug `{slug}` does not contain \"{keyword}\".",
                None,
                f"Ideal slug: `{re.sub(r'[^a-z0-9]+', '-', keyword.lower()).strip('-')}`. "
                "Only change the URL if the post is being redirected.",
            )

    slug_words = [w for w in re.split(r"[-_]+", slug) if w]
    if len(slug_words) > SLUG_MAX_WORDS:
        res.add(
            "seo", "Slug is compact", "warn",
            f"Slug `{slug}` has {len(slug_words)} words (aim for {SLUG_MAX_WORDS} or fewer).",
            None,
            "Only change the URL if the post is being redirected.",
        )
    else:
        res.add("seo", "Slug is compact", "pass", f"Slug `{slug}` has {len(slug_words)} words.")

    # Lengths
    if len(title) > TITLE_MAX:
        res.add("seo", "Title length", "warn", f"Title is {len(title)} characters (guideline: about {TITLE_MAX} or fewer).",
                fm_lines.get("title"), "Shorten the title.")
    else:
        res.add("seo", "Title length", "pass", f"Title is {len(title)} characters.")
    if meta_title:
        if len(meta_title.strip()) > TITLE_MAX:
            res.add("seo", "Meta title length", "warn",
                    f"`meta_title` is {len(meta_title.strip())} characters (guideline: about {TITLE_MAX} or fewer).",
                    fm_lines.get("meta_title"), "Shorten `meta_title`.")
        else:
            res.add("seo", "Meta title length", "pass", f"`meta_title` is {len(meta_title.strip())} characters.")
        if meta_title != meta_title.strip():
            res.add("seo", "Meta title length", "warn", "`meta_title` has leading or trailing spaces.",
                    fm_lines.get("meta_title"), f"Use \"{meta_title.strip()}\".")
    if len(eff_meta_desc) > META_DESC_MAX:
        res.add("seo", "Meta description length", "warn",
                f"Meta description is {len(eff_meta_desc)} characters (guideline: about {META_DESC_MAX} or fewer).",
                fm_lines.get("meta_description") or fm_lines.get("description"), "Shorten it.")
    elif eff_meta_desc:
        res.add("seo", "Meta description length", "pass", f"Meta description is {len(eff_meta_desc)} characters.")

    # H1 in body
    h1s = [h for h in headings if h["level"] == 1]
    for h in h1s:
        res.add("seo", "Single H1", "warn",
                f"Body has an H1 (\"{h['text']}\"), but `title` is already rendered as the H1.",
                h["line"], "Change it to an H2 (`## `).")

    # Images
    images = extract_images(lines, body_start)
    if len(images) >= MIN_IMAGES:
        res.add("seo", "At least 3 images", "pass", f"{len(images)} images in the body.")
    else:
        res.add("seo", "At least 3 images", "fail",
                f"Only {len(images)} image(s) in the body (need at least {MIN_IMAGES}).",
                None, f"Add {MIN_IMAGES - len(images)} more relevant image(s), e.g. current ImageKit UI screenshots.")

    alt_problems = name_problems = 0
    for img in images:
        st, reason = judge_alt(img["alt"], keyword, img["src"])
        short_src = os.path.basename(urllib.parse.urlparse(img["src"]).path) or img["src"]
        if st != "pass":
            alt_problems += 1
            res.add("seo", "Image alt text", st,
                    f"{img['kind']} image `{short_src}`: {reason} (alt: \"{img['alt'] or ''}\").",
                    img["line"],
                    "Write a short sentence describing what the image shows"
                    + ("; for Markdown images this text is also the caption shown below the image." if img["kind"] == "markdown" else "."))
        if img["kind"] == "html":
            res.add("seo", "Image alt text", "warn",
                    f"`<img>` tag for `{short_src}`: no caption will show below this image.",
                    img["line"], "Use Markdown `![description](src)` if the image needs a caption.")
        ok, reason, name = judge_filename(img["src"])
        if not ok:
            name_problems += 1
            res.add("seo", "Image file names", "fail", f"`{name}`: {reason}.", img["line"],
                    "Re-upload with a descriptive, hyphenated name (e.g. `imagekit-dam-search-filters.png`) and update the src.")
    if images and not alt_problems:
        res.add("seo", "Image alt text", "pass", "Every image has descriptive alt text.")
    if images and not name_problems:
        res.add("seo", "Image file names", "pass", "All image file names are descriptive.")

    # Dates
    pub = parse_date(fm.get("pubDate"))
    upd = parse_date(fm.get("updatedDate"))
    if not fm.get("updatedDate"):
        res.add("seo", "updatedDate later than pubDate", "fail", "`updatedDate` is not set, so no \"Updated on\" date will show.",
                fm_lines.get("pubDate"), f"Add `updatedDate: \"{today.isoformat()}\"` (or the planned publish date).")
    elif not upd:
        res.add("seo", "updatedDate later than pubDate", "fail", f"`updatedDate` (\"{fm.get('updatedDate')}\") is not a valid date.",
                fm_lines.get("updatedDate"), "Use the format YYYY-MM-DD.")
    elif pub and upd <= pub:
        res.add("seo", "updatedDate later than pubDate", "fail",
                f"`updatedDate` ({upd}) is not later than `pubDate` ({pub}); the \"Updated on\" date will not show.",
                fm_lines.get("updatedDate"), f"Set `updatedDate: \"{today.isoformat()}\"` (or the planned publish date).")
    else:
        if (today - upd).days > 30:
            res.add("seo", "updatedDate later than pubDate", "warn",
                    f"`updatedDate` ({upd}) is later than `pubDate` ({pub}) but is {(today - upd).days} days old; it looks like a previous update.",
                    fm_lines.get("updatedDate"), f"Set `updatedDate: \"{today.isoformat()}\"` (or the planned publish date).")
        elif upd > today + dt.timedelta(days=14):
            res.add("seo", "updatedDate later than pubDate", "warn",
                    f"`updatedDate` ({upd}) is more than two weeks in the future.", fm_lines.get("updatedDate"),
                    "Set it to the actual publish date.")
        else:
            res.add("seo", "updatedDate later than pubDate", "pass", f"`updatedDate` ({upd}) is later than `pubDate` ({pub}).")

    # ---------------------------------------------------------------- links
    links = extract_links(lines, body_start)
    for link in links:
        link["kind"], link["url"] = classify_href(link["href"])

    blog_links = [l for l in links if l["kind"] == "blog"]

    existing = set()
    if os.path.isdir(blog_dir):
        existing = {re.sub(r"\.(mdoc|md|mdx)$", "", f) for f in os.listdir(blog_dir) if f.endswith((".mdoc", ".md", ".mdx"))}

    def is_live_post(slug_):
        if not slug_:
            return False
        if f"/blog/{slug_.lower()}/" in killed:
            return False
        return slug_ in existing if existing else True

    # Internal link count vs base ref: links to existing, non-killed posts, counted once per post
    def count_blog_links(text):
        ls = text.splitlines()
        _, _, bs = parse_front_matter(ls)
        found = [classify_href(l["href"]) for l in extract_links(ls, bs)]
        targets = [blog_slug(u) for k, u in found if k == "blog"]
        targets = [t for t in targets if is_live_post(t)]
        return len(targets), len(set(t.lower() for t in targets))

    cur_total, cur_unique = count_blog_links(raw)
    base_ref = args.base_ref
    if not repo_root:
        res.add("links", "3+ new internal links", "skip",
                "The post is not inside a git repository, so it can't be compared with the `dev` version. Count the new internal links by hand.")
    else:
        ok_ref, ref_err = git(["rev-parse", "--verify", "--quiet", base_ref + "^{commit}"], repo_root)
        if not ok_ref:
            res.add("links", "3+ new internal links", "skip",
                    f"`{base_ref}` is not available in this clone, so the internal-link count could not be compared "
                    f"(the writer can run `git fetch origin dev` and check again). "
                    f"Current post: {cur_unique} unique internal blog links.")
        else:
            ref_date, _ = git(["log", "-1", "--format=%cs", base_ref], repo_root)
            base_label = f"`{base_ref}` (latest commit {ref_date.strip()})" if ref_date else f"`{base_ref}`"
            old, gerr = git(["show", f"{base_ref}:{rel_path}"], repo_root)
            if old is None:
                res.add("links", "3+ new internal links", "skip",
                        f"`{rel_path}` does not exist on `{base_ref}` (new post?), so there is no previous version to "
                        f"compare. Current post: {cur_unique} unique internal blog links.")
            else:
                old_total, old_unique = count_blog_links(old)
                delta = cur_unique - old_unique
                msg = (f"Links to {cur_unique} different blog posts now vs {old_unique} on {base_label} "
                       f"({delta:+d}; counting each existing, non-killed post once).")
                if old == raw:
                    msg += f" The file is identical to `{base_ref}`: no changes made yet."
                if delta >= MIN_NEW_INTERNAL_LINKS:
                    res.add("links", "3+ new internal links", "pass", msg)
                else:
                    res.add("links", "3+ new internal links", "fail", msg, None,
                            f"Add links to at least {MIN_NEW_INTERNAL_LINKS - delta} more relevant blog posts "
                            "(posts in data/keywords.csv, using their keyword as anchor text).")

    # Anchor text vs keywords.csv
    anchor_fail = anchor_pass = 0
    anchor_skipped = []
    for l in blog_links:
        s = blog_slug(l["url"])
        if not s or not is_live_post(s):
            continue  # broken or killed links are reported by their own checks
        kw = keywords.get(f"/blog/{s.lower()}/")
        if not kw:
            anchor_skipped.append(f"/blog/{s}/")
            continue
        if anchor_matches(l["anchor"], kw):
            anchor_pass += 1
        else:
            anchor_fail += 1
            res.add("links", "Internal anchor text", "fail",
                    f"Link to /blog/{s}/ uses anchor \"{l['anchor']}\"; that post's keyword is \"{kw}\".",
                    l["line"], f"Change the anchor text to \"{kw}\" (or a close variation).")
    if anchor_pass and not anchor_fail:
        res.add("links", "Internal anchor text", "pass", f"All {anchor_pass} link(s) to keyword posts use the right anchor text.")
    elif not anchor_pass and not anchor_fail:
        res.add("links", "Internal anchor text", "skip" if anchor_skipped else "pass",
                "No links to posts in data/keywords.csv." if not anchor_skipped else
                "No links to posts in data/keywords.csv, so there is no keyword to check anchor text against.")
    if anchor_skipped:
        res.add("links", "Internal anchor text", "skip",
                "Not in data/keywords.csv, anchor text not checked: " + ", ".join(sorted(set(anchor_skipped))) + ".")

    # Internal links point to existing posts
    broken_internal = 0
    for l in blog_links:
        s = blog_slug(l["url"])
        if s is None:
            continue
        if s not in existing:
            broken_internal += 1
            close = difflib.get_close_matches(s, sorted(existing), n=1, cutoff=0.5)
            res.add("links", "Internal links exist", "fail",
                    f"/blog/{s}/ has no matching src/content/blog/{s}.mdoc.", l["line"],
                    f"Did you mean https://imagekit.io/blog/{close[0]}/ ?" if close else "Remove the link or point it to an existing post.")
    if not os.path.isdir(blog_dir):
        res.add("links", "Internal links exist", "skip", "Could not find src/content/blog/ to check internal links against.")
    elif not broken_internal:
        n_int = len([l for l in blog_links if blog_slug(l["url"])])
        res.add("links", "Internal links exist", "pass",
                f"All {n_int} internal blog link(s) point to existing posts." if n_int else "No internal blog links to check.")

    # Killed posts
    killed_hits = 0
    for l in blog_links:
        if blog_path_key(urllib.parse.urlparse(l["url"]).path) in killed:
            killed_hits += 1
            res.add("links", "No links to killed posts", "fail",
                    f"Links to {urllib.parse.urlparse(l['url']).path}, which is being removed.", l["line"],
                    "Remove the link or point it to a related live post.")
    if not killed_hits:
        res.add("links", "No links to killed posts", "pass", "No links to posts in data/killed-posts.txt.")

    # Generic anchors
    bad_anchor_re = re.compile(r"^(click here|here|read more|this link|click this link|link)$|\bclick here\b", re.I)
    bad = 0
    for l in links:
        if l["kind"] in ("anchor",):
            continue
        a = re.sub(r"[.:!,\s]+$", "", l["anchor"].strip())
        if bad_anchor_re.search(a):
            bad += 1
            res.add("links", "No generic anchor text", "fail", f"Anchor \"{l['anchor']}\" → {l['href']}.", l["line"],
                    "Use descriptive anchor text that says what the linked page is about.")
    if not bad:
        res.add("links", "No generic anchor text", "pass", "No \"click here\", \"read more\", \"here\" or \"this link\" anchors.")

    # External (and other imagekit.io) links
    to_check = {}
    for l in links:
        if l["kind"] in ("external", "site"):
            to_check.setdefault(l["url"], []).append(l)
    for l in links:
        if l["kind"] == "other":
            href = l["href"]
            fix = (f"Use \"https://{href.lstrip('/')}\" (without https:// the browser treats it as a relative path)."
                   if re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/|$)", href, re.I)
                   else "Use a full https:// URL or a /path on imagekit.io.")
            res.add("links", "External links work", "fail", f"Link target \"{href}\" is not a valid URL.", l["line"], fix)
    if not to_check:
        res.add("links", "External links work", "pass", "No external links.")
    elif args.no_network:
        res.add("links", "External links work", "skip",
                "Network checks were turned off (--no-network). Check these by hand: " + ", ".join(sorted(to_check)) + ".")
    else:
        ctx = make_ssl_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(to_check, pool.map(lambda u: fetch_status(u, ctx), to_check)))
        if all(r["status"] is None and r["network"] for r in results.values()):
            why = next(iter(results.values()))["error"]
            res.add("links", "External links work", "skip",
                    f"No network access ({why}), so none of the links could be checked. Check these by hand: "
                    + ", ".join(sorted(to_check)) + ".")
        else:
            ok_count = 0
            for url, r in results.items():
                first = to_check[url][0]
                where = first["line"]
                label = "ImageKit page" if first["kind"] == "site" else "External link"
                if r["status"] is None:
                    res.add("links", "External links work", "warn",
                            f"{label} {url} could not be reached ({r['error']}).", where,
                            "Open it in a browser; if it is dead, remove or replace it.")
                elif r["status"] in (401, 403, 429, 999):
                    res.add("links", "External links work", "warn",
                            f"{label} {url} returned {r['status']} (the site blocks automated checks).", where,
                            "Open it in a browser to confirm it works.")
                elif r["status"] >= 400:
                    res.add("links", "External links work", "fail",
                            f"{label} {url} returned HTTP {r['status']}.", where,
                            "Remove or replace the link (find the page's new URL or a better source).")
                else:
                    ok_count += 1
                    if not same_page(url, r["final_url"]):
                        res.add("links", "External links work", "warn",
                                f"{label} {url} redirects to {r['final_url']}.", where,
                                f"Update the link to {r['final_url']} (check it is still the same content).")
            if ok_count == len(results):
                res.add("links", "External links work", "pass", f"All {ok_count} external/ImageKit link(s) return a working page.")
            elif ok_count:
                res.add("links", "External links work", "pass", f"{ok_count} of {len(results)} external/ImageKit link(s) return a working page.")

    # ---------------------------------------------------------------- trust
    author = (fm.get("author") or "").strip() if isinstance(fm.get("author"), str) else ""
    authors = load_authors(repo_root) if repo_root else None
    if not author:
        res.add("trust", "Valid author", "fail", "`author` is not set.", fm_lines.get("author"),
                "Set `author` to an ID from src/data/authors.ts.")
    elif authors is None:
        res.add("trust", "Valid author", "skip", f"`author` is \"{author}\", but src/data/authors.ts could not be read to validate it.")
    elif author in authors:
        res.add("trust", "Valid author", "pass", f"`author` \"{author}\" is a valid author ID.")
    else:
        close = difflib.get_close_matches(author, sorted(authors), n=1, cutoff=0.5)
        res.add("trust", "Valid author", "fail", f"`author` \"{author}\" is not in src/data/authors.ts.", fm_lines.get("author"),
                f"Did you mean \"{close[0]}\"?" if close else "Use an ID from src/data/authors.ts.")

    # ---------------------------------------------------------------- publishing
    missing = []
    for f in ("title", "description", "meta_description", "updatedDate", "featuredImage", "tags"):
        v = fm.get(f)
        empty = v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not [x for x in v if str(x).strip()])
        if empty:
            missing.append(f)
            res.add("publishing", "Front matter complete", "fail", f"`{f}` is empty or missing.", fm_lines.get(f),
                    f"Fill in `{f}`.")
    if not missing:
        res.add("publishing", "Front matter complete", "pass",
                "`title`, `description`, `meta_description`, `updatedDate`, `featuredImage` and `tags` are all filled in.")

    # ---------------------------------------------------------------- context for review
    imagekit_lines = [
        {"line": ln, "text": strip_markup(t).strip()}
        for ln, t in body if re.search(r"imagekit", strip_markup(t), re.I) and strip_markup(t).strip()
    ]
    for f in ("title", "description", "meta_title", "meta_description"):
        v = fm.get(f)
        if isinstance(v, str) and re.search(r"imagekit", v, re.I):
            imagekit_lines.insert(0, {"line": fm_lines.get(f), "text": f"{f}: {v}"})

    tables = []
    in_pipe = False
    for ln, t in body:
        if re.match(r"^\s*\{%\s*table\b", t):
            tables.append(ln)
        is_pipe = bool(re.match(r"^\s*\|.*\|\s*$", t))
        if is_pipe and not in_pipe:
            tables.append(ln)
        in_pipe = is_pipe
    tables += [ln for ln, t in body if re.search(r"<table\b", t, re.I)]

    return {
        "post": {
            "path": rel_path,
            "slug": slug,
            "live_url": live_url,
            "title": title,
            "meta_title": eff_meta_title,
            "meta_description": eff_meta_desc,
            "keyword": keyword,
            "keyword_source": keyword_source,
            "repo_root": repo_root,
            "base_ref": base_ref,
            "checked_on": today.isoformat(),
        },
        "results": res.items,
        "context": {
            "headings": headings,
            "imagekit_mentions": imagekit_lines,
            "image_count": len(images),
            "table_lines": sorted(tables),
            "internal_blog_links": [{"line": l["line"], "anchor": l["anchor"], "url": l["url"]} for l in blog_links],
        },
    }


ICONS = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}


def print_text(report):
    p = report["post"]
    print(f"Post:     {p['title']}")
    print(f"Path:     {p['path']}")
    print(f"URL:      {p['live_url']}")
    print(f"Keyword:  {p['keyword'] or '(not found in data/keywords.csv)'}"
          + (f"  [from {p['keyword_source']}]" if p["keyword_source"] else ""))
    print(f"Compared: {p['base_ref']}   Checked on: {p['checked_on']}")
    counts = {}
    for r in report["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("Summary:  " + ", ".join(f"{counts.get(k, 0)} {k}" for k in ("fail", "warn", "pass", "skip")))
    for key, name in SECTIONS.items():
        items = [r for r in report["results"] if r["section"] == key]
        if not items:
            continue
        print(f"\n## {name}")
        order = {"fail": 0, "warn": 1, "skip": 2, "pass": 3}
        for r in sorted(items, key=lambda r: (order[r["status"]], r["line"] or 0)):
            loc = f" (line {r['line']})" if r["line"] else ""
            print(f"[{ICONS[r['status']]}] {r['check']}{loc}: {r['message']}")
            if r["fix"] and r["status"] != "pass":
                print(f"       Fix: {r['fix']}")
    ctx = report["context"]
    print("\n## Context for the review checks")
    print("Headings:")
    for h in ctx["headings"]:
        print(f"  line {h['line']}: {'#' * h['level']} {h['text']}")
    print("Tables: " + (", ".join(f"line {n}" for n in ctx["table_lines"]) if ctx["table_lines"] else "none"))
    print("Lines that mention ImageKit:")
    for m in ctx["imagekit_mentions"]:
        print(f"  line {m['line']}: {m['text']}")


def main():
    ap = argparse.ArgumentParser(description="Automatic pre-publish checks for an ImageKit blog post.")
    ap.add_argument("post", help="Path to the .mdoc post, e.g. src/content/blog/dam-demo.mdoc")
    ap.add_argument("--keyword", help="Target keyword (overrides data/keywords.csv)")
    ap.add_argument("--base-ref", default="origin/dev", help="Git ref of the previous version (default: origin/dev)")
    ap.add_argument("--no-network", action="store_true", help="Skip the HTTP checks of external links")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = ap.parse_args()
    report = run(args)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report)


if __name__ == "__main__":
    main()
