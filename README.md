# insta-scrape

Turns the Instagram posts saved in a DM export into English Obsidian notes:

```
export JSON → ingest → download (gallery-dl, yt-dlp fallback) → transcribe (whisper + video frames)
            → queue → Claude Code writes the notes (/enrich-notes) → collect → render → notes/
```

There's no Anthropic API involved: the Claude step runs in a Claude Code session in this
repo, using the `enrich-notes` skill in `.claude/skills/`.

Notes keep the post's useful content at whatever length it needs. Posts with nothing
worth keeping (memes, vibes) are skipped. All state is in `data/manifest.sqlite`; see
the docstring in `pipeline.py` for details.

## One-time setup

Export your Instagram cookies once, from your own terminal (it needs access to Brave's
cookie store):

```
.venv/bin/gallery-dl --cookies-from-browser brave/.instagram.com --cookies-export data/cookies.txt --simulate "https://www.instagram.com/reel/DR1_bcPgVVf/"
```

Re-run it whenever downloads stop with "Instagram wants a login". `data/cookies.txt`
holds your logged-in session, so don't share it.

## Running

```
.venv/bin/python pipeline.py all --limit 20   # ingest, download, transcribe 20 posts; queue them for Claude
```

Then, in Claude Code in this folder: `/enrich-notes 20`. It writes the notes into `notes/`.

```
.venv/bin/python pipeline.py status -v        # counts, failures, skip reasons
.venv/bin/python pipeline.py retry            # give failed posts more attempts
.venv/bin/python pipeline.py render --force   # rewrite every note after changing the template
.venv/bin/python pipeline.py merge KEEP DUP   # fold duplicates into KEEP, after writing the
                                              # merged data/claude/KEEP.json
```

Every stage also runs on its own (`ingest`, `download`, `transcribe`, `queue`, `collect`,
`render`) and takes `--limit N` where it makes sense. Stages resume where they left off, so Ctrl-C is safe anywhere.
A newer export can be merged in with `pipeline.py ingest path/to/messages.json`.

`transcribe.py`, `reels/`, `transcribed_reels/` and `transcriptions/` are from the old
manual flow and are not used by the pipeline.



.venv/bin/python pipeline.py all