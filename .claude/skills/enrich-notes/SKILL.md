---
name: enrich-notes
description: Run the Claude step of the Instagram-to-Obsidian pipeline - read each queued post's packet and images, write its note JSON, then collect and render the notes. Use when asked to enrich, process, or write notes for downloaded posts. Takes an optional number of posts (e.g. "/enrich-notes 20").
---

# Enrich notes

The pipeline has no Anthropic API access. This session does the Claude step itself:
Python prepares a packet per post, Claude writes `data/claude/<id>.json`, and Python
checks it and renders the note.

1. Queue: `.venv/bin/python pipeline.py queue --limit N` (N from the arguments; omit
   `--limit` for everything). It prints the ids waiting for Claude. If none, say so and stop.
2. Split the ids into groups of about 10. Start one `general-purpose` subagent per group,
   up to 5 at a time, in a single message so they run in parallel. Images are heavy,
   so don't do the posts in the main session unless there are 3 or fewer. Give each
   subagent this prompt, with its ids filled in:

   > Write Obsidian note JSON for these saved Instagram posts:
   > <ids, one per line>
   > First read /Users/siddharthaasal/00_Code/insta-scrape/.claude/skills/enrich-notes/note-guide.md
   > and follow it exactly. For each id, read data/packets/<id>.md (relative to
   > /Users/siddharthaasal/00_Code/insta-scrape), view every image it lists, and write the
   > result file it names, using the Write tool directly (other agents run in parallel,
   > so no shared helper scripts). Don't run pipeline.py or touch any other files.
   > Reply with one line per id: the title, or "skipped: <reason>".

3. When every group is done: `.venv/bin/python pipeline.py collect`. Any file it reports
   as invalid: fix the JSON (read the packet again if needed) and run collect again.
4. `.venv/bin/python pipeline.py render`, then `.venv/bin/python pipeline.py status`.
5. Report: how many notes were written, how many posts were skipped (with reasons), and
   any failures.
