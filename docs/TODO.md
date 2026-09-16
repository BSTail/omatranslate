# Translation Reliability Checklist

## Handoff Status

Latest (2026-09-16 11:19): first post-fix production replay is successful.
Seven consecutive gates (`in-85` through `in-91`) all produced drafts, finals,
translations, and exactly one safe clear request/send/ack cycle. Gate-to-first-
draft latency was 11.908s, 5.853s, then 2.125s, 2.444s, 1.962s, 2.126s, and
1.958s. This eliminates the prior progressive failure where attempts 3-4
produced no events. Overlay receipt remained 0.149-0.378ms after draft send;
there were no clear failures, stale-epoch deferrals, reconnects, warnings, or
service restarts. All seven clears were acknowledged before the next gate.

Do not interpret event `asr_ms` or the journal's completed-event `first_delta`
as gate latency: both currently start from the previous card/session timer and
include wire-silent idle before the gate. The objective numbers above are
`gate opened` monotonic -> `incoming first draft` monotonic. This is a telemetry
issue, not a production blocker. The first attempt was genuinely slower, but it
also sent 17.52s of audio versus 13.52s then 5.04-10.00s; every post-clear
runner succeeded. No functional change is justified from this run. A future
controlled ABBA can compare a fresh WebSocket immediately sending one fixed WAV
against the same fresh WebSocket primed with the current warmup, using identical
bytes/pacing, if first-play latency remains important.

Latest (2026-09-16 03:41): verified warmup did not fix the live failure. Four
post-warmup gates at 03:14 sent valid exact-400ms audio: first delta latencies
were 11.938s and 5.812s, then two later gates (6.48s and 4.88s sent WAVs) emitted
no ASR events. Visible finals began `Para que...` and `orgullosa de ti...`;
overlay delivery remained sub-millisecond and the service/backend stayed alive.
Each WAV contains acoustic energy after only 0.254-0.350s initial near-silence,
so the failure is downstream of the controller's gate/send boundary.

A controlled order-repeated A/B using those exact four WAVs isolated retained
NeMo runner state. Keeping the automatic-final runner produced clips 1-2 then
no output for clip 3 in both repeats (and none for clip 4 in the complete run).
Explicit clear after each completed final produced 8/8 outputs and reduced clip
2 first-delta latency from about 6.14s to 2.45s. Evidence and raw event streams:
`/tmp/opencode/post-final-reset/`.

Implemented and deployed a safe version: `.completed` requests a runner clear
only when its gate is already closed; the capture pump sends the clear only at
actual wire silence after bounded trailing audio. A reopen invalidates a stale
request, so accepted onset PCM is never erased; a later closed-gate completion
can request its own epoch. Clear is bounded to 2s and failure reconnects. No
commit or WebSocket/capture rotation at that checkpoint. Focused controller tests: 33; full suite:
46. One restart at 03:41:18; verified warmup proof at 03:41:23 and incoming ready
03:41:24, active with NRestarts=0. Validation later completed with seven
successful gate/final/clear cycles; see the newer entry above.

Latest (2026-09-16 02:59): user priority is preserving the beginning and
showing the first translation as early as possible; exact ASR wording is
secondary. Two natural replays on one healthy stream took 11.929s then 2.323s
from gate open to first delta, both with exact 400ms preroll and no process or
stream failure. The same earlier pattern was 11.926s followed by about 2.1s.
Exact sent WAVs contain real onset audio, so complete controller-side onset loss
is ruled out.

Bounded same-PCM tests did not reproduce the natural 12s delay: a fresh socket
after 10/30/60s idle produced first deltas in about 2.774s; production-like
startup silence produced 3.75-3.79s even after 55s wire idle; burst input took
0.884s; and the exact slow in-7 WAV took 2.782s when replayed regularly. Fresh
connection, idle, startup silence, and PCM content are therefore not sufficient
causes. Artifacts are under `/tmp/opencode/idle-latency-matrix/`,
`startup-gate-matrix/`, `startup-gate-long-idle/`, `fresh-stream-burst/`, and
`replay-exact-slow-in7/`.

The old startup warmup sent one unpaced tone blob and immediately cleared and
closed without reading a server event, so it never proved inference occurred.
The deployed warmup now waits for `session.updated`, sends 1.6s tone plus 1.6s
silence in paced 160ms chunks, commits, waits for a processed delta/completed
event, clears with acknowledgement, and uses bounded cleanup. Full suite: 38
tests. One restart at 02:59:06; streaming server ready 02:59:07, verified warmup
completed with `audio_processed=3.2` at 02:59:11, and incoming ready 02:59:12.
Service is active with `NRestarts=0`. This is a verified startup correction, not
yet a proven latency fix. Next: two natural live replays after this restart.

Latest diagnostic (2026-09-16): each multimedia gate-open interval now records
the exact PCM bytes successfully sent to streaming NeMo, enabled only by the
existing `debug_capture` setting. Files are valid mono PCM16/16 kHz WAVs under
`~/.local/state/omatranslate/debug/sent/`, named with generation, card, and gate
monotonic timestamp. They begin with the exact preroll+opening chunk sent on the
wire, append subsequent open-gate sends, and finalize on gate close, stream end,
replacement, or shutdown. Finalization logs path, generation/card/onset, bytes,
duration, and reason. The separate sent-artifact directory retains only the
newest `debug_keep` files; clear-logs removes it recursively. No ASR recovery,
threshold, framing, gate, or send behavior changed. Focused tests compare WAV
payload to `send_audio` calls and cover gate-close and cancellation finalization;
all 34 tests pass, plus the real-GTK suite (8 pass, 1 synthetic-only skip).
Repo and deployed `audio.py`/`controller.py` match. One requested service restart
completed at 02:03:03; controller PID 1760045, streaming NeMo PID 1760065,
offline NeMo PID 1760087, and overlay PID 1760114 were ready by 02:03:05 with
an established controller-to-8080 connection and systemd `NRestarts=0`. No audio
was replayed, so `debug/sent/` will be created by the next natural gate opening.

Read-only live evidence before this diagnostic: after the 01:33 exact-400ms
restart, first-delta latency from gate opening was 2.280s, then 1.989s and
2.154s for the productive attempts. Five later openings at 01:43:48, 01:44:02,
01:44:14, 01:44:27, and 01:45:24 produced no delta/final before a clean stream
end/reconnect at 01:50:34. Controller and both NeMo PIDs stayed alive; systemd
reported zero restarts. The top screenshot card was finalized `in-13`, not an
active draft: `Amo papi me siento muy...` / `Master Daddy I feel very...`.

Latest (2026-09-16 01:33): withdrew ONLY whole-chunk preroll rounding after
user reported worse live onset loss. Repo/deployed controller now retain exact
400ms continuous bytearray preroll, including quiet/soft PCM; watchdog safety,
wire-silent idle and refinement safeguards unchanged. Removed the whole-chunk
variable-size test and adjusted byte-count expectations; all 32 tests pass.
Deployed source matched expected before patching; one restart at 01:33:09,
both models warmed and incoming capture active by 01:33:12. No commits.

Live failure after 01:25 restart: all six openings logged 15360B (480ms)
preroll. First drafts at 01:27:36/01:27:52/01:28:30 followed gate openings by
12.045/1.313/11.618s; finals began "Para que" / "haciendo por cada cosa" /
"Para que" (events.jsonl lines 89-91). Overlay delivery took 0.29-0.38ms.
Openings at 01:28:01 and 01:28:12 had no intervening draft; third draft was
28.736s after that first opening. Opening 01:29:21 had no delta/final before
stream ended 01:30:06. All occurred on one stream, not watchdog teardown.
Withdrawal is not proof that 400ms resolves onset loss; live retest pending.

Requested single endpointing-on/off comparison BLOCKED before audio replay:
local upstream HTTP session parser only exposes endpointing_ms (threshold),
not an enable flag. <=0 restores configured threshold, not off; session.updated
echoes supplied fields and does not prove support. No invented field, huge
threshold workaround, new server or global configuration change attempted.
Source evidence retained in /tmp/opencode/endpointing-comparison-blocker.md.
Next: establish installed binary provenance and a verified per-session EOU
disable path before the same-PCM regular-framing, commit/drain comparison;
otherwise seek approval for a separate controlled endpointing test. No further
alignment tuning or gate-triggered hard commits.

Changes are deployed to `~/.local/share/omatranslate/src/olt/` and the user
verified rendering and scrolling. Automated verification: 34 tests passed;
real-GTK suite passed 8 tests with 1 synthetic-only skip and no GTK criticals.
This is a partial reliability fix, not a resolution of missing opening words.

Deployed so far (watchdog committed as 91b7bff; preroll uncommitted):
1. Destructive no-delta watchdog replaced with a warning-only delay monitor
   (15 s, once per gate onset). Recovery on missing deltas is disabled until
   continuous capture + complete replay exist. Connection/EOF recovery and
   wire-silent idle are unchanged.
2. Continuous bounded gate preroll: the closed-gate buffer now retains quiet and
   soft PCM instead of clearing on each quiet chunk, so word onsets reach ASR.
   Consecutive-loud qualification, no duplicate trigger, and wire silence remain.

Live replays after fix 1 stayed on one stream (no teardown). Replays after fix 2
delivered the full 400 ms preroll, but the transcript still misrecognizes or
drops the opening words ("Ama papi" instead of "Te amo papi"). Threshold tuning
is now likely guessing.

Controlled ABBA replay reproduced the omission twice with gating and retained
the earlier passage twice without gating. The detected activity interval was
present unchanged in gated sent PCM; silence history, framing, and timing remain
confounded. Ungated recognition still starts "Ama papi", not the intended words.
Next: replay the exact gated sent WAV in regular 160 ms frames on its own sample
timeline, preferably on an independently owned test server; retain natural finals
then commit/drain. Do not change working overlay geometry or refinement safeguards.

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

- [x] Four live replays at 00:33-00:34 stayed on one stream without watchdog teardown. Gate-to-first-delta delays were 5.66s, 5.82s, 2.32s, and 5.64s; opening transcript words still missing. No claim that watchdog correction resolves onset loss.
- [x] Preserve continuous bounded gate preroll, including quiet/soft chunks, instead of clearing it on each quiet chunk. Consecutive-loud qualification and wire-silent idle preserved; 32 tests pass.
- [x] Live-verify continuous preroll correction (00:39-00:40): full 400 ms preroll delivered, but transcripts still misrecognize or drop the opening ("Ama papi" vs "Te amo papi"). Preroll correction alone does not resolve the missing beginning.
- [x] Controlled ABBA experiment: identical source interval [50.24, 80.00) seconds from in-20260916-003824.wav. Gated finals start "Para que" (first delta 12.24/12.23s after detected activity); ungated start "Ama papi" (2.78/2.78s). Exact sent WAVs/events/config in `/tmp/opencode/gate-abba-003824/`; script `/tmp/opencode/gate_experiment.py`. One clip, two trials per condition; shared compute and unverified server binary provenance limit conclusions. Natural finals arrived before EOF; no commit means complete draining is not proven.
- [ ] Replay exact gated sent WAV with regular framing/sample-time pacing to distinguish concatenated PCM history from original transport presentation. Use isolated server where feasible; handle ping/pong and commit acknowledgement.

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
