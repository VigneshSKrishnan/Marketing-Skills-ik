---
name: blog-final-check
description: Final pre-publish check of one ImageKit blog post (src/content/blog/<slug>.mdoc) against the blog refresh checklist. Runs automatic SEO/link/front-matter checks, reviews the writing and ImageKit positioning, asks the writer the manual yes/no questions, and gives one report with a publish verdict. Read-only: it reports in the chat and never edits, commits or pushes anything. Use when a writer asks to final-check, pre-publish check, or review a refreshed blog post.
argument-hint: "[path to post, e.g. src/content/blog/dam-demo.mdoc]"
allowed-tools: Read, Grep, Glob, Bash(python3 *), Bash(git rev-parse *), Bash(git show *), Bash(git log *)
disallowed-tools: Edit, Write, NotebookEdit, Bash(git push *), Bash(git commit *), Bash(git fetch *), Bash(git pull *), Bash(git add *), Bash(git checkout *), Bash(git reset *), Bash(gh *)
---

# Blog final check

You check **one** ImageKit blog post against the refresh checklist and give the writer a single report.

## Rules (always)

- **Read-only.** Never edit the post or any other file. Never run commands that change the repo or talk to GitHub: no `git fetch`, `pull`, `add`, `commit`, `push`, `checkout` or `reset`, no `gh`, no pull requests or comments. Don't save the report to a file or post it anywhere.
- **Respond to the writer in the chat.** After reviewing the results, give the report as your reply. That is the only output. Don't offer to apply the fixes; the writer makes the changes.
- **Never present a guess as a fact.** If a check can't run (no git history, no network, post not in `keywords.csv`), say exactly that and what the writer should check by hand.
- **Be concrete.** For every problem, quote the line (with its line number) and give the replacement text.

## Step 1: Get the post

The writer runs this from a local clone of `imagekit-internal/imagekit-blog`.

- The post path is: `$ARGUMENTS`
- If that is empty, ask: "Which post should I check? Please give the path, e.g. `src/content/blog/dam-demo.mdoc`." Wait for the answer.
- If the file doesn't exist, say so and ask again. Do not guess a path.

## Step 2: Run the automatic checks

1. Run the checker from the blog repo root. It only reads files (including the `dev` version through `git show origin/dev:<path>`) and requests external links to see if they work:
   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/check_post.py" <post-path>
   ```
   - The script compares against the writer's local `origin/dev` and prints the date of its latest commit. If `origin/dev` is missing or that date is more than a few days old, say so in the report and tell the writer they can run `git fetch origin dev` themselves and check again. Don't run it for them.
   - If the output says the post is **not in data/keywords.csv**, ask the writer: "This post isn't in our keyword list. What is the target keyword for it?" Then re-run with `--keyword "<their keyword>"`.
   - The script checks external links over the network. If it reports no network access, keep that SKIP in the report and list the links for the writer to check by hand.
2. Treat the script's `FAIL` / `WARN` / `PASS` / `SKIP` lines as the [Auto] results. Don't change their status. You may add a short note if a result is clearly a false alarm (for example a historical year like "WebP was released in 2010"), but keep it in the list.

## Step 3: Review checks

Read the whole post (Read tool) and `${CLAUDE_SKILL_DIR}/data/positioning.md`. For each check below, give a short judgement. When something needs to change, quote the line with its line number and give replacement wording. Keep each finding to two or three lines.

**Content**
- **Answer up front:** Is the main question (the one the target keyword implies) answered in the first few paragraphs? If not, suggest an opening sentence that answers it.
- **No choppy writing:** Flag abrupt jumps between ideas, stacks of one-line fragments, or sentences that don't connect. Quote them and suggest a smoother version.
- **No assumptions:** List **every** claim, statistic or number that has no source and isn't clearly framed as opinion. For each: quote it and say "add a source" or suggest how to reword it as opinion.
- **Dated facts:** Flag prices, plan limits, tool names, features, product names, rankings or reports that may be out of date, for the writer to verify. Say "verify", not "wrong", unless you are certain.
- **Tables for comparisons:** If the post compares two or more things in prose or lists (tools, plans, options), suggest a table and give its column headings.
- **No thin or repeated sections:** Flag sections that are only a sentence or two, or that repeat another section.

**On-page SEO**
- **Keyword variants in headings:** Do the H2s and H3s use variants of the target keyword? The script prints the headings under "Context for the review checks". Suggest one or two rewritten headings.

**ImageKit positioning**
- **Positioning:** Check every line that says what ImageKit is or does (the script lists lines that mention ImageKit, but read the whole post) against `data/positioning.md`. Quote any line that describes ImageKit differently, for example only as an image CDN, only as image optimization, or as just a DAM, and suggest wording based on the positioning statement. Also flag claims about ImageKit's plans, limits or numbers that don't match the positioning (for example customer counts) for the writer to verify.
- **Call to action:** Is there a clear call to action to try or learn about ImageKit (e.g. a sign-up or demo link)? If not, suggest one in line with the positioning.

**Readability and trust**
- **Short paragraphs and clear subheadings:** Flag paragraphs longer than about 4 to 5 sentences and long stretches without a subheading.

## Step 4: Manual checks

After the automatic and review checks, say exactly:

> Now a few questions I can't check myself. Please answer each one with yes or no.

Then ask these **one at a time**, waiting for each answer before asking the next. Put the target keyword in where shown.

1. Did you check the top 10 Google results for "<keyword>" and make sure the post matches their format and covers what they cover? Answer yes or no.
2. Did you keep the sections that bring in Search Console impressions? Answer yes or no.
3. Do all the screenshots show the current ImageKit interface? Answer yes or no.
4. Is the featured image up to date? Answer yes or no.
5. Do the tables in the post display correctly on mobile? Answer yes or no.
   (The script prints `Tables:` under "Context for the review checks". If it says `none`, don't ask; record "n/a, no tables" and count it as a yes.)
6. Did you preview the post before publishing? Answer yes or no.
7. Will you log the publish date in the refresh sheet? Answer yes or no.

If an answer isn't clearly yes or no, ask the same question again. Record each answer.

## Step 5: The report

Show **one** report in this order, using these headings:

```
# Blog final check: <post title>

**Path:** <post path>
**Target keyword:** <keyword> (<from data/keywords.csv | given by writer>)
**Live URL:** https://imagekit.io/blog/<slug>/

## ❌ Must fix
<every [Auto] FAIL: line number, what's wrong, the exact change>

## ⚠️ Warnings and review notes
<every [Auto] WARN and SKIP (say why it couldn't run and what to check by hand)>
<every [Review] finding: quoted line with its line number, then the suggestion>

## ✅ Passed
<one line per passed [Auto] check, and per [Review] check with no findings>

## 📝 Manual checks
<each question, then the writer's answer: yes / no>

## Verdict
<"Ready to publish" or "Not ready">
<if not ready: a list of everything still open (each ❌ item and each manual check not answered yes)>

⏰ Reminder: re-check this post's position for "<keyword>" 4–6 weeks after it goes live.
```

- Group items within each section by checklist area (Content, On-page SEO, Links, ImageKit positioning, Readability and trust, Publishing).
- **Verdict:** "Ready to publish" only if there are no ❌ items **and** every manual answer is yes. Otherwise "Not ready" plus the list of what's open. Warnings and review notes don't block the verdict, but mention any you think matter most.
- Always end with the reminder line. It is a reminder, not a question.
