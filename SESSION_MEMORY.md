# Session Memory — omatranslate (plugin name: OmaTranslate)

Paste this into a fresh session to restore context.

## Project
Offline bilingual (en↔es) live speech-translation plugin for Omarchy Linux
(Timekettle-style). Coexists with the existing Voxtype dictation plugin
(untouched). Priorities: accuracy > speed > lightweight. Everything local.

## Stack
- ASR: NeMo-Speech.cpp (Vulkan, Intel Arc), model `nemotron-3.5-asr-streaming-0.6b`.
- NMT: LibreTranslate on port 5001 (`en,es`); existing dictation plugin uses 5000.
- TTS: Piper (es_ES-davefx-medium, en_US-lessac-medium).
- Controller: Python asyncio; overlay is GTK4 layer-shell; control server on 127.0.0.1:8670.

## Key workarounds (do NOT regress)
- NeMo: `LD_PRELOAD=/usr/lib/libstdc++.so.6` for Vulkan + `--no-warmup`
  (avoids intermittent `GGML_ASSERT(ne3 == ne13)` SIGABRT).
- NeMo serve MUST run with `--asr.endpointing.enable=true`, else the incoming
  monitor stream never emits `.completed` finals (no translation cards).
- Realtime WebSocket endpoint is `/v1/realtime` (NOT
  `/v1/audio/transcriptions/realtime`, which 404s). The WS handshake must use
  `reader.readuntil(b"\r\n\r\n")` — reading with `read(4096)` over-reads the
  first `session.created` frame and desyncs the frame parser ("malformed ASR
  frame" warnings, no finals).
- Overlay: gtk4-layer-shell needs `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so`.
- Overlay cards must be UPDATED IN PLACE (mutate label text/CSS classes), never
  remove+re-add inside the ScrolledWindow viewport (causes Gtk-CRITICAL
  `gtk_widget_is_ancestor` assertions / SIGABRT).
- Incoming `pump()` must swallow ConnectionResetError/OSError on TTS pause.
- Audio capture MUST use `parec` with `--latency-msec 10`. The default fragsize
  (64000 B = 2 s) adds ~2 s buffered latency. Do NOT switch to `pw-cat`:
  on this PipeWire setup `pw-cat --target @DEFAULT_MONITOR@` / `<monitor-name>`
  silently connects to the MIC source (Source 60), not the monitor (Source 58);
  only the numeric serial works, which is not stable. `parec` resolves
  @DEFAULT_MONITOR@ / @DEFAULT_SOURCE@ correctly.
- Debug WAVs: a capture file only contains audio for the session it covers; a
  translation finalizes the session, closes that WAV, and opens a NEW one. So
  the newest WAV is often silent (post-audio). Look at the WAV whose timestamp
  brackets the event, not the newest one. Retention (`debug_keep=20`) prunes
  older captures.
- Debug captures now bounded: `debug_roll_sec` (600) rolls the WAV after N
  seconds and `debug_silence_sec` (60) trims leading/trailing silence on close
  (all-silent files are deleted). This stops idle monitor sessions ballooning
  into hundreds of MB of near-silence.
- Startup prune: `prune_debug_dir_startup` deletes all-silent debug WAVs
  before retention runs, so idle churn can't evict the few speech-bearing
  captures. (The daughter's 06:04–06:08 captures were lost to retention before
  this existed; re-record to analyze her speech.)

## Hotkeys (Hyprland)
- F10 / Shift+F10: outgoing PTT en→es / es→en.
- F11: speak translated text. F12: clear overlay (+ drops incoming card).
- Shift+F12: pause/resume incoming.

## Current state (all working)
- Outgoing PTT → overlay Draft/Ready → F11 TTS (speakers or virtual-mic toggle).
- Incoming translation (monitor capture, isolated from mic) → bilingual cards.
- Multimedia mode toggle (longer EOU 1600ms for continuous media speech).
- Unified overlay: single themed panel, newest-at-top, grows to screen height,
  scrollable; history toggle (all vs newest-only).
- Settings panel toggles: direction, auto-speak, incoming enable/direction,
  multimedia, history, glossary (word-boost), per-app activation, output,
  diagnostics, clear-logs, Record audio captures, Audio cleanup, Keep screen
  awake.
- Latency telemetry + JSONL event log (`events.jsonl` in state dir);
  `olt-ctl diagnostics` shows avg/min/max per stage.
- Panel toggles live under the DIAGNOSTICS section (bottom of panel). If the
  user can't see new toggles, run `omarchy-shell shell rescanPlugins` to force
  a full plugin reload (hot-reload sometimes misses additions).
- Known harmless warning: `Panel.qml:146 Parameter "exitCode" is not declared`
  (deprecation). User said to ignore it.

## Paths
- Deployed source: `~/.local/share/omatranslate/src/olt/`
- Config: `~/.config/omatranslate/config.toml`
- Widget: `~/.config/omarchy/plugins/bstail.omatranslate/` (manifest.json, BarWidget.qml, Panel.qml)
- systemd user units: `omatranslate.service`, `libretranslate-live.service`
- CLI: `~/.local/bin/olt-ctl` (ptt_start/ptt_stop/speak/clear/clear_logs/pause_incoming/status/diagnostics)
- Logs/events: `~/.local/state/omatranslate/` (olt.log, events.jsonl)
- Git repo (source of truth): `BSTail/omatranslate` (renamed from
  `BSTail/omarchy-live-translator` on 2026-09-16; GitHub redirects the old URL),
  local working clone at the repo root (this dir).

## Last completed tasks
1. Capture latency fix: `parec --latency-msec 10` (was ~2 s buffered latency
   from the default 64000-byte fragsize). Verified: tone captured at full
   amplitude with no noise floor, starting at 0.0 s.
2. Debug-capture cleanup: `prune_debug_captures` also removes zero-byte WAVs
   left by crashed/upgraded captures.
3. Telemetry consistency: incoming `asr_ms` now measures capture-start →
   ASR-final (same definition as outgoing), not first-delta → final.
4. Multimedia toggle description clarified in Panel.qml.
5. Keep-screen-awake toggle (default ON): controller runs
   `omarchy-toggle-idle stay-awake`/`allow-idle` to suppress the Omarchy
   screensaver/lock while translating. Panel toggle "Keep screen awake".
6. Audio pre-processing before ASR: `preprocess.py` (DC-block high-pass
   120 Hz + 6 dB gain). Config `preprocess_enable`/`highpass_hz`/`preamp_db`;
   panel toggle "Audio cleanup".
7. `chunk_ms` config (default 160) for ASR WebSocket frame size.
8. Debug audio capture: timestamped WAVs (`in-*/out-*.wav`), retention
   `debug_keep` (20), empty captures pruned, panel toggle "Record audio
   captures" (default ON), `clear_logs` also deletes captures.
9. Fixed incoming state/race bugs: `incoming_enabled` updates config;
   `_restart_incoming` is async and awaits the old task; unexpected pump
   errors logged.
10. Glossary phrases: Matt/Mat/Mac/Max/amat/mc/mcway, Mattacito, Maxacito,
    Danna (cousin), Roblox/Robus/Roblo/robla.
11. **Silence gate** (commit d30333a): `[incoming] gate_*` config; `SilenceGate`
    class in controller.py mutes the monitor (zero PCM) when RMS stays below
    `gate_close_rms` for `gate_close_ms`, reopens above `gate_open_rms` for
    `gate_open_ms`, with preroll buffer to avoid clipping word onsets. Active
    ONLY in multimedia mode (live calls flow unfiltered). Panel "Ignore silence"
    toggle + "Minimum speech level" slider (visible only when multimedia ON).
    `multimedia_endpointing_ms` 1600 → 2500 (fewer mid-pause splits).
12. **Nemo SIGABRT on rapid slider drag** (commit 9afbbb6): dragging the gate
    slider fired one incoming restart per notch; the old pump's `send_audio`
    raced the new stream teardown and aborted the ASR server
    (`GGML_ASSERT ne3==ne13`). FIX: `_apply_settings` runs under
    `_settings_lock` and `_restart_incoming` under `_restart_lock` (old stream
    fully tears down before the new one starts; slider drags coalesce). Also
    `_reply` now swallows BrokenPipeError/ConnectionResetError (panel poll with
    `--max-time` was disconnecting mid-response). Verified: 7 rapid slider
    changes, no abort.
13. **First-final offline snapshot bug** (found 2026-09-15): `_snapshot_utterance_audio`
    uses `audio_processed` delta vs `prev_processed_sec=0.0` on the first final,
    but `audio_processed` counts ALL audio since stream open (incl. minutes of
    gated zero-PCM), so the first refine grabs the whole 20s ring (mostly
    silence) → slow/wrong first refine. Symptom: absurd `asr_ms` (538s/989s/
    1048s) in events. FIXED (commit 4b6df8a): the pump tracks gate open/close
    transitions as ABSOLUTE ring byte positions (`ring_base` compensates front
    trimming) and `_snapshot_utterance_audio` slices exactly that window.
14. **Engine warm-up** (user: warm at startup / plugin load): `--no-warmup`
    means both models compile Vulkan pipelines lazily on first real inference
    → first refine ~2s vs ~1s warm. FIXED (commit 4b6df8a): `NemoASR.warm_stream`
    (sine burst through a throwaway stream then `clear()`) + `warm_offline`
    (one tiny transcription) run right after startup, before the incoming task.
    Both log "pipeline compiled"; non-fatal on failure. Verified no SIGABRT.
15. **Gen-orphaning bug** (commit 980b31e): the long-lived incoming stream
    captured `gen = self._incoming_gen` once at start; `clear` (F12) and
    `clear_logs` bumped the gen WITHOUT restarting the stream, so the stream
    kept a stale gen and every subsequent final was dropped as "stale"
    (symptom: Spanish draft rendered but no English translation). FIX: `clear`
    and `clear_logs` now call `_restart_incoming()`. Verified: `clear` triggers
    a fresh `incoming started` with the current gen.

## Current investigation / next up (user's priority, 2026-09-15)
- **Post-final reset production validation passed, 2026-09-16 11:19:** user
  replayed audio seven times (`in-85`..`in-91`). Every gate produced draft,
  final, translation, and one clear request/sent/ack; all acks preceded the next
  gate. Objective gate->first-draft: 11.908s, 5.853s, then 2.125s, 2.444s,
  1.962s, 2.126s, 1.958s. This fixes the prior attempt3-4 no-output collapse.
  Overlay send->receipt 0.149-0.378ms. No failures, warnings, deferrals,
  reconnects, or restarts. First attempt was genuinely slow but sent17.52s vs
  later5.04-13.52s; all post-clear fresh runners succeeded, so no functional
  code change is justified yet. Important telemetry caveat: event `asr_ms` and
  completed-log `first_delta` use the prior card/session `t_capture_start`, not
  gate onset, and include idle time. Use gate-open monotonic->first-draft
  monotonic for current comparisons. Potential next experiment only if needed:
  fixed-WAV fresh-WebSocket ABBA, unprimed vs current proven warmup. `sox` and
  `soxi` are now installed from Arch `extra` (`SoX_ng v14.8.0.1`) and available
  for the next audio analysis.
- **Safe post-final runner reset deployed, 2026-09-16 03:41:** verified warmup
  did not fix live behavior. Four gates at03:14 had first deltas +11.938s,
  +5.812s, then no events for two valid sent WAVs (6.48s/4.88s). Finals began
  `Para que...` and `orgullosa de ti...`; overlay sub-ms; processes healthy.
  WAV onset energy begins after only0.254-0.350s, so not controller send loss.
  Controlled repeated A/B in `/tmp/opencode/post-final-reset/`: retaining the
  automatic-final runner always failed at clip3 (and clip4); clear-after-final
  produced8/8 and reduced clip2 first delta ~6.14s->2.45s. Implemented
  pump-owned safe clear in controller: completed requests only while gate
  closed; clear occurs only at wire silence after trailing audio; gate epochs
  invalidate stale requests on rapid reopen; later closed completion can request
  its own reset; 2s timeout reconnects; no commit/socket rotation. Tests:
  controller33, full46, diff-check clean. Repo/deployed controller match. One
  restart03:41:18; warmup proof audio_processed3.2 at03:41:23, incoming ready
  03:41:24, active NRestarts0. Validation completed at11:14 with seven
  consecutive successful gate/final/clear cycles; see the newer entry above.
- **Verified streaming warmup deployed, 2026-09-16 02:59:** user prioritizes no
  chopped beginning and earliest visible translation; exact wording is
  secondary. Natural replays on one healthy stream measured 11.929s then 2.323s
  gate-to-first-delta with exact400ms preroll; an earlier run repeated 11.926s
  then ~2.1s. Exact sent WAVs contain real onset audio. Controlled same-PCM
  tests did not reproduce12s: fresh sockets after10/30/60s idle all ~2.774s;
  production startup shape 3.75-3.79s including55s wire idle; burst 0.884s;
  exact slow in-7 regular replay 2.782s. Thus fresh socket, idle, startup
  silence, and PCM alone are insufficient explanations; root cause unresolved.
  Artifacts: `/tmp/opencode/idle-latency-matrix/`, `startup-gate-matrix/`,
  `startup-gate-long-idle/`, `fresh-stream-burst/`, `replay-exact-slow-in7/`.
  Old warmup sent one unpaced tone blob then immediately clear/closed without
  consuming events. New `src/olt/nemo.py` waits session.updated, sends paced
  1.6s tone +1.6s silence in160ms chunks, commits, requires processed
  delta/completed proof, clears with acknowledgement, and has bounded cleanup.
  New `tests/test_nemo.py`; full suite 38 pass. Deployed with one restart at
  02:59:06: server ready02:59:07, proof completed/audio_processed3.2 at
  02:59:11, offline/incoming ready02:59:12, active NRestarts0. This is startup
  correctness, not yet a proven latency fix. Subsequent live testing and the
  retained-runner A/B are documented in the newer entries above.
- **Exact sent-PCM diagnostic added, 2026-09-16:** when existing
  `debug_capture` is enabled, every multimedia gate opening writes exactly the
  PCM bytes successfully sent to streaming NeMo into a mono PCM16/16 kHz WAV in
  `~/.local/state/omatranslate/debug/sent/`. Names encode generation, card ID,
  and gate monotonic timestamp. Captures start with the exact preroll+opening
  chunk, append only open-gate sends, and finalize on gate close, stream end,
  replacement, or shutdown. Logs include reason, path, gen/card/onset, bytes,
  and duration. Sent artifacts have their own `debug_keep` retention; clear-logs
  removes them recursively. No recognition, gate, threshold, stream recovery,
  framing, or wire-silence behavior changed. Focused tests verify exact payload,
  WAV format, gate-close lifecycle, and cancellation lifecycle. All 34 tests
  pass; real-GTK passes 8 with 1 synthetic-only skip. Repo/deployed sources
  match. One requested restart at 02:03:03 produced controller PID 1760045,
  streaming/offline NeMo PIDs 1760065/1760087, and overlay PID 1760114; all were
  ready by 02:03:05, systemd `NRestarts=0`, and the 8080 stream is established.
  No replay was run, so `debug/sent/` is created on the next natural gate open.
- **Latest live diagnosis before diagnostic deployment:** service PID 1742698
  and NeMo PIDs 1742717/1742739 stayed alive with systemd `NRestarts=0` after
  01:33. Productive gate-to-first-delta times were 2.280s, 1.989s, and 2.154s.
  Five gate openings from 01:43:48 through 01:45:24 emitted no delta/final before
  the session ended cleanly and reconnected at 01:50:34 against the same backend
  PID. This supports a nonproductive ASR session, not a hung/restarted process.
  Screenshot top card was finalized history `in-13` (`Amo papi me siento muy...`
  / `Master Daddy I feel very...`), not an active partial; later no-result gates
  created no card. Exact audio content at the missing onset remained the key
  evidence gap, which the sent-PCM diagnostic is designed to close.
- **Whole-chunk mitigation WITHDRAWN, 2026-09-16 01:33:** user reports worse.
  Only rounding reverted in repo/tests/deployed controller using apply_patch:
  exact400ms bounded bytearray preroll retains quiet/soft history; warning-only
  watchdog, wire-silent idle and refinement safeguards preserved. 32 tests pass;
  deployed source verified before patching, one restart at 01:33:09, both models
  warmed/incoming active by 01:33:12. No commits; live rollback retest pending.
  After 01:25, all openings really delivered 480ms (15360B) preroll. Drafts at
  01:27:36/01:27:52/01:28:30 lagged latest opening by 12.045/1.313/11.618s;
  finals start "Para que" / "haciendo por cada cosa" / "Para que". Overlay
  send-to-receipt 0.29-0.38ms. Openings 01:28:01/01:28:12 had no draft before
  01:28:30 (28.736s from first); 01:29:21 opening had no delta/final before
  stream ended 01:30:06. Same stream across all these events; not watchdog loss.
  Requested bounded endpointing-on/off replay BLOCKED by source inspection:
  HTTP session parser only supports endpointing_ms threshold, not enable/off;
  <=0 restores default. session.updated merely echoes fields. No replay/new
  server/global changes or oversized-threshold substitute. Evidence artifact:
  /tmp/opencode/endpointing-comparison-blocker.md. NEXT: verify installed binary
  provenance and a supported per-session disable path before same-PCM regular
  framing + commit/drain comparison; otherwise obtain approval for a separately
  controlled endpointing test. Do not add alignment padding or hard gate commits.
- **Latest mitigation deployed, 2026-09-16 01:25:** retain whole capture chunks
  for preroll (400ms target becomes 480ms at 160ms chunks), not partial chunks.
  No synthetic padding; wire-silent idle and safety guards preserved. 33 tests
  passed; service active and warmed after restart. Live replay still needed.
  Regularly framed gated PCM reproduced missing passage twice, ruling out wire
  gaps/burst framing as necessary causes. An 80ms shift restored earlier passage;
  whole-chunk 480ms gated replay restored it in two captured intervals, likely
  repeated playback of the same speech. Still starts "Ama", not "Te amo".
  Evidence supports mitigation, not universal correctness or proven upstream bug.
  Source/model suggest 80ms encoder stride and 160ms processing blocks; installed
  binary provenance remains unverified. All experiments used existing server,
  sequential sessions with ping/pong and commit/drain; no test server created.
  Artifacts: /tmp/opencode/gate-regular-003824/, gate-phase80-003824/,
  gate-regular-repeat-003824/, gate-second-400/, gate-second-480/,
  gate-first-480/. Changes remain uncommitted. NEXT: live replay same clip twice
  and genuinely different Spanish speech after idle; inspect onset and latency.
- **Controlled ABBA experiment completed (2026-09-16):** fresh gated/ungated/
  ungated/gated sessions using identical capture interval [50.24, 80.00) seconds
  from `~/.local/state/omatranslate/debug/in-20260916-003824.wav`. Both gated
  finals start "Para que"; both ungated finals retain earlier passage starting
  "Ama papi" (still not the intended "Te amo papi"). First-delta delays from
  detected activity: 12.241/2.777/2.781/12.231s. The 527360-byte detected activity
  interval [56.32,72.80) is contiguous and unchanged in gated sent PCM. This
  implicates combined gate history/framing/timing, not simple removal of that
  interval; threshold-derived activity is not proof of exact acoustic boundaries.
  Artifacts: `/tmp/opencode/gate-abba-003824/` (summary.json, input.wav, exact
  sent WAVs and event/send logs); script `/tmp/opencode/gate_experiment.py`.
  No production changes/restarts; live PIDs unchanged. Independent review found
  comparison credible but shared compute and unknown installed binary provenance
  remain limitations. Natural finals preceded EOF; 20s wait without commit does
  not prove full drain, and diagnostic reader does not answer ping frames.
  NEXT: replay exact gated sent WAV in regular 160ms frames paced by its own
  sample timeline. If onset returns, transport schedule/framing is implicated;
  otherwise concatenated PCM history remains sufficient. Prefer an independently
  owned server (historical concurrency crash risk); add ping/pong and final commit
  acknowledgement. Do not disable production gating based on this single clip.
  Last pushed commit remains 91b7bff; preroll/controller tests and notes remain
  uncommitted. Temporary artifacts are local, not committed or uploaded.
- **Latest replays (00:39-00:40, after continuous-preroll fix):** full 400 ms
  preroll delivered; gate-to-delta times 11.92s, 2.32s, 2.28s. Transcripts still
  misrecognize/drop the opening: "Ama papi" instead of "Te amo papi". The gate
  fix is active but does not resolve the missing beginning. Further threshold
  tuning is likely guessing; next step is a controlled same-audio comparison
  (gating bypassed vs enabled) recording exact sent PCM and every delta/final.
- **Watchdog safety correction (2026-09-16, deployed at 00:30):** removed automatic
  stream closure on missing deltas. A diagnostic monitor now warns after 15s,
  once per gate onset, without interrupting capture/ASR. Delayed post-final and
  cold-stream tests pass; all 29 tests pass. EOF/socket-error reconnect and
  wire-silent idle remain unchanged. Service startup verified; speech replay
  not yet live-verified. Independent static review found no actionable issues. This
  disables no-delta recovery rather than claiming safe replay is implemented;
  truly wedged connections need future continuous-capture, complete-replay
  recovery with duplicate-output handling. Next: live-check and compare
  captured onset audio against ASR output for the independent missing words.
- **Capture investigation:** events confirm the first final starts "Para que"
  and the successful later final starts "Te amo"; identical playback starting
  positions are not established. Debug WAVs are post-cleanup, pre-gate, and may
  be silence-trimmed on close without renaming, so filename plus sample offset
  is not a reliable wall-clock mapping. Finals do not roll the current capture
  (supersedes the older general debug-WAV note above). Gate buffers only
  consecutive above-threshold chunks while closed, not continuous preroll;
  soft onset loss is possible but not proven for this recording. Next controlled
  comparison should retain exact sent PCM and all ASR events.
- **Live verification 2026-09-16:** changes are deployed, service active;
  publication of the tested changes and checklist requested by the user.
  User confirms correct panel size, no visual clipping, and
  scrolling. Screenshot's successful newest card begins "Te amo"; earlier
  attempt starts "Para que", so actual missing opening words remain unresolved.
  First monitored run: gate-to-first-delta 11.91s, send-to-overlay receipt ~0.4ms;
  user saw ~12s. Later successful replay: gate-to-first-delta ~2.28s.
- **New high-priority regression evidence:** watchdog now armed across finals
  recycled sessions at 00:10:24 and 00:10:35, ~1.1s after gate reopen. This can
  discard onset audio (no replay) before legitimate ASR latency elapses. Need
  safe recovery, not assumption that one second proves a wedge. Earlier first
  run also missed beginning without a watchdog restart, so this is not a full
  explanation of all missing content. See journal and docs/TODO.md.
- **Reliability fixes after overlay review:** see `docs/TODO.md` for the current
  checklist, tests, and remaining live verification. Overlay byte-framed input
  now drains batched commands; history-off keeps the newest card visible and
  restores older cards correctly. Startup sends the configured history mode.
  Gate preroll duplication, watchdog reset across finals, and pump-exit recovery
  have regression tests. Per-card monotonic timestamps separate first ASR delta,
  controller send, and overlay receipt (not first painted frame).
- **Important refinement limitation:** incoming refinement now skips replacement
  when complete utterance bounds cannot be established. The local NeMo source's
  `audio_processed` may include buffered future audio; gate transitions are not
  ASR utterance boundaries. Previously a 20-second ring tail could replace a full
  transcript with only its ending. Streaming transcription/translation remain
  enabled. Restore refinement only with proven audio coverage, not a larger
  arbitrary ring. Installed binary/source equivalence is not yet verified.
- **Overlay sizing status:** commits 5e888b1, 0cd7d2b, and 4f5ec01 fixed monitor
  lookup, resize propagation, and panel shrink. User confirmed no shrink, but
  missing text persisted. The initial 10-20-second display delay remains a live
  investigation, not a confirmed GTK sizing failure.
- **NEXT BUG (user's priority): overlay fixed-height clipping.** The overlay
  translation window has a too-short FIXED height that clips the bottom of
  longer translated sentences. Fix: make the window height track content
  (auto-size, capped at screen height, scroll only when at the cap). Lives in
  `src/olt/overlay.py`. Historical note superseded by the sizing status above.
- **CLOSED OUT — the idle-gap "missing-popup" bug.** (See the (a)+(b)-lite
  workaround note below and README.) Root cause was upstream; filed as #48 and
  worked around locally; verified with 20s and 2-minute idle-gap tests.
- **ROOT CAUSE FOUND — it is an IDLE bug, not a startup bug (user's call, now
  proven).** After a clip finalizes, a long run of *zero-PCM silence* fed to
  the streaming RNNT server makes the next clip produce NO deltas and NO
  final: the server stops decoding until the stream is torn down. Reproduced
  in isolation (fresh NeMo server, no controller/gate/overlay) on BOTH the
  Vulkan and CPU backends, so it is upstream, not our code.
- **The trigger is FULL 160ms zero-PCM frames, not small frames or wire
  silence.** Isolated CPU-server matrix (clip → 6s trailing silence → gap →
  clip): wire-silent gaps ≤25s decode fine; 1ms zero-PCM keepalives decode fine
  even at 60s (30/45/60s all pass); 16/40/80ms zero-PCM frames decode fine;
  full 160ms zero-PCM frames (what the RMS gate emits while closed) at gap
  ≥15s → the second clip dies. `input_audio_buffer.clear` does NOT recover it.
  Endpointing OFF (no EOU) still decodes continuously through 60s of full
  zero-PCM (hundreds of deltas), so the wedge is specific to the token-silence
  EOU path, not the base cache-aware RNNT decoder.
- **Upstream SIGABRT is the same family.** The Vulkan streaming server crashed
  at 18:04:50 with `GGML_ASSERT(ne3 == ne13) failed` at ggml-cpu.c:1270 inside
  `CacheAwareEncoder::encode` (mul_mat), matching the known intermittent
  warmup crash. 9 prior nemo-speech SIGABRT cores exist on this machine. The
  CPU isolated server did NOT crash during the stall matrix, so the stall and
  the Vulkan abort are separate manifestations of the same fragile streaming
  path.
- **No upstream report exists yet.** GitHub search of NVIDIA/NeMo-Speech.cpp
  for `ne3/ne13/GGML_ASSERT` returns nothing; idle/silence/stall search returns
  only unrelated issues (#40 token-silence EOU, #19 TTS DC, #8 converter).
  Filing a new upstream issue is warranted once we have a minimal repro.
- **FILED upstream: https://github.com/NVIDIA/NeMo-Speech.cpp/issues/48**
  ("Streaming RNNT wedges after sustained zero-PCM silence; Vulkan aborts with
  GGML_ASSERT(ne3 == ne13)"). Includes the full gap matrix, the 60s 1ms
  keepalive result, the endpointing-OFF control, coredump backtrace, and the
  fire_eou/reset_utterance hypothesis. Awaiting maintainer response.
- **Controller now self-heals.** `incoming()` calls `asr.restart()` on loop
  error (previously hot-looped ECONNREFUSED every 2s after a crash), and
  `NemoASR.restart()` was added. Deployed + verified: two post-restart clips
  translated; the idle-gap failure has not re-fired since.
- **Workaround (a)+(b)-lite IMPLEMENTED + VERIFIED (commits 5de20c3, a7f6277).**
  The silence gate now goes *wire-silent* when closed: on close it emits a
  bounded run of trailing silence (equal to the EOU window, so token-silence EOU
  still fires) and then sends NO frames until the next re-open. This removes the
  zero-PCM trigger entirely. A stall watchdog (b-lite) recycles the stream
  session if the gate is open with speech flowing but no delta arrives within 1s
  — armed only AFTER the stream has produced its first delta (a7f6277 fixes a
  false trip on the cold first utterance). (c) Silero VAD EOU remains deferred.
  **LIVE TESTS (both passed):** clip1 → 20s idle gap → clip2: both translated
  (clip1 54→46 chars, clip2 36→65 chars). clip1 → **2-minute** idle gap → clip2:
  both translated (clip1 58→46, clip2 36→50), no stall, no watchdog trip, stream
  stayed open the whole time. User confirmed the on-screen result matches.
- **Senior-review course correction (still holds):** upstream issue #40 / PR
  #41 document premature EOU and hard-reset corruption, not this stall. Do NOT
  cherry-pick PR #41 or add gate-triggered `input_audio_buffer.commit`.
- **Explicit realtime route:** the installed 0.1.0 binary accepts both
  `/v1/realtime` and `/v1/audio/transcriptions/realtime` (HTTP 101 verified).
  Official docs call `/v1/realtime` a backward-compatibility alias; migrate to
  the explicit audio route after this diagnosis, not during it.
- **Clipboard OCR:** user retested and says it is working. The speculative
  `_clipboard_last` idempotency experiment was removed from source before
  commit; no OCR behavior change is planned now.
- **Graft:** local structural graph rebuilt from scratch with graft 0.18.0 and
  `graft check` passes. No `GRAFT_API_KEY` is available, so the deep semantic
  tier was removed rather than retaining stale summaries.
- **User test results (2026-09-15, excellent)**: multiple daughter audio files
  translated well; the 4-second clip (historically the hardest) came out 100%
  correct on the most recent run. Full beginning + end of sentences captured.
- FIX FIRST — incoming audio loss around finalization (bug, found in review):
  in `_incoming_once`, when `.completed` arrives the controller awaits NMT while
  the pump keeps pushing fresh audio into a stream whose results are never read,
  then tears down capture and reopens it → audio spoken during translation is
  dropped + capture gap. FIXED (commit 6f783b8): capture + ASR stream stay open
  across consecutive finals; finals are translated in background workers with a
  generation counter (`_incoming_gen`) so stale results can't resurrect cleared
  cards. Deployed + service restarted + verified.
- **Re-score WER properly**: DONE with jiwer 4.0.0 (`tools/score_wer.py`).
  Corrected WER: parakeet-tdt 76.3%/47.0%, nemotron-3.5 93.2%/60.6% (clip 1/2).
  S/D/I: parakeet 18/0/27 + 12/0/19; nemotron 21/1/33 + 21/1/18. Insertions
  dominate (both models add content vs the verbatim reference). Prior difflib
  numbers were NOT valid WER. Reference text remains authoritative; disputed
  extra content still needs a Spanish speaker to arbitrate.
- **Head-to-head architecture comparison** (NOT a confirmed two-tier plan yet):
  (a) buffered/chunked Parakeet-only vs (b) Nemotron draft → Parakeet refine.
  Measure draft latency / final latency / text stability on short + continuous
  speech. DATA COLLECTED: Parakeet via persistent serve (POST
  /v1/audio/transcriptions) is fast — 2s chunk ~0.29s, 3s ~0.12s, 5s ~0.14s,
  8s ~0.18s; full 36s clip ~0.74s, 30s clip ~0.58s. C++ runtime
  `streaming_recognize` is genuinely unavailable for Parakeet
  (recognizer.cpp:209-214 throws "offline-only" for non-CTC heads without
  cache streaming). So two-tier = Nemotron streaming drafts + Parakeet server
  re-transcribing each final utterance (~0.1-0.3s added latency). Parakeet-only
  streaming is impossible in this runtime (NeMo buffered chunking = separate
  Python/torch stack). Remaining: measure end-to-end draft→final latency + text
  stability on a real utterance stream before implementing.
- **Two-tier incoming translation** (user idea): IMPLEMENTED + validated. `ASRConfig`
  gains `offline_model/offline_port/offline_enabled`; `NemoASR.start_offline()`
  runs a 2nd `serve` (Parakeet) on 8081; incoming captures a ~20s raw PCM ring
  and, on each `.completed`, uses the server's `audio_processed` delta to bound
  the utterance and POSTs it to Parakeet via `transcribe_offline()`
  (multipart); a background task re-transcribes + re-translates and updates the
  card IN PLACE (same id) if text differs. Generation counter drops stale
  refinements.   `_snapshot_utterance_audio` skips sub-300ms utterances (empty
  WAV 500s Parakeet). Validated live: refine works, Parakeet more complete than
  Nemotron draft. Parakeet still misrecognizes ("caballete"→"caballo"/"horse").
  **Default on** (`offline_enabled=true`); panel toggle "Two-tier accuracy"
  turns it off. Control `set` action wired (`two_tier`). Service restarted
  clean; verified toggle on/off (Parakeet process starts/stops correctly).
- **Auto-detect input level (gating)** — user idea, agreed in principle: gate
  incoming translation on a minimum monitor signal level. Panel slider, default
  low. DISABLED while an active call is happening (user confirmed). Gating must
  not starve ASR of trailing silence (or endpointing never completes); call
  detection needs explicit design (`media.role=Communication` + manual override).
  IMPLEMENTED as the silence gate (multimedia-only, default ON) — see Last
  completed tasks #11. Live-call mode (multimedia OFF) flows unfiltered, which
  satisfies "disabled during calls".
- **Evaluate a higher-accuracy ASR model**: Parakeet TDT 0.6B v3 pulled + timed
  (Vulkan q8_0): preliminary difflib WER 64.4%/34.8% vs Nemotron 83.1%/45.5%;
  15 s file transcription ~1.1 s vs ~4.3 s (both incl. subprocess startup).
  CAVEAT: difflib ≠ minimum edit distance; re-score with jiwer. Canary 1B Flash /
  v2 remain candidates.
- Calibration clips: `~/Downloads/es-calibrate-{1,2}.{m4a,txt}` (verbatim
  reference). Streaming model produces extra fluent-looking content on long
  continuous speech (cause unverified). Accuracy ≈ 6–7/10. Pre-processing /
  right_context / language prompt / punctuation / batching don't help.
- Analyze the recorded daughter WAVs in `~/.local/state/omatranslate/debug/`
  (level/bandwidth/silence) to explain why child speech is still hard. NOTE:
  match each WAV to the event timestamp; the newest WAV is often post-audio
  silence (see workarounds).
- User idea (future): use recorded WAVs as a translation-speed benchmark
  across sentence lengths / settings.
- Word boosting is currently a no-op: the shipped GGUF has the tokenizer VOCAB
  (`asr.tokenizer.vocab`, 13087 pieces) but lacks `asr.tokenizer.spm_model`
  (the base64 SentencePiece proto the C++ runtime needs to tokenize arbitrary
  boost phrases). Feasibility verified: re-convert the .nemo checkpoint with
  `convert_model.py` (in `/tmp/opencode/nemo-speech-src/`) which embeds
  spm_model. Requires: 2.37 GB .nemo download + a venv with torch, numpy,
  gguf, sentencepiece, protobuf, safetensors, PyYAML, huggingface-hub
  (see `requirements.txt`). ~863 GB free disk. Ready to execute when user
  approves (heavy download + torch install).
- Then: package as an Omarchy plugin (installer, config, docs) — LAST step.
- Then: endpointing/VAD tuning; Phase 2 incoming speech-to-speech (opt-in).
  Model/voice management deferred.

## Review findings (independent agent, 2026-09-14) — being addressed now
1. Incoming audio loss around finalization (bug) — see FIX FIRST above.
2. WER methodology: `difflib.SequenceMatcher` ≠ minimum edit distance; use jiwer.
3. "Parakeet offline-only" too broad; buffered chunking exists in NeMo.
4. Drafts are source-only (not translated drafts).
5. Overlay: separate creation vs revision order; preserve scroll; generation IDs
   to invalidate stale results on clear/pause/direction/stop.
6. Resource claims unverified (file speed ≠ live latency; 2 models ≠ 2× VRAM).
7. Gating can starve endpointing; headphones stop acoustic (not digital) feedback
   — TTS into the captured sink's monitor still loops.
8. Misc: +6 dB preamp can clip; debug WAVs are post-processing; custom WS client
   lacks ping/pong/fragmentation. The older note that the explicit realtime
   route 404s is stale: the installed binary now returns HTTP 101 for both the
   explicit and compatibility routes (verified 2026-09-15).

## Senior review (2026-09-15) — current guardrails
- Official API docs say realtime clients may send one `session.update`, binary
  PCM16, and finish with `input_audio_buffer.commit`; `clear` discards buffered
  audio. The explicit route is `/v1/audio/transcriptions/realtime`; the old
  `/v1/realtime` route is a compatibility alias when VoiceChat is absent.
- Official endpointing docs: token-silence is the default, while VAD-driven EOU
  requires a separate Silero GGUF. The installed binary supports `--vad-model`
  and VAD-based endpointing, but the project does not currently install one.
- NVIDIA/NeMo-Speech.cpp issue #40 is open. It confirms token-silence can mistake
  RNNT decode latency for acoustic silence and fire too early. PR #41 proposes
  a soft EOU checkpoint and passes automated builds, but is unmerged and has
  unresolved reviewer concerns around punctuation leakage and forced EOU.
- Issue #22 separately reports punctuation leaking into the next final on a
  persistent stream. These upstream reports make gate-triggered hard commits a
  risky workaround rather than a safe first choice.
- The exploratory WS scripts in `/tmp/opencode/` are NOT project tests and their
  results are not authoritative: some used 100 ms frames instead of production
  160 ms, sent audio unpaced or in one large frame, ran beside the live server,
  and one Piper raw clip was 22.05 kHz while declared as 16 kHz.
- Runtime provenance remains unclear: `~/.local/bin/nemo-speech` is a manually
  deployed 0.1.0 binary (not package-owned); local upstream checkout is
  NVIDIA main `a5b6953`. Record the binary's exact source commit/build options
  before comparing fixes or filing a new upstream issue.
- Graft is a gitignored local cache by design. A clean structural rebuild is
  current; without an API key there are no semantic summaries. Run
  `graft build --deep` again only after configuring `GRAFT_API_KEY`.

## Naming / branding
- Plugin renamed to **OmaTranslate** (`bstail.omatranslate`) — manifest,
  BarWidget, Panel header, tooltip, README. Bar layout in
  `~/.config/omarchy/shell.json` updated to `bstail.omatranslate`.
- Old plugin dir moved to `~/.config/omarchy/plugins.old/` (out of active
  plugins). Full rename to **omatranslate** completed 2026-09-16: GitHub repo,
  systemd service (`omatranslate.service`), and state/config/share dirs
  (`~/.local/state/omatranslate`, `~/.config/omatranslate`,
  `~/.local/share/omatranslate`). The internal `olt` shorthand (olt-ctl,
  olt.log, olt-overlay) is unchanged.
- User registered omatranslate.com — do NOT reference the dot-com anywhere yet.

## Panel theming (2026-09-14)
- Working: "OmaTranslate" title in theme accent; "Running" green / "Stopped" red
  (theme `green`/`red` tokens read via a FileView on the current theme's
  colors.toml — the shell's Color singleton only exposes foreground/accent/
  urgent, so green/cyan are read directly); section headers themed:
  OUTGOING=accent, INCOMING=cyan, GLOSSARY=green, ACTIVATION=accent,
  DIAGNOSTICS=green (PanelSectionHeader accepts `foreground`).
- REVERTED / not working (do NOT retry without a new approach):
  1. Glow on the Start/Stop button — caused a duplicate "Stop" button artifact
     and a clipped glow edge; removed.
  2. Colored border on Start/Stop via a custom `ThemedButton.qml` — the
     component failed to load ("Ui.Button - Ui is neither a type nor a
     namespace"; inside a plugin file the type is just `Button`, not
     `Ui.Button`), which broke the whole panel and hid the bar icon. Reverted.
  3. Accent-colored selected chip text in ButtonGroup — `Button` paints selected
     text via `Style.selectedStateColor()` = theme `selected-color` token
     (bright foreground, not accent); overriding `_selectedColor` requires a
     wrapper component, which is what failed in #2. Parked.
- QML lessons: plugin files import `qs.Ui` but reference types unqualified
  (`Button`, `Toggle`, `PanelSeparator`). `qmllint` is at `/usr/lib/qt6/bin/qmllint`
  (not on PATH). Shell kit docs: `~/.agents/skills/omarchy/theming.md` +
  `plugins.md`; kit source `/usr/share/omarchy/shell/Ui/*.qml` and
  `/usr/share/omarchy/shell/Commons/{Color,Style,Border}.qml`.

## Privacy / logging (commits cf61461, 6c61f49, 69cfec3, 3824eac)
- Default is privacy-first: transcript/clipboard TEXT is NOT written to
  `events.jsonl` or the human log unless the relevant toggle is ON. Otherwise
  only metadata (direction, char lengths, timings) is logged.
- `debug_capture` toggle (panel label "Save all audio transcripts") gates BOTH
  full transcript text in events AND the WAV debug captures.
- Clipboard text logging has its OWN toggle: `[clipboard] log_text` (default
  false) + panel toggle "Log clipboard text" under DIAGNOSTICS (commit
  3824eac). When ON, clipboard source/target text is logged; when OFF only
  src_len/tgt_len. Wired via status `clipboard_log_text` + `_apply_settings`.
- 24h retention for ALL plugin log files: `logging.prune_events(86400)` (line
  age) + `logging.prune_log_files(86400)` (mtime) run at startup and hourly via
  `Controller._events_pruner()`. `olt.log` now uses TimedRotatingFileHandler
  (midnight, backupCount=1) so the active log never holds more than a day.
- `clear_logs` / panel "Clear logs & history" still wipes events.jsonl + WAVs +
  olt.log.
- LibreTranslate retry added (engines.py `_post`): one retry after 0.5s on
  transient failure; both translate and detect use it.
- Hardware note: NO discrete VRAM — Intel Core Ultra 7 258V + Arc 130V/140V
  iGPU uses unified memory (32 GB). Both ASR models are 0.6B q8_0 (~741 MB +
  ~714 MB), trivial vs 32 GB. Two-tier default-ON is fine.

## User preferences
- Native English speaker; main direction en→es outgoing, es→en incoming.
- Assume bi-directional works; assume Bluetooth/headphones need no echo pause.
- Wants clean, futuristic, dynamic UI following the active Omarchy theme.
- Prefers working plugin over model management for now.

## Clipboard translation — DONE (text + image OCR)
- Controller action `clipboard_translate`: reads Wayland clipboard, branches on
  content type, translates to the opposite en/es language, writes back via
  `wl-copy`, and schedules a guarded 30s auto-clear.
- Text path: `wl-paste -n` → `LibreTranslate.detect()` → translate → `wl-copy`.
- Image path (commit 50040d4): `wl-paste --list-types` detects `image/*`;
  `_clipboard_ocr()` reads the image bytes fully into memory, then feeds them
  to `tesseract stdin stdout -l spa+eng --psm 6 --tessdata-dir <dir>`. KEY
  LESSON: asyncio subprocesses cannot pipe one child's stdout directly into
  another's stdin (a StreamReader has no `fileno`); materialize the bytes in
  between. Verified live: ES image → EN text, EN image → ES text, JPEG + PNG
  both work, text path still works.
- Non-pair text (e.g. French) → translate toward the user's own language
  (`cfg.outgoing.language[:2]`).
- Auto-clear: `[clipboard] clear_sec = 30`; clears only if the clipboard still
  holds OUR translation (never wipes text the user copied in the meantime).
- No clipboard history manager on this machine (no cliphist/copyq/clipman);
  Wayland clipboard is single-slot, so "clear history" = `wl-copy --clear`.
- OCR config: `[clipboard] ocr_enabled/ocr_psm/ocr_lang/tessdata_dir`.
  Tessdata shipped at `~/.local/share/omatranslate/tessdata/`
  (eng + spa traineddata; no root needed). `spa.traineddata` is from
  tesseract-ocr/tessdata_fast (2.3 MB).
- `LibreTranslate.detect()` added (engines.py); `ClipboardConfig` added
  (config.py); status exposes `clipboard_clear_sec` and `clipboard_ocr`.
- Bar icon: second BarIconButton (glyph `\uf0ea` = Font Awesome `fa-paste`),
  always visible; left-click runs `olt-ctl clipboard_translate`; tooltip
  "Translated → clipboard" (or "failed") via `root.bar.showTooltip`, hidden by
  a 2s Timer. First glyph attempt `\uf328` was the OpenBSD pufferfish logo —
  wrong. Glyph lookup: fontTools cmap or Nerd Fonts `glyphnames.json`.
- BACK BURNER: LM Studio vision translation (user said later). Noted as a
  possible future trade-off: Google Translate image API (one image, minimal
  privacy loss) — but staying fully local for now.
