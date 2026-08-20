#!/usr/bin/env python3
"""scheming first-run setup (C5). Stdlib only.

Three things:
1. Transcript retention (CONSENT-GATED): Claude Code prunes session transcripts
   after `cleanupPeriodDays` (default 30) — that silently caps scheming's substrate,
   the user's own trace history, at ~1 month. Setup raises it to a large value
   (default 3650) by MERGING one key into the Claude Code settings file, never
   clobbering the user's other settings. Gated on --yes (interactive input() is
   fragile in headless/hook contexts). Malformed settings JSON aborts only the
   settings write; missing settings is created minimally.
2. Init SCHEMING_HOME + an empty library (via scheming_core; the mine skill populates it).
3. Report: old->new retention, home dir, library state, and the honest caveat
   that the retention change only helps GOING FORWARD.

CLI: python lib/setup.py [--yes] [--retention-days N] [--settings PATH]
     python lib/setup.py --selftest
Idempotent, exit 0 on success.
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scheming_core

DEFAULT_RETENTION = 3650          # ~10y; disk cost trivial (~1.6GB worst case)
CLAUDE_CODE_DEFAULT = 30          # what Claude Code assumes when the key is absent
KEY = "cleanupPeriodDays"


def _read_settings(path):
    """Return (dict, note). note in {'ok','missing','malformed'}. Never raises.
    'malformed' also covers a top-level non-object — we must not merge into it."""
    if not os.path.exists(path):
        return {}, "missing"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}, "malformed"
    return (data, "ok") if isinstance(data, dict) else ({}, "malformed")


def _old_display(old):
    if isinstance(old, (int, float)):
        return str(old)
    return "unset (Claude Code default %d)" % CLAUDE_CODE_DEFAULT


def run(settings_path, target=DEFAULT_RETENTION, apply=False, out=print):
    """Do setup. Prints a report via `out`. Returns a summary dict.
    `apply` is the --yes consent flag; without it, settings are never written."""
    # --- step 2: init home + empty library (always safe, no consent needed) ---
    home = scheming_core.ensure_home()
    lib = scheming_core.load_library()          # creates library.json on disk if missing
    n_entries = len(lib.get("entries", []))

    # --- step 1: retention (consent-gated merge) ---
    settings, note = _read_settings(settings_path)
    old = settings.get(KEY) if note != "malformed" else None
    already = isinstance(old, (int, float)) and old >= target
    wrote = False

    if note == "malformed":
        status = "aborted: settings file is not valid JSON (left untouched): %s" % settings_path
    elif already:
        status = "already set (%s >= %d) - no change" % (old, target)
    elif not apply:
        status = "WOULD change %s -> %d (not written; needs consent)" % (_old_display(old), target)
    else:
        settings[KEY] = target
        scheming_core.save_json_atomic(settings_path, settings)   # merge: only one key touched
        wrote = True
        status = "changed %s -> %d" % (_old_display(old), target)

    # --- step 3: report ---
    out("scheming setup")
    out("  retention: %s" % status)
    out("             settings file: %s" % settings_path)
    if note == "missing" and not wrote and apply is False:
        out("             (no settings file yet - one will be created on --yes)")
    if not apply and note != "malformed" and not already:
        out("             to apply: python lib/setup.py --yes"
            + ("" if target == DEFAULT_RETENTION else " --retention-days %d" % target)
            + ("" if settings_path == scheming_core.claude_settings_path() else ' --settings "%s"' % settings_path))
        out("             note: retention only helps GOING FORWARD - already-pruned")
        out("                   sessions are unrecoverable, so apply this early.")
    elif wrote:
        out("             note: this only extends retention GOING FORWARD; sessions")
        out("                   already pruned are gone. The sooner set, the more survives.")
    out("  home:      %s" % home)
    out("  library:   initialized, empty (%d entries) - mining runs next to populate it"
        % n_entries)
    out("  privacy:   telemetry logs under SCHEMING_HOME are append-only and never rotate;")
    out("             a secret typed into a recall query or a correction turn is stored")
    out("             there verbatim. Keep SCHEMING_HOME private; scrub it if that happens.")

    # library-evolution telemetry: confirms retention was actually extended (else the
    # substrate silently prunes at 30d). consent-before-write is itself the privacy record.
    scheming_core.emit("library_events", {
        "event": "setup", "consent": bool(apply),
        "cleanup_days_before": old if isinstance(old, (int, float)) else None,
        "cleanup_days_after": target if wrote else (old if already else None),
        "wrote_settings": wrote})

    return {"retention_status": status, "wrote_settings": wrote, "already": already,
            "note": note, "old": old, "target": target, "home": home, "entries": n_entries}


def _selftest():
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="scheming_setup_test_")
    iso = scheming_core.isolated_home(os.path.join(tmp, "home")); iso.__enter__()
    quiet = lambda *a, **k: None
    sp = os.path.join(tmp, "settings.json")
    try:
        # (a) without --yes: settings NOT written, but home+library ARE created, change reported
        r = run(sp, apply=False, out=quiet)
        assert not os.path.exists(sp), "dry run must not write settings"
        assert os.path.exists(scheming_core.library_path()), "library must be created"
        assert os.path.isdir(scheming_core.home()), "home must be created"
        assert "WOULD change" in r["retention_status"] and r["wrote_settings"] is False

        # (b) with --yes: key set to target AND a pre-existing unrelated key survives (merge)
        scheming_core.save_json_atomic(sp, {"theme": "dark", KEY: 30})
        r = run(sp, apply=True, out=quiet)
        got = scheming_core.load_json(sp)
        assert got[KEY] == DEFAULT_RETENTION, got
        assert got["theme"] == "dark", "merge must not clobber other settings"
        assert r["wrote_settings"] is True

        # (c) malformed settings JSON aborts the settings write without throwing, file untouched
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{ this is not json ")
        r = run(sp, apply=True, out=quiet)          # must not raise
        assert r["wrote_settings"] is False and "aborted" in r["retention_status"]
        with open(sp, encoding="utf-8") as f:
            assert f.read() == "{ this is not json ", "malformed file must be left as-is"

        # (d) idempotent: re-run with --yes when already >= target does NOT decrease the value
        scheming_core.save_json_atomic(sp, {KEY: DEFAULT_RETENTION + 100})
        r = run(sp, apply=True, out=quiet)
        assert scheming_core.load_json(sp)[KEY] == DEFAULT_RETENTION + 100, "must not decrease"
        assert r["already"] is True and r["wrote_settings"] is False
        print("scheming setup selftest ok")
    finally:
        iso.__exit__(None, None, None)     # restore env
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    try:  # UTF-8 stdout so a cp1252 console can't crash a print on non-ASCII paths
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="scheming first-run setup")
    ap.add_argument("--yes", action="store_true",
                    help="consent: apply the cleanupPeriodDays merge to the settings file")
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION,
                    help="target cleanupPeriodDays (default %d)" % DEFAULT_RETENTION)
    ap.add_argument("--settings", default=None,
                    help="settings file path (default: Claude Code ~/.claude/settings.json)")
    ap.add_argument("--selftest", action="store_true", help="run isolated self-test and exit")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    settings_path = args.settings or scheming_core.claude_settings_path()
    run(settings_path, target=args.retention_days, apply=args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
