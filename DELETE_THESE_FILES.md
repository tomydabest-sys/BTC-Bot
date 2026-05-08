# Files to delete from your repo

These are the files that should be removed as part of this rewrite.
Run these commands at your repo root after merging the new files:

```bash
# Typo'd duplicate of dual_direction_arb.py — was never registered, never ran
rm src/polybot/strategies/dual_detection_arb.py

# (Optional) old run logs and stale dbs from previous session
rm -f data/bot.db data/features.db logs/decisions.jsonl logs/bot.log
```

That's it. Everything else is replaced in-place by the new versions in the
zip. The deleted file's behaviour is unchanged because nothing referenced it.
