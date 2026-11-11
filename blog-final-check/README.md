# blog-final-check

A Claude Code skill that checks **one** ImageKit blog post against the blog refresh checklist before it is published, and gives the writer a report with a verdict ("Ready to publish" or "Not ready"). It only reports. It never edits the post.

It runs three kinds of checks:

| Type | Who does it | Examples |
|---|---|---|
| **Auto** | `scripts/check_post.py` (pass / fail / warn, with line numbers) | keyword placement, title and meta lengths, images and alt text, image file names, `updatedDate`, new internal links compared with `dev`, anchor text, broken internal and external links, links to killed posts, author ID, front matter |
| **Review** | Claude reads the post | answers the question up front, choppy writing, unsupported claims, dated facts, comparisons as tables, thin sections, keyword variants in headings, ImageKit positioning, call to action, paragraph length |
| **Manual** | The writer answers yes / no | top 10 Google results checked, Search Console sections kept, screenshots and featured image current, tables on mobile, previewed, publish date logged |

## Files

```
blog-final-check/
  SKILL.md                  # instructions Claude follows when the skill runs
  scripts/check_post.py     # the automatic checks (Python 3, standard library only)
  data/keywords.csv         # url,keyword for each DAM post (target keyword + anchor text for links to that post)
  data/killed-posts.txt     # posts being removed; nothing should link to them
  data/positioning.md       # ImageKit positioning statement (short and long)
  .claude-plugin/plugin.json
```

The repository root also has `.claude-plugin/marketplace.json`, which makes this repo a Claude Code plugin marketplace.

## Requirements

- [Claude Code](https://code.claude.com/docs)
- Python 3.9 or later (standard library only, nothing to `pip install`)
- git, and a local clone of `imagekit-internal/imagekit-blog`

## Install

These steps follow the current Claude Code docs: [Skills](https://code.claude.com/docs/en/skills.md), [Create a marketplace](https://code.claude.com/docs/en/plugins/create-marketplace.md), [Host and maintain a marketplace](https://code.claude.com/docs/en/plugins/host-marketplace.md) and [Install and manage plugins](https://code.claude.com/docs/en/plugins/install.md). Choose **one** option.

### Option A: plugin marketplace (recommended, easy to update)

1. This repo is private, so Claude Code uses your own git credentials to fetch it. If `git clone` of this repo already works for you, skip this step. Otherwise set up GitHub credentials once in your terminal:

   ```bash
   gh auth login
   ```

   ```bash
   gh auth setup-git
   ```

2. In Claude Code, add the marketplace:

   ```
   /plugin marketplace add VigneshSKrishnan/Marketing-Skills-ik
   ```

3. Install the plugin:

   ```
   /plugin install blog-final-check@imagekit-marketing-skills
   ```

   Choose the **user** scope so it is available in every project, including your blog clone. From a terminal you can instead run:

   ```bash
   claude plugin marketplace add VigneshSKrishnan/Marketing-Skills-ik
   ```

   ```bash
   claude plugin install blog-final-check@imagekit-marketing-skills --scope user
   ```

4. Run `/reload-plugins` (or restart Claude Code).

Plugin skills are namespaced, so the command is **`/blog-final-check:blog-final-check`**.

**Updating.** Auto-update is off by default for team marketplaces. To get the latest version:

```bash
claude plugin marketplace update imagekit-marketing-skills
```

```bash
claude plugin update blog-final-check@imagekit-marketing-skills
```

Or turn on auto-update once: `/plugin` → **Marketplaces** → `imagekit-marketing-skills` → **Enable auto-update**. The plugin has no `version` field, so every new commit to this repo counts as an update.

### Option B: copy into your personal skills folder

1. Clone this repo:

   ```bash
   git clone https://github.com/VigneshSKrishnan/Marketing-Skills-ik.git
   ```

2. Copy the skill folder into `~/.claude/skills/`:

   ```bash
   mkdir -p ~/.claude/skills && cp -R Marketing-Skills-ik/blog-final-check ~/.claude/skills/
   ```

3. Restart Claude Code. The command is **`/blog-final-check`**.

To update, `git pull` in your clone and copy the folder again.

## Run it

1. Open a terminal in your local clone of the blog repo and start Claude Code there:

   ```bash
   cd path/to/imagekit-blog
   ```

   ```bash
   claude
   ```

2. Run the skill with the path to the post (Option A command shown; with Option B use `/blog-final-check`):

   ```
   /blog-final-check:blog-final-check src/content/blog/dam-demo.mdoc
   ```

   If you leave out the path, Claude asks for it. You can also just ask: "Final-check src/content/blog/dam-demo.mdoc before I publish it."

What happens:

1. Claude runs `git fetch origin dev` so the previous version of the post is current. The internal-link check compares your file with `git show origin/dev:<path>` (`dev` is the blog repo's default branch). If `origin/dev` or the post's previous version isn't available, that check is skipped and the report says so.
2. Claude runs `scripts/check_post.py`. It looks up the target keyword for the post's URL in `data/keywords.csv`. If the post isn't there, Claude asks you for the keyword.
3. External links are requested over the network (redirects followed, 15-second timeout). If there is no network, the report lists the links for you to check by hand.
4. Claude reads the post and does the review checks.
5. Claude asks the manual questions one at a time. Answer each with yes or no.
6. You get one report: ❌ Must fix, ⚠️ Warnings and review notes, ✅ Passed, 📝 Manual checks, Verdict, and a reminder to re-check the ranking 4–6 weeks after the post goes live.

### Run only the automatic checks

The script works on its own, without Claude:

```bash
python3 path/to/blog-final-check/scripts/check_post.py src/content/blog/dam-demo.mdoc
```

Options:

| Option | What it does |
|---|---|
| `--keyword "dam demo"` | Use this target keyword instead of the one in `data/keywords.csv` |
| `--base-ref origin/dev` | Git ref to compare internal links against (default `origin/dev`) |
| `--no-network` | Skip the HTTP checks of external links (they are listed as manual) |
| `--json` | Print JSON instead of text |

## How the automatic checks decide

- **Keyword matching** is case-insensitive and ignores punctuation (`DAM vs. MAM` matches `dam vs mam`). A plural, other word forms or `DAM` ↔ `digital asset management` count as a variant and give a warning, not a failure.
- **Anchor text** for links to posts in `keywords.csv` passes when it is the keyword or a close variation: plural/singular and other word forms, `DAM` ↔ `digital asset management`, and extra words around it (`DAM workflows` passes for `digital asset management workflows`). Links to posts that aren't in the file are listed as not checked.
- **Internal blog links** are links to `imagekit.io/blog/...` (absolute or `/blog/...`). The "3 more internal links" check counts each existing, non-killed post once, in the current file and in the `dev` version. Links to other ImageKit pages (`/guides/...`, `/features/...`, docs) are checked like external links.
- **Old years**: a year before the current one in the title, meta fields or a heading fails; in body text it is a warning, since it may be a historical date.
- **`updatedDate`** fails if it's missing or not later than `pubDate`, and warns if it is more than 30 days old (probably left over from an earlier update).
- **Lengths** (title about 60 characters, meta description about 155) and the **slug** (keyword in it, 6 words or fewer) only warn. Change a URL only if the post is being redirected.
- **Images**: Markdown images, `<img>` tags and `gallery` images all count toward the minimum of 3. For Markdown images the alt text is also the caption shown below the image. `<img>` tags get a warning because no caption is shown. File names fail when generic (`image1.png`, `Screenshot.png`, `Untitled.jpg`, `IMG_1234.jpg`) or random strings; ImageKit's upload suffix (e.g. `_mUBcYig7G`) is ignored.
- **External links**: status 400 or higher fails. 401, 403, 429 and 999 (sites that block automated checks), timeouts and redirects to a different page are warnings to check in a browser.

## Updating the data files

### `data/keywords.csv`

Source: the **DAM - Organic Refresh Project** Google Sheet (https://docs.google.com/spreadsheets/d/1sbRnikQCCVbCyG2jDQvS5-O4dRh6LiPN4euw_0pQprM/edit), columns `url` and `keyword`. When keywords change:

1. In the sheet: **File → Download → Comma-separated values (.csv)**. This downloads the current tab.
2. Turn it into `keywords.csv` (keeps only the `url` and `keyword` columns, converts full URLs to `/blog/<slug>/`, and sorts):

   ```bash
   python3 - "$HOME/Downloads/<downloaded-file>.csv" blog-final-check/data/keywords.csv <<'EOF'
   import csv, sys, urllib.parse
   src, dst = sys.argv[1], sys.argv[2]
   rows = {}
   with open(src, newline="", encoding="utf-8") as fh:
       for r in csv.DictReader(fh):
           r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
           url, kw = r.get("url", ""), r.get("keyword", "")
           if not url or not kw:
               continue
           path = urllib.parse.urlparse(url).path or url
           path = "/" + path.strip("/") + "/"
           rows[path] = kw.lower()
   with open(dst, "w", newline="", encoding="utf-8") as fh:
       w = csv.writer(fh, lineterminator="\n")
       w.writerow(["url", "keyword"])
       for path in sorted(rows):
           w.writerow([path, rows[path]])
   print(f"wrote {len(rows)} rows to {dst}")
   EOF
   ```

   Replace `<downloaded-file>.csv` with the file you downloaded. Run it from the root of your clone of this repo.
3. Check the diff (`git diff blog-final-check/data/keywords.csv`), then commit and push. Everyone gets the change on their next plugin update (or next copy, for Option B).

Format: a header line `url,keyword`, then one post per line. The URL can be `/blog/<slug>/` or the full `https://imagekit.io/blog/<slug>/`. Keywords are matched case-insensitively.

### `data/killed-posts.txt`

One post path per line (`/blog/<slug>/`). Lines starting with `#` are ignored. Any link to these posts fails the check.

### `data/positioning.md`

The ImageKit positioning statement Claude uses to review how the post describes ImageKit. Replace the text when the positioning changes.
