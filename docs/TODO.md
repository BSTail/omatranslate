# Translation Reliability Checklist

## Handoff Status

Changes are deployed to `~/.local/share/omatranslate/src/olt/` and the user
verified rendering and scrolling. Automated verification: 29 tests passed;
real-GTK suite passed 8 tests with 1 synthetic-only skip and no GTK criticals.
This is a partial reliability fix, not a resolution of missing opening words.
The destructive watchdog timeout has been replaced in the repository with a
warning-only delay monitor (15 seconds, once per gate onset). Deployed and service
startup verified at 00:30 on 2026-09-16; speech replay is not yet live-verified.
Connection/EOF recovery is unchanged. Automatic
no-delta recovery is disabled until continuous capture and complete replay exist.
Next: live verification and audio/ASR onset comparison.
Do not change the working overlay geometry or restore unsafe partial refinement.

## Current Fixes

- [x] Drain overlay commands using nonblocking byte framing; handle batched commands, split UTF-8, and EOF.
- [x] Keep newest translation visible with history off; restore history in creation order; tolerate late updates without reparenting labels.
- [x] Apply configured history mode at overlay startup.
- [x] Preserve the approved monotonic panel height; reset when all cards are removed.
- [x] Reject incomplete refinement snapshots and preserve the original bilingual pair on translation failure or stale generation.
- [x] Stop guessing incoming refinement boundaries. Incoming refinement is currently skipped because this realtime protocol provides no proven complete utterance bounds. Streaming ASR and translation remain active; outgoing behavior is unchanged.
- [x] Remove duplicated reopening chunks and disconnected stale gate preroll, preserving bounded trailing silence and wire-silent idle.
- [x] Retain watchdog stream-health state across finals; close streams when their audio pump exits so incoming reconnects.
- [x] Add per-card first-delta/send/overlay-receipt timing and gate-onset monotonic timing to distinguish ASR delay from delivery delay.
- [x] Add deterministic controller and overlay regression tests.

## Verification and Follow-ups

- [x] User verified proper panel size, no visual clipping, and working scrolling after deployment (2026-09-16). Successful replay begins with "Te amo"; earlier replay starts at "Para que". Missing transcription content remains separate from layout.
- [x] Remove destructive no-delta timeout: warn without closing the stream, preserving delayed speech. Regression coverage checks delayed post-final text, cold streams, warning deduplication, and delta disarming. All 29 tests pass. Deployed; startup verified, speech replay pending.
- [ ] Implement automatic stall recovery only with continuous capture and complete replay, including duplicate-output prevention. Live retries previously triggered teardown after ~1.1 seconds, discarding unreplayed onset audio; successful runs needed ~2.28s and ~11.91s. No arbitrary timeout proves a wedge. This does not explain the earlier missing beginning without a restart.
- [x] Live timing isolated initial delay before ASR delta receipt: gate opened 00:04:38, delta arrived ~11.91s later; overlay received it ~0.4ms after controller send. User independently observed ~12s. This measures gate onset, not exact first speech sample.

- [ ] Verify live Spanish onset and final output on short and over-20-second speech, with history on/off and after an idle gap. Automated tests do not establish the cause of the user's observed 10-20-second initial delay.
- [ ] Restore safe incoming offline refinement using proven utterance boundaries. Confirm the installed NeMo binary's protocol against the local source; do not interpret audio_processed as a precise utterance endpoint without verification.
- [ ] Extend timing to first painted frame if receipt is prompt but text still appears late. Current overlay timing measures command receipt, not display completion.
- [ ] Review outgoing clear/auto-speak races and natural endpoint handling while PTT remains held.
- [ ] Clean up abandoned incoming drafts after empty finals and interrupted streams.
- [ ] Review panel settings request cancellation and HTTP response-body reads on the asyncio loop.

## Test Commands

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
OLT_TEST_REAL_GTK=1 LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so G_DEBUG=fatal-criticals PYTHONPATH=src python3 -m unittest discover -s tests -p test_overlay.py
git diff --check
```

The real-GTK suite requires a graphical session and exercises widget allocation/history, not the complete audio pipeline. Retest the deployed service separately.
