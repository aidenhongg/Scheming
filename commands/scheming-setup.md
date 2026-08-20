---
description: First-run scheming setup — extend transcript retention (with consent), initialize the local library, and build it from the user's history.
---

Run scheming's one-time setup, then build the library. Three parts: a
consent-gated settings change, a safe local init, and an immediate first mine.

**1. Explain the transcript-retention issue plainly.** Tell the user, in your
own words:

- scheming mines *their own* Claude Code session transcripts into a library of proven
  procedures. Those transcripts are scheming's raw material.
- Claude Code prunes transcripts after `cleanupPeriodDays` — **default 30** — so
  out of the box most of their history is capped at ~1 month and older sessions
  are already gone for good.
- The fix is to raise `cleanupPeriodDays` (default target **3650**, ~10 years) in
  their Claude Code settings. This only extends retention **going forward** —
  already-pruned sessions can't come back, so it's worth doing early.
- Disk cost is trivial (~1.6 GB worst case). It changes exactly one settings key
  and merges — it will not touch their other settings.

**2. Ask for consent** to raise `cleanupPeriodDays`. Wait for a clear yes/no.

**3. Run setup.**

- On **yes**:
  ```
  python "${CLAUDE_PLUGIN_ROOT}/lib/setup.py" --yes
  ```
- On **no** (init only — no settings change):
  ```
  python "${CLAUDE_PLUGIN_ROOT}/lib/setup.py"
  ```
  Then tell them they can apply the retention change later by re-running
  `/scheming-setup` and saying yes, or running the command above with `--yes`.

Relay the script's report: the old→new retention value (or what it *would*
change), the settings file path, the `SCHEMING_HOME` directory, and that the library
starts empty. Also relay the **privacy note** it prints: scheming's telemetry logs
under `SCHEMING_HOME` are local-only, append-only, and never rotate, so a secret typed
into a query or a correction turn is retained there verbatim — keep `SCHEMING_HOME`
private and scrub it if that happens.

**4. Build the library now.** Don't wait to be asked — immediately run the
**`scheming-mine`** skill. In one pass it pulls in the user's past Workflow runs
(free, instant) and mines their densest raw sessions into procedures. Tell them
they can re-run `/scheming-mine` anytime to grow the library as their history
accumulates. Then it's ready — scheming will surface procedures on its own when
they fit.
