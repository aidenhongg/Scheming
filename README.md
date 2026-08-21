<img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTb5pi6cAU1CJXad4OKhBgUBbnpx4BjJXfn_w&s" alt="Project Screenshot" width="200">

# scheming — mine your Claude Code history into a self-improving procedure library

`scheming` is a Claude Code plugin that mines **your own** session history into a
searchable library of hard-won procedures, and recalls the right one back into a
future session at the moment it applies. Over time it learns — from your real
reactions — which solution to lead with, and surfaces it right when it applies.

**Private by design.** Your library is built from your own `~/.claude/projects`
and never leaves your machine — nothing is bundled, and nothing is shared.

## This will consume 1M+ Sonnet tokens on setup so please be aware

Requires **Claude Code** and **Python 3.8+** available as `python` on your PATH.

Local (development / trying it out):
```
claude --plugin-dir /path/to/plugin
```

## Lifecycle

```
/scheming-setup    keep your history from expiring, create your library, and build it
/scheming-mine     re-run anytime to grow the library as your history accumulates
scheming-recall    surfaced automatically mid-session when it fits
   ↓ every suggestion is remembered
after each session  scheming reviews how you reacted and gets better
```

1. **`/scheming-setup`** — Claude Code normally deletes your session history after
   ~30 days, so most of it is gone within a month. Setup asks your OK to keep it
   for the long haul (it only helps going forward, so do it early), then builds
   your library from your history right away — pulling in your past Workflow runs
   and mining your most active sessions.
2. **`/scheming-mine`** — re-run this whenever you want more coverage. In one pass
   it takes in your past Workflow runs (free, instant) and reads your most active
   sessions, pulling out the reusable procedures — keeping only the ones backed by
   real evidence.
3. **`scheming-recall`** — before multi-step work, or when it hits a confusing error,
   Claude checks the library — but only when there's a strong match. It finds the
   procedures most relevant to what you're doing and leads with the one that's
   worked best, showing competing approaches together so the right one can be
   picked for the situation.
4. **It gets better on its own** — after a session, scheming quietly reviews how you
   reacted to what it suggested and updates each procedure's track record, in the
   background, using your existing Claude Code login (no API key). It never blocks
   your session, and if the review can't run it simply skips it.

## What's in the box

| Path | Role |
|---|---|
| `skills/scheming-recall/` | surfaces the right procedure mid-session |
| `skills/scheming-mine/`   | builds the library from your Workflow runs + sessions |
| `commands/scheming-setup.md` | the setup command (also kicks off the first build) |
| `hooks/hooks.json`   | runs the background review at session start |
| `lib/*.py`           | one small script per job |

## Your data

Everything scheming writes lives under **`SCHEMING_HOME`** (default `~/.scheming`):
your library and the usage history it learns from. Your session transcripts never
leave your machine. The only times a model runs are your own mining runs and the
background review — both under your own Claude Code login. Uninstalling the plugin
leaves `SCHEMING_HOME` in place, so your library survives.

**Privacy:** everything is stored locally and is **never** sent anywhere. The
history files don't auto-delete, so if you ever type a secret into a prompt, it
can sit in `SCHEMING_HOME` — keep that folder private and scrub it if that happens.

## Development

```
python tests/run_all.py     # every component selftest + the end-to-end pipeline
```
