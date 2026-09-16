"""Controller: owns the engines, the overlay, and the two pipelines.

- Outgoing: push-to-talk mic capture → ASR → NMT → overlay (Draft/Ready) →
  Piper TTS → virtual mic, gated on an explicit Speak action.
- Incoming: monitor capture → ASR → NMT → overlay (Incoming).

Hotkeys and the overlay talk to the controller over a tiny local HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import subprocess
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audio, engines, logging, nemo
from .config import Config, load

log = logging.get()

# Diagnostic only: observed legitimate first-delta latency exceeds 11 seconds.
# A timeout cannot prove a wedge. Recovery needs continuous capture and complete
# replay before it can safely replace a stream that has accepted speech.
_ASR_DELAY_WARNING_S = 15.0


class SilenceGate:
    """Mute the monitor when it stays quiet (multimedia mode only).

    The incoming monitor is a continuous stream; between real audio there is
    near-silence (digital noise floor, faint system sounds). Feeding that to
    the streaming ASR can produce hallucinated finals. This gate only lets real
    audio through once the signal rises above the open threshold for long
    enough. A short preroll buffer is flushed on re-open so word onsets are
    not clipped.

    When the gate closes it emits a bounded run of trailing silence — just long
    enough for the server's token-silence endpointing to fire — and then goes
    *wire-silent* (sends no frames at all). This is deliberate: a sustained run
    of full zero-PCM frames (the previous behaviour) wedges the streaming RNNT
    server (NVIDIA/NeMo-Speech.cpp#48), while wire silence is safe.

    RMS is computed on 16 kHz mono s16le samples.
    """

    def __init__(
        self,
        open_rms: float,
        close_rms: float,
        open_ms: int,
        close_ms: int,
        preroll_ms: int,
        trailing_ms: float,
    ):
        self.open_rms = max(0.0, open_rms)
        self.close_rms = max(0.0, close_rms)
        self.open_ms = max(0, open_ms)
        self.close_ms = max(0, close_ms)
        self.preroll_ms = max(0, preroll_ms)
        self.trailing_ms = max(0.0, trailing_ms)
        self._open = True
        self._loud_ms = 0.0
        self._quiet_ms = 0.0
        self._trailing_left_ms = 0.0
        self._preroll: bytearray = bytearray()
        self._preroll_cap = int(audio.RATE * self.preroll_ms / 1000) * 2

    @property
    def open(self) -> bool:
        return self._open

    def _rms(self, chunk: bytes) -> float:
        n = len(chunk) // 2
        if n == 0:
            return 0.0
        samples = struct.unpack(f"<{n}h", chunk)
        return math.sqrt(sum(s * s for s in samples) / n)

    def process(self, chunk: bytes, chunk_ms: int) -> bytes | None:
        """Return the audio to forward for this chunk, or ``None`` to send none."""
        if not chunk:
            return None
        rms = self._rms(chunk)
        if self._open:
            if rms <= self.close_rms:
                self._quiet_ms += chunk_ms
                if self._quiet_ms >= self.close_ms:
                    self._open = False
                    self._quiet_ms = 0.0
                    self._trailing_left_ms = self.trailing_ms
                    log.info(
                        "silence gate closed (rms %.0f < %.0f for %dms)",
                        rms, self.close_rms, self.close_ms,
                    )
                else:
                    return chunk
            else:
                self._quiet_ms = 0.0
                return chunk
        # Closed (or just closed): gate real speech as preroll.
        if rms > self.open_rms:
            self._loud_ms += chunk_ms
            if self._loud_ms >= self.open_ms:
                self._open = True
                self._loud_ms = 0.0
                self._trailing_left_ms = 0.0
                preroll = bytes(self._preroll)
                self._preroll.clear()
                log.info(
                    "silence gate opened (rms %.0f > %.0f for %dms, %d bytes preroll)",
                    rms, self.open_rms, self.open_ms, len(preroll),
                )
                return preroll + chunk
            self._preroll.extend(chunk)
            if len(self._preroll) > self._preroll_cap:
                del self._preroll[: len(self._preroll) - self._preroll_cap]
        else:
            self._loud_ms = 0.0
            self._preroll.clear()
        # Still closed: bounded trailing silence, then wire silence.
        if self._trailing_left_ms > 0:
            self._trailing_left_ms = max(0.0, self._trailing_left_ms - chunk_ms)
            return bytes(len(chunk))
        return None


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.asr = nemo.NemoASR(cfg.paths, cfg.asr)
        self.nmt = engines.LibreTranslate(cfg.paths)
        self.tts = engines.Piper(cfg.paths, cfg.tts)
        self.overlay_proc: asyncio.subprocess.Process | None = None
        self.incoming_task: asyncio.Task | None = None
        self.outgoing_task: asyncio.Task | None = None
        self._overlay_stdin = None
        self._card_seq = 0
        self._incoming_card_id: str | None = None
        # Bumped whenever incoming is cleared/paused/restarted/direction-changed.
        # Background finalizers capture it and drop their result if it no longer
        # matches, so stale translations can't resurrect a cleared card.
        self._incoming_gen = 0
        # Settings are applied serially on a queue (each incoming stream
        # restart is expensive and must not overlap). This also coalesces a
        # slider drag into the latest value instead of one restart per notch.
        self._settings_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()

    # -- overlay -----------------------------------------------------------

    async def start_overlay(self) -> None:
        env = os.environ.copy()
        # gtk4-layer-shell must be linked before libwayland-client; preloading
        # it is the supported workaround for Python (PyGObject) apps.
        env["LD_PRELOAD"] = "/usr/lib/libgtk4-layer-shell.so"
        self.overlay_proc = await asyncio.create_subprocess_exec(
            "python3",
            "-m",
            "olt.overlay",
            self.cfg.overlay.position,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._overlay_stdin = self.overlay_proc.stdin
        self.overlay_send({"cmd": "history", "enabled": self.cfg.overlay.history})
        asyncio.create_task(self._watch_overlay_stderr())
        log.info("overlay started (pid %s)", self.overlay_proc.pid)

    async def _watch_overlay_stderr(self) -> None:
        assert self.overlay_proc is not None and self.overlay_proc.stderr is not None
        while True:
            line = await self.overlay_proc.stderr.readline()
            if not line:
                break
            log.info("overlay: %s", line.decode(errors="replace").rstrip())

    def overlay_send(self, msg: dict) -> None:
        if self._overlay_stdin is not None:
            try:
                self._overlay_stdin.write((json.dumps(msg) + "\n").encode())
            except Exception as exc:  # overlay died; don't crash the controller
                log.error("overlay write failed: %s", exc)

    # -- pipelines ---------------------------------------------------------

    def _next_card(self, direction: str) -> str:
        self._card_seq += 1
        return f"{direction}-{self._card_seq}"

    def _speech_contexts(self) -> list[dict] | None:
        if not self.cfg.glossary.enabled:
            return None
        phrases = [p.strip() for p in self.cfg.glossary.phrases if p.strip()]
        if not phrases:
            return None
        return [{"phrases": phrases, "boost": float(self.cfg.glossary.boost)}]

    def _activation_ok(self) -> bool:
        """True when incoming/outgoing translation may run for the focused window."""
        if not self.cfg.activation.enabled:
            return True
        want_class = self.cfg.activation.app_class.strip().lower()
        want_title = self.cfg.activation.app_title.strip().lower()
        if not want_class and not want_title:
            return True
        try:
            out = subprocess.run(
                ["hyprctl", "activewindow", "-j"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            data = json.loads(out)
        except Exception:
            return True  # can't tell; don't block translation
        cls = (data.get("class") or "").lower()
        title = (data.get("title") or "").lower()
        if want_class and want_class not in cls:
            return False
        if want_title and want_title not in title:
            return False
        return True

    async def outgoing(self, source_lang: str, target: str, stop_event: asyncio.Event) -> None:
        if not self._activation_ok():
            log.info("outgoing blocked: focused window is not an allowed call app")
            return
        card_id = self._next_card("out")
        t0 = time.monotonic()
        self.overlay_send(
            {"cmd": "card", "id": card_id, "direction": "out",
             "source": "", "target": "", "state": "draft"}
        )
        cap = audio.mic_capture(self.cfg)
        stream = await self.asr.connect_stream(
            source_lang, speech_contexts=self._speech_contexts()
        )
        await cap.start()
        log.info("outgoing started (%s→%s)", source_lang, target)
        partial = ""
        pump_task: asyncio.Task | None = None
        finalized = False
        t_asr_final: float | None = None
        try:
            async def pump():
                while True:
                    chunk = await cap.read_chunk(self.cfg.chunk_ms)
                    if not chunk:
                        break
                    await stream.send_audio(chunk)

            pump_task = asyncio.create_task(pump())

            async def reader():
                nonlocal partial, finalized, t_asr_final
                async for ev in stream.events():
                    t = ev.get("type")
                    if t == "conversation.item.input_audio_transcription.delta":
                        partial = (partial + ev.get("delta", "")).strip()
                        self.overlay_send(
                            {"cmd": "card", "id": card_id, "direction": "out",
                             "source": partial, "target": "", "state": "draft"}
                        )
                    elif t == "conversation.item.input_audio_transcription.completed":
                        final = ev.get("transcript", partial).strip()
                        finalized = True
                        t_asr_final = time.monotonic()
                        await self._finalize_outgoing(card_id, final, source_lang, target)
                        return

            reader_task = asyncio.create_task(reader())

            # Wait for either the push-to-talk release or a natural endpoint.
            await stop_event.wait()
            log.info("outgoing stop requested; finalizing")
            await cap.stop()
            pump_task.cancel()
            await stream.commit()
            try:
                await asyncio.wait_for(reader_task, timeout=30)
            except asyncio.TimeoutError:
                log.warning("outgoing finalize timed out")
                if partial and not finalized:
                    await self._finalize_outgoing(card_id, partial, source_lang, target)
            logging.log_event({
                "kind": "outgoing",
                "direction": f"{source_lang[:2]}→{target}",
                "card": card_id,
                "capture_start_s": round(t0, 3),
                "asr_final_s": round(t_asr_final, 3) if t_asr_final else None,
                "asr_ms": round((t_asr_final - t0) * 1000, 1) if t_asr_final else None,
            })
        finally:
            if pump_task and not pump_task.done():
                pump_task.cancel()
            await cap.stop()
            await stream.close()

    async def _auto_speak_after(self, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        if getattr(self, "_ready_card", None) is not None:
            log.info("auto-speak after %.1fs", delay_s)
            await self.speak_ready()

    async def _finalize_outgoing(
        self, card_id: str, text: str, source_lang: str, target: str
    ) -> None:
        if not text:
            self.overlay_send({"cmd": "clear", "id": card_id})
            return
        t_nmt0 = time.monotonic()
        try:
            translated = await self.nmt.translate(text, source_lang[:2], target)
        except Exception as exc:
            log.error("outgoing translation failed: %s", exc)
            self.overlay_send(
                {"cmd": "card", "id": card_id, "direction": "out",
                 "source": text, "target": f"[translation failed: {exc}]",
                 "state": "ready"}
            )
            return
        nmt_ms = (time.monotonic() - t_nmt0) * 1000
        log.info("outgoing translated (%s→%s): %d chars → %d chars",
                 source_lang[:2], target, len(text), len(translated))
        logging.log_event({
            "kind": "translation",
            "direction": f"{source_lang[:2]}→{target}",
            "card": card_id,
            "src_len": len(text),
            "tgt_len": len(translated),
            "nmt_ms": round(nmt_ms, 1),
            **({"source": text, "target": translated} if self.cfg.debug_capture else {}),
        })
        self.overlay_send(
            {"cmd": "card", "id": card_id, "direction": "out",
             "source": text, "target": translated, "state": "ready"}
        )
        # Store the current card for the Speak action.
        self._ready_card = {"id": card_id, "source": text, "target": translated,
                            "target_lang": target}
        # Auto-speak: speak immediately after finalizing (used when auto-speak
        # is enabled). Manual Speak (F11) still goes through speak_ready().
        if self.cfg.outgoing.auto_speak_after_ms > 0:
            asyncio.create_task(
                self._auto_speak_after(self.cfg.outgoing.auto_speak_after_ms / 1000)
            )

    async def speak_ready(self) -> None:
        card = getattr(self, "_ready_card", None)
        if card is None:
            log.info("speak requested but no ready card")
            return
        self.overlay_send({"cmd": "card", "id": card["id"], "direction": "out",
                           "source": card["source"], "target": card["target"],
                           "state": "spoken"})
        t_tts0 = time.monotonic()
        try:
            wav = await self.tts.synthesize(card["target"], card["target_lang"])
        except Exception as exc:
            log.error("TTS failed: %s", exc)
            self.overlay_send({"cmd": "card", "id": card["id"], "direction": "out",
                               "source": card["source"], "target": card["target"],
                               "state": "ready"})
            return
        tts_ms = (time.monotonic() - t_tts0) * 1000
        logging.log_event({
            "kind": "tts",
            "card": card["id"],
            "target_lang": card["target_lang"],
            "tts_ms": round(tts_ms, 1),
            "destination": self.cfg.outgoing.output_destination,
            **({"text": card["target"]} if self.cfg.debug_capture else {}),
        })
        # When playing through speakers, the monitor (incoming capture) hears
        # our own TTS and would re-translate it. Pause incoming for the
        # duration of playback to break the echo loop.
        was_paused = False
        if self.cfg.outgoing.output_destination == "speakers":
            if self.incoming_task and not self.incoming_task.done():
                self.incoming_task.cancel()
                self.incoming_task = None
                was_paused = True
                log.info("incoming paused during TTS playback")
        t_play0 = time.monotonic()
        await self._play(wav)
        play_ms = (time.monotonic() - t_play0) * 1000
        logging.log_event({
            "kind": "playback",
            "card": card["id"],
            "destination": self.cfg.outgoing.output_destination,
            "play_ms": round(play_ms, 1),
        })
        if was_paused:
            self.incoming_task = asyncio.create_task(self.incoming())
            log.info("incoming resumed after TTS playback")
        self._ready_card = None

    async def _play(self, wav: bytes) -> None:
        dest = self.cfg.outgoing.output_destination
        if dest == "speakers":
            await self._play_to_device(wav, self.cfg.outgoing.speakers_sink)
        else:
            await self._play_to_virtual_mic(wav)

    async def _play_to_device(self, wav: bytes, device: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "paplay",
            "--raw",
            f"--rate={self.cfg.tts.sample_rate}",
            "--format=s16le",
            "--channels=1",
            "--device",
            device,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(input=wav)
        if proc.returncode != 0:
            log.error("paplay failed: %s", err.decode(errors="replace"))

    # -- clipboard translation --------------------------------------------

    async def clipboard_translate(self) -> None:
        """Translate the current Wayland clipboard to the opposite language.

        Reads the clipboard, branching on content type: text is translated
        directly; an image is OCR'd with tesseract and the recognized text is
        translated. The result is written back with wl-copy and a guarded
        auto-clear removes it shortly after so translated text does not linger.
        """
        has_image = await self._clipboard_has_image()
        if has_image and self.cfg.clipboard.ocr_enabled:
            text = await self._clipboard_ocr()
            if not text:
                log.info("clipboard translate: OCR found no text in the image")
                return
        else:
            text = await self._clipboard_read()
            if not text:
                log.info("clipboard translate: clipboard is empty or non-text")
                return
        src = await self.nmt.detect(text)
        if src not in ("en", "es"):
            # Foreign text: translate toward the user's own language.
            tgt = self.cfg.outgoing.language[:2]
            log.info("clipboard translate: detected %r (non-pair); targeting %r",
                     src or "unknown", tgt)
        else:
            tgt = "en" if src == "es" else "es"
            log.info("clipboard translate: detected %r; targeting %r", src, tgt)
        try:
            translated = await self.nmt.translate(text, src or "auto", tgt)
        except Exception as exc:
            log.error("clipboard translation failed: %s", exc)
            return
        if not translated:
            return
        await self._clipboard_write(translated)
        logging.log_event({
            "kind": "clipboard",
            "direction": f"{src or 'auto'}→{tgt}",
            "src_len": len(text),
            "tgt_len": len(translated),
            **({"source": text, "target": translated}
               if self.cfg.clipboard.log_text else {}),
        })
        if self.cfg.clipboard.log_text:
            log.info("clipboard translated (%s→%s): %r → %r",
                     src or "auto", tgt, text, translated)
        else:
            log.info("clipboard translated (%s→%s): %d chars → %d chars",
                     src or "auto", tgt, len(text), len(translated))
        if self.cfg.clipboard.clear_sec > 0:
            asyncio.create_task(
                self._clipboard_clear_after(self.cfg.clipboard.clear_sec, translated)
            )

    async def _clipboard_read(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--no-newline",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("wl-paste failed: %s", exc)
            return ""
        if proc.returncode != 0:
            return ""
        return out.decode(errors="replace").strip()

    async def _clipboard_write(self, text: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-copy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(input=text.encode()), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("wl-copy failed: %s", exc)

    async def _clipboard_types(self) -> list[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--list-types",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("wl-paste --list-types failed: %s", exc)
            return []
        if proc.returncode != 0:
            return []
        return out.decode(errors="replace").splitlines()

    async def _clipboard_has_image(self) -> bool:
        types = await self._clipboard_types()
        return any(t.startswith("image/") for t in types)

    async def _clipboard_ocr(self) -> str:
        """OCR the clipboard image with tesseract and return the text.

        The image is read fully into memory first (a clipboard image is small)
        and then handed to tesseract on stdin. We cannot pipe one subprocess's
        stdout directly into another's stdin with asyncio subprocesses (a
        StreamReader has no fileno), so we materialize the bytes in between.
        """
        types = await self._clipboard_types()
        image_type = next((t for t in types if t.startswith("image/")), "image/png")
        try:
            paste = await asyncio.create_subprocess_exec(
                "wl-paste", "--type", image_type,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            img, _ = await asyncio.wait_for(paste.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("wl-paste (image) failed: %s", exc)
            return ""
        if paste.returncode != 0 or not img:
            log.info("clipboard translate: no image data")
            return ""
        try:
            ocr = await asyncio.create_subprocess_exec(
                "tesseract", "stdin", "stdout",
                "-l", self.cfg.clipboard.ocr_lang,
                "--psm", str(self.cfg.clipboard.ocr_psm),
                "--tessdata-dir", self.cfg.clipboard.tessdata_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(ocr.communicate(input=img), timeout=30)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("tesseract failed: %s", exc)
            return ""
        if err:
            log.warning("tesseract: %s", err.decode(errors="replace").strip())
        return out.decode(errors="replace").strip()

    async def _clipboard_clear_after(self, delay_s: int, expected: str) -> None:
        await asyncio.sleep(delay_s)
        current = await self._clipboard_read()
        if current and current == expected:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wl-copy", "--clear",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10)
                log.info("clipboard cleared after %ds", delay_s)
            except (OSError, asyncio.TimeoutError) as exc:
                log.error("clipboard clear failed: %s", exc)
        else:
            log.info("clipboard clear skipped (contents changed)")

    async def _play_to_virtual_mic(self, wav: bytes) -> None:
        # Ensure the virtual mic (null sink + monitor) exists, then play into it.
        self._ensure_virtual_mic()
        await self._play_to_device(wav, self.cfg.outgoing.virtual_mic)

    def _ensure_virtual_mic(self) -> None:
        name = self.cfg.outgoing.virtual_mic
        try:
            out = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return
        if name not in out:
            subprocess.run(
                ["pactl", "load-module", "module-null-sink",
                 f"sink_name={name}", f"sink_properties=device.description=OLT-Virtual-Mic"],
                capture_output=True, timeout=5,
            )
            log.info("created virtual mic sink %s", name)

    async def incoming(self) -> None:
        if not self.cfg.incoming.enabled:
            return
        log.info("incoming started (monitor %s)", self.cfg.incoming.source_device)
        while True:
            try:
                await self._incoming_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("incoming loop error: %s", exc)
                # The streaming ASR server can SIGABRT (ggml-cpu.c:1270
                # `ne3 == ne13` in the cache-aware encoder) while decoding a
                # live stream. Its stdout/stderr watcher only reads lines, so
                # the crash is invisible to `start()`. Restart the server and
                # the stream here instead of hot-looping reconnect errors.
                await self.asr.restart()
                await asyncio.sleep(2)

    async def _incoming_once(self) -> None:
        if not self._activation_ok():
            await asyncio.sleep(0.5)
            return
        cap = audio.monitor_capture(self.cfg)
        eou_ms = (
            self.cfg.incoming.multimedia_endpointing_ms
            if self.cfg.incoming.multimedia
            else self.cfg.incoming.endpointing_ms
        )
        stream = await self.asr.connect_stream(
            self.cfg.incoming.source_language,
            endpointing_ms=eou_ms,
            speech_contexts=self._speech_contexts(),
        )
        await cap.start()
        # One capture + one ASR stream stay open across consecutive utterances;
        # we never tear them down between finals. This is the fix for audio
        # being dropped while a final was being translated (the old code awaited
        # NMT inline and then re-opened capture, losing whatever arrived during
        # the translate). Finals are now handed to background workers.
        gen = self._incoming_gen
        card_id = self._next_card("in")
        self._incoming_card_id = card_id
        partial = ""
        t_asr_final: float | None = None
        t_capture_start = time.monotonic()
        # First-play diagnostics: count deltas, time the first one, and tally
        # every other event type so a silent failure is attributable.
        delta_count = 0
        stream_delta_count = 0
        first_delta_at: float | None = None
        first_card_at: float | None = None
        other_events: dict[str, int] = {}
        # Absolute positions count only PCM sent on this stream, including
        # preroll and synthesized trailing silence, never wire-silent gaps.
        captured_bytes = 0
        lookback_ms = 20000
        self._incoming_ring = bytearray()
        ring_cap = int(audio.RATE * 2 * lookback_ms / 1000)
        # Absolute stream byte positions: `_incoming_ring` is trimmed from the
        # front as it overflows, so offsets recorded as raw indices would shift.
        # `ring_base` = bytes already trimmed off the front; a stored utterance
        # offset is absolute (ring_base + index) and converted back on snapshot.
        ring_base = 0
        # Report delayed ASR once per onset without discarding accepted audio.
        onset_mono: float | None = None
        onset_delta_count = 0
        onset_warned = False
        # Silence gate is only meaningful in multimedia mode (continuous media
        # audio). In live-call mode audio flows unfiltered.
        gate = None
        if self.cfg.incoming.multimedia and self.cfg.incoming.gate_enabled:
            gate = SilenceGate(
                self.cfg.incoming.gate_open_rms,
                self.cfg.incoming.gate_close_rms,
                self.cfg.incoming.gate_open_ms,
                self.cfg.incoming.gate_close_ms,
                self.cfg.incoming.gate_preroll_ms,
                # Trailing silence after close keeps token-silence EOU firing
                # before the stream goes wire-silent.
                eou_ms,
            )
        try:
            async def pump():
                nonlocal captured_bytes, ring_base
                nonlocal onset_mono, onset_delta_count, onset_warned
                try:
                    while True:
                        chunk = await cap.read_chunk(self.cfg.chunk_ms)
                        if not chunk:
                            break
                        if gate is not None:
                            was_open = gate.open
                            chunk = gate.process(chunk, self.cfg.chunk_ms)
                            if not was_open and gate.open:
                                onset_mono = time.monotonic()
                                onset_delta_count = stream_delta_count
                                onset_warned = False
                                log.info(
                                    "gate opened (stream %.2fs, preroll+chunk %d bytes, monotonic %.6f)",
                                    captured_bytes / (audio.RATE * 2), len(chunk), onset_mono,
                                )
                            elif was_open and not gate.open:
                                onset_mono = None
                                log.info(
                                    "gate closed (stream %.2fs)",
                                    captured_bytes / (audio.RATE * 2),
                                )
                            if chunk is None:
                                # Idle: wire silence. This is what avoids the
                                # upstream zero-PCM wedge during long quiet runs.
                                continue
                        await stream.send_audio(chunk)
                        if self.cfg.asr.offline_enabled:
                            self._incoming_ring.extend(chunk)
                            if len(self._incoming_ring) > ring_cap:
                                excess = len(self._incoming_ring) - ring_cap
                                del self._incoming_ring[:excess]
                                ring_base += excess
                        captured_bytes += len(chunk)
                except (ConnectionResetError, OSError, asyncio.CancelledError):
                    # The socket can close when incoming is paused for TTS
                    # playback; the pump is done, not an error.
                    pass
                except Exception as exc:
                    log.error("incoming pump error: %s", exc)
                finally:
                    # EOF and send/read failures must unblock the event reader
                    # so incoming() can reconnect instead of waiting forever.
                    await stream.close()

            async def monitor_asr_delay():
                nonlocal onset_warned
                while True:
                    await asyncio.sleep(0.1)
                    if (
                        gate is not None
                        and gate.open
                        and onset_mono is not None
                        and not onset_warned
                        and stream_delta_count == onset_delta_count
                        and (time.monotonic() - onset_mono) > _ASR_DELAY_WARNING_S
                    ):
                        log.warning(
                            "ASR delayed: gate open %.0fms with no deltas; "
                            "keeping stream open to preserve speech",
                            (time.monotonic() - onset_mono) * 1000,
                        )
                        onset_warned = True

            pump_task = asyncio.create_task(pump())
            delay_task = (
                asyncio.create_task(monitor_asr_delay()) if gate is not None else None
            )
            try:
                async for ev in stream.events():
                    t = ev.get("type")
                    if t == "conversation.item.input_audio_transcription.delta":
                        delta_count += 1
                        stream_delta_count += 1
                        if first_delta_at is None:
                            first_delta_at = time.monotonic()
                        partial = (partial + ev.get("delta", "")).strip()
                        self.overlay_send(
                            {"cmd": "card", "id": card_id, "direction": "in",
                             "source": partial, "target": "", "state": "draft"}
                        )
                        if first_card_at is None:
                            first_card_at = time.monotonic()
                            log.info(
                                "incoming first draft %s: delta monotonic %.6f, sent %.6f",
                                card_id, first_delta_at, first_card_at,
                            )
                    elif t == "conversation.item.input_audio_transcription.completed":
                        final = ev.get("transcript", partial).strip()
                        t_asr_final = time.monotonic()
                        # Log every final (even empty) so a first-utterance
                        # empty `.completed` is visible instead of silently
                        # producing no card.
                        log.info(
                            "incoming completed (gen %d, %d chars, audio_processed %r, "
                            "deltas %d, first_delta %.3fs)",
                            gen, len(final), ev.get("audio_processed"),
                            delta_count, first_delta_at - t_capture_start if first_delta_at is not None else -1.0,
                        )
                        utterance_audio = b""
                        if self.cfg.asr.offline_enabled:
                            # NeMo CacheStreamRunner::step reports supplied
                            # audio_end, including buffered future utterances.
                            # The realtime protocol exposes no complete start/
                            # end bounds. Neither that high-water mark nor gate
                            # transitions safely identify a particular final.
                            utterance_audio = self._snapshot_utterance_audio(
                                None, None, ring_base,
                            )
                            if final and not utterance_audio:
                                log.info("incoming refinement skipped: no proven complete audio bounds")
                        if final:
                            # Translate off the event loop so the next
                            # utterance's deltas are consumed immediately.
                            asyncio.create_task(
                                self._finalize_incoming(
                                    card_id, final, gen,
                                    t_capture_start, t_asr_final,
                                    utterance_audio,
                                    first_delta_at, first_card_at,
                                )
                            )
                        # Start a fresh card for the next utterance.
                        card_id = self._next_card("in")
                        self._incoming_card_id = card_id
                        partial = ""
                        t_capture_start = time.monotonic()
                        delta_count = 0
                        first_delta_at = None
                        first_card_at = None
                        audio.prune_debug_dir(self.cfg)
                    else:
                        other_events[t or "<no-type>"] = other_events.get(t or "<no-type>", 0) + 1
            finally:
                log.info(
                    "incoming stream ended (gen %d, deltas %d, other events %s)",
                    gen, delta_count, other_events or "{}",
                )
                if delay_task is not None:
                    delay_task.cancel()
                    await asyncio.gather(delay_task, return_exceptions=True)
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            await cap.stop()
            await stream.close()
            if self._incoming_card_id == card_id:
                self._incoming_card_id = None

    def _snapshot_utterance_audio(
        self,
        utt_start: int | None,
        utt_end: int | None,
        ring_base: int = 0,
    ) -> bytes:
        """Snapshot proven absolute sent-PCM byte bounds, never partial coverage."""
        ring = getattr(self, "_incoming_ring", None)
        if not ring:
            return b""
        if (type(utt_start) is not int or type(utt_end) is not int
                or utt_start < ring_base or utt_start < 0
                or utt_end <= utt_start or utt_end > ring_base + len(ring)
                or utt_start % 2 or utt_end % 2):
            return b""
        raw = bytes(ring[utt_start - ring_base:utt_end - ring_base])
        # Skip degenerate snapshots: an empty or sub-300ms utterance is not
        # worth an offline round-trip (and an empty WAV 500s the server).
        if len(raw) < int(0.3 * audio.RATE * 2):
            return b""
        return raw

    async def _finalize_incoming(
        self,
        card_id: str,
        final: str,
        gen: int,
        t_capture_start: float,
        t_asr_final: float,
        utterance_audio: bytes,
        first_delta_at: float | None = None,
        first_card_at: float | None = None,
    ) -> None:
        """Translate a final utterance and update its card, dropping if stale."""
        src = self.cfg.incoming.source_language[:2]
        tgt = self.cfg.incoming.target
        t_nmt0 = time.monotonic()
        try:
            translated = await self.nmt.translate(final, src, tgt)
        except Exception as exc:
            translated = f"[translation failed: {exc}]"
        nmt_ms = (time.monotonic() - t_nmt0) * 1000
        if gen != self._incoming_gen:
            log.info("dropping stale incoming final (gen %d != %d)", gen, self._incoming_gen)
            return
        asr_ms = round((t_asr_final - t_capture_start) * 1000, 1)
        self.overlay_send(
            {"cmd": "card", "id": card_id, "direction": "in",
             "source": final, "target": translated, "state": "incoming"}
        )
        if first_card_at is None:
            first_card_at = time.monotonic()
        logging.log_event({
            "kind": "incoming",
            "direction": f"{src}→{tgt}",
            "card": card_id,
            "src_len": len(final),
            "tgt_len": len(translated),
            "asr_ms": asr_ms,
            "capture_start_s": t_capture_start,
            "first_delta_receipt_s": first_delta_at,
            "first_card_sent_s": first_card_at,
            "nmt_ms": round(nmt_ms, 1),
            "multimedia": self.cfg.incoming.multimedia,
            **({"source": final, "target": translated} if self.cfg.debug_capture else {}),
        })
        log.info("incoming translated (%s→%s): %d chars → %d chars",
                 src, tgt, len(final), len(translated))
        # Two-tier refinement: re-transcribe the utterance with the offline
        # model and replace the card in place if it yields better text.
        if self.cfg.asr.offline_enabled and utterance_audio:
            await self._refine_card(card_id, final, translated, gen, src, tgt,
                                    utterance_audio)

    async def _refine_card(
        self,
        card_id: str,
        final: str,
        translated: str,
        gen: int,
        src: str,
        tgt: str,
        utterance_audio: bytes,
    ) -> None:
        """Re-transcribe with the offline model and update the card in place."""
        try:
            refined = await self.asr.transcribe_offline(
                utterance_audio, self.cfg.incoming.source_language
            )
        except Exception as exc:
            log.error("offline refinement failed: %s", exc)
            return
        cleaned = refined.strip()
        if not cleaned or gen != self._incoming_gen:
            return
        # Skip when the offline model agrees with the streaming draft — there is
        # nothing to change.
        if cleaned == final:
            return
        try:
            refined_translated = await self.nmt.translate(cleaned, src, tgt)
        except Exception as exc:
            log.error("offline refinement translation failed: %s", exc)
            return
        if gen != self._incoming_gen or not refined_translated:
            return
        logging.log_event({
            "kind": "incoming_refine",
            "direction": f"{src}→{tgt}",
            "card": card_id,
            "src_len": len(cleaned),
            "tgt_len": len(refined_translated),
            **({"source": cleaned, "target": refined_translated}
               if self.cfg.debug_capture else {}),
        })
        log.info("incoming refined (%s→%s): %d chars → %d chars", src, tgt,
                 len(cleaned), len(refined_translated))
        self.overlay_send(
            {"cmd": "card", "id": card_id, "direction": "in",
             "source": cleaned, "target": refined_translated, "state": "incoming"}
        )

    # -- control server ----------------------------------------------------

    def start_control_server(self) -> None:
        controller = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/") == "/status":
                    self._reply(HTTPStatus.OK, controller.status())
                else:
                    self._reply(HTTPStatus.OK, {"status": "ok", "name": "olt-controller"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    msg = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                action = msg.get("action")
                try:
                    asyncio.run_coroutine_threadsafe(
                        controller.dispatch(action, msg), controller.loop
                    )
                except Exception as exc:
                    log.error("dispatch failed: %s", exc)
                self._reply(HTTPStatus.OK, {"ok": True})

            def _reply(self, status, obj):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # The client (curl --max-time, panel poll, hotkey) may have
                    # gone away mid-reply; nothing to do.
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.cfg.control_port), Handler)
        log.info("control server on 127.0.0.1:%s", self.cfg.control_port)
        import threading

        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    async def dispatch(self, action: str, msg: dict) -> None:
        if action == "ptt_start":
            source = msg.get("source_lang", self.cfg.outgoing.language)
            target = msg.get("target", self.cfg.outgoing.target)
            if self.outgoing_task and not self.outgoing_task.done():
                self.outgoing_task.cancel()
            self._ptt_stop_event = asyncio.Event()
            self.outgoing_task = asyncio.create_task(
                self.outgoing(source, target, self._ptt_stop_event)
            )
        elif action == "ptt_stop":
            stop_ev = getattr(self, "_ptt_stop_event", None)
            if stop_ev is not None:
                stop_ev.set()
        elif action == "speak":
            await self.speak_ready()
        elif action == "clear":
            self.overlay_send({"cmd": "clear_all"})
            self._incoming_card_id = None
            # Bump the generation by restarting the stream, so the live stream
            # never holds a stale gen (see the gen-orphaning bug: clearing used
            # to just += 1, leaving the long-lived stream on the old gen, which
            # made every subsequent final get dropped as "stale").
            await self._restart_incoming()
        elif action == "clear_logs":
            self.clear_logs()
            await self._restart_incoming()
        elif action == "clipboard_translate":
            await self.clipboard_translate()
        elif action == "pause_incoming":
            if self.incoming_task and not self.incoming_task.done():
                self.incoming_task.cancel()
                self.incoming_task = None
                self._incoming_card_id = None
                self._incoming_gen += 1
                log.info("incoming paused")
            else:
                self.incoming_task = asyncio.create_task(self.incoming())
                log.info("incoming resumed")
        elif action == "set":
            await self._apply_settings(msg.get("settings", {}))
        else:
            log.warning("unknown action: %s", action)

    async def _apply_settings(self, settings: dict) -> None:
        async with self._settings_lock:
            await self._apply_settings_locked(settings)

    async def _apply_settings_locked(self, settings: dict) -> None:
        if "incoming_enabled" in settings:
            want = bool(settings["incoming_enabled"])
            have = self.incoming_task is not None and not self.incoming_task.done()
            self.cfg.incoming.enabled = want
            if want and not have:
                self.incoming_task = asyncio.create_task(self.incoming())
                log.info("incoming enabled via settings")
            elif not want and have:
                self.incoming_task.cancel()
                self.incoming_task = None
                self._incoming_card_id = None
                self._incoming_gen += 1
                log.info("incoming disabled via settings")
        if "direction" in settings:
            direction = settings["direction"]
            if direction == "en-es":
                self.cfg.outgoing.language = "en-US"
                self.cfg.outgoing.target = "es"
            elif direction == "es-en":
                self.cfg.outgoing.language = "es-ES"
                self.cfg.outgoing.target = "en"
            log.info("direction set to %s", direction)
        if "auto_speak" in settings:
            self.cfg.outgoing.auto_speak_after_ms = (
                0 if not settings["auto_speak"] else 3000
            )
            log.info("auto_speak set to %s", bool(settings["auto_speak"]))
        if "incoming_direction" in settings:
            direction = settings["incoming_direction"]
            if direction == "es-en":
                self.cfg.incoming.source_language = "es-ES"
                self.cfg.incoming.target = "en"
            elif direction == "en-es":
                self.cfg.incoming.source_language = "en-US"
                self.cfg.incoming.target = "es"
            await self._restart_incoming()
            log.info("incoming direction set to %s", direction)
        if "multimedia" in settings:
            self.cfg.incoming.multimedia = bool(settings["multimedia"])
            # The EOU window is baked into the live ASR stream at connect
            # time, so a toggle needs a fresh stream to take effect.
            await self._restart_incoming()
            log.info("multimedia mode set to %s", self.cfg.incoming.multimedia)
        if "gate" in settings:
            gate = settings["gate"]
            if isinstance(gate, dict):
                if "enabled" in gate:
                    self.cfg.incoming.gate_enabled = bool(gate["enabled"])
                if "open_rms" in gate:
                    self.cfg.incoming.gate_open_rms = float(gate["open_rms"])
                if "close_rms" in gate:
                    self.cfg.incoming.gate_close_rms = float(gate["close_rms"])
                if "open_ms" in gate:
                    self.cfg.incoming.gate_open_ms = int(gate["open_ms"])
                if "close_ms" in gate:
                    self.cfg.incoming.gate_close_ms = int(gate["close_ms"])
                if "preroll_ms" in gate:
                    self.cfg.incoming.gate_preroll_ms = int(gate["preroll_ms"])
            await self._restart_incoming()
            log.info(
                "silence gate updated: enabled=%s open_rms=%.0f close_rms=%.0f",
                self.cfg.incoming.gate_enabled,
                self.cfg.incoming.gate_open_rms,
                self.cfg.incoming.gate_close_rms,
            )
        if "history" in settings:
            self.cfg.overlay.history = bool(settings["history"])
            self.overlay_send({"cmd": "history", "enabled": self.cfg.overlay.history})
            log.info("overlay history set to %s", self.cfg.overlay.history)
        if "glossary" in settings:
            gl = settings["glossary"]
            if isinstance(gl, dict):
                if "enabled" in gl:
                    self.cfg.glossary.enabled = bool(gl["enabled"])
                if "phrases" in gl:
                    self.cfg.glossary.phrases = list(gl["phrases"])
                if "boost" in gl:
                    self.cfg.glossary.boost = float(gl["boost"])
            await self._restart_incoming()
            log.info("glossary updated: enabled=%s phrases=%d",
                     self.cfg.glossary.enabled, len(self.cfg.glossary.phrases))
        if "activation" in settings:
            act = settings["activation"]
            if isinstance(act, dict):
                if "enabled" in act:
                    self.cfg.activation.enabled = bool(act["enabled"])
                if "app_class" in act:
                    self.cfg.activation.app_class = str(act["app_class"])
                if "app_title" in act:
                    self.cfg.activation.app_title = str(act["app_title"])
            log.info("activation updated: enabled=%s class=%r title=%r",
                     self.cfg.activation.enabled,
                     self.cfg.activation.app_class,
                     self.cfg.activation.app_title)
        if "output_destination" in settings:
            dest = settings["output_destination"]
            if dest in ("virtual_mic", "speakers"):
                self.cfg.outgoing.output_destination = dest
                log.info("output destination set to %s", dest)
        if "debug_capture" in settings:
            self.cfg.debug_capture = bool(settings["debug_capture"])
            log.info("debug capture set to %s", self.cfg.debug_capture)
        if "preprocess_enable" in settings:
            self.cfg.preprocess_enable = bool(settings["preprocess_enable"])
            log.info("audio pre-processing set to %s", self.cfg.preprocess_enable)
        if "preamp_db" in settings:
            self.cfg.preamp_db = float(settings["preamp_db"])
            log.info("preamp gain set to %.1f dB", self.cfg.preamp_db)
        if "chunk_ms" in settings:
            self.cfg.chunk_ms = int(settings["chunk_ms"])
            log.info("audio chunk size set to %d ms", self.cfg.chunk_ms)
        if "keep_awake" in settings:
            self.cfg.keep_awake = bool(settings["keep_awake"])
            self._apply_keep_awake()
            log.info("keep-awake set to %s", self.cfg.keep_awake)
        if "two_tier" in settings:
            want = bool(settings["two_tier"])
            if want != self.cfg.asr.offline_enabled:
                self.cfg.asr.offline_enabled = want
                if want:
                    await self.asr.start_offline()
                else:
                    await self.asr.stop_offline()
                log.info("two-tier accuracy set to %s", want)
        if "clipboard_log_text" in settings:
            self.cfg.clipboard.log_text = bool(settings["clipboard_log_text"])
            log.info("clipboard text logging set to %s", self.cfg.clipboard.log_text)

    async def _restart_incoming(self) -> None:
        """Reconnect the incoming stream so stream-level settings take effect.

        Serialized on `_restart_lock`: the old stream must fully tear down
        (capture stop + WebSocket close) before a new one starts, otherwise
        the old pump's `send_audio` races the new stream's teardown and can
        abort the ASR server (the SIGABRT we saw while dragging the gate
        slider). The lock also coalesces rapid-fire setting changes.
        """
        async with self._restart_lock:
            if self.incoming_task is not None and not self.incoming_task.done():
                self.incoming_task.cancel()
                self._incoming_gen += 1
                try:
                    await self.incoming_task
                except asyncio.CancelledError:
                    pass
            if self.cfg.incoming.enabled:
                self.incoming_task = asyncio.create_task(self.incoming())

    def _apply_keep_awake(self) -> None:
        """Toggle the Omarchy stay-awake flag that suppresses idle/lock."""
        try:
            subprocess.run(
                ["omarchy-toggle-idle",
                 "stay-awake" if self.cfg.keep_awake else "allow-idle"],
                capture_output=True, timeout=5,
            )
        except Exception as exc:
            log.error("keep-awake toggle failed: %s", exc)

    def clear_logs(self) -> None:
        """Delete all log files and translation history for this plugin.

        Removes olt.log*, events.jsonl, debug capture WAVs, and clears the
        on-screen overlay. The controller keeps running; new events start fresh.
        """
        log_dir = Path(self.cfg.log_dir)
        removed = 0
        try:
            patterns = ("olt.log", "olt.log.*", "events.jsonl")
            paths = [p for pat in patterns for p in log_dir.glob(pat)]
            if self.cfg.debug_capture:
                paths += [p for p in Path(self.cfg.debug_dir).glob("*.wav")]
            for path in paths:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("could not remove %s: %s", path, exc)
        except Exception as exc:
            log.error("clear_logs failed: %s", exc)
        self.overlay_send({"cmd": "clear_all"})
        self._incoming_card_id = None
        # The caller restarts the incoming stream after this (so the live
        # stream's generation stays in sync); see the dispatch of clear_logs.
        log.info("cleared %d log/history file(s)", removed)

    def status(self) -> dict:
        incoming_running = self.incoming_task is not None and not self.incoming_task.done()
        return {
            "status": "ok",
            "name": "olt-controller",
            "direction": "en-es" if self.cfg.outgoing.language.startswith("en") else "es-en",
            "auto_speak": self.cfg.outgoing.auto_speak_after_ms > 0,
            "output_destination": self.cfg.outgoing.output_destination,
            "incoming_enabled": incoming_running,
            "incoming_direction": "es-en" if self.cfg.incoming.source_language.startswith("es") else "en-es",
            "multimedia": self.cfg.incoming.multimedia,
            "history": self.cfg.overlay.history,
            "gate": {
                "enabled": self.cfg.incoming.gate_enabled,
                "open_rms": self.cfg.incoming.gate_open_rms,
                "close_rms": self.cfg.incoming.gate_close_rms,
                "open_ms": self.cfg.incoming.gate_open_ms,
                "close_ms": self.cfg.incoming.gate_close_ms,
                "preroll_ms": self.cfg.incoming.gate_preroll_ms,
            },
            "glossary": {
                "enabled": self.cfg.glossary.enabled,
                "phrases": self.cfg.glossary.phrases,
                "boost": self.cfg.glossary.boost,
            },
            "activation": {
                "enabled": self.cfg.activation.enabled,
                "app_class": self.cfg.activation.app_class,
                "app_title": self.cfg.activation.app_title,
            },
            "debug_capture": self.cfg.debug_capture,
            "debug_dir": self.cfg.debug_dir,
            "debug_keep": self.cfg.debug_keep,
            "preprocess_enable": self.cfg.preprocess_enable,
            "highpass_hz": self.cfg.highpass_hz,
            "preamp_db": self.cfg.preamp_db,
            "chunk_ms": self.cfg.chunk_ms,
            "keep_awake": self.cfg.keep_awake,
            "two_tier": self.cfg.asr.offline_enabled,
            "clipboard_clear_sec": self.cfg.clipboard.clear_sec,
            "clipboard_ocr": self.cfg.clipboard.ocr_enabled,
            "clipboard_log_text": self.cfg.clipboard.log_text,
        }

    # -- lifecycle ---------------------------------------------------------

    async def _events_pruner(self) -> None:
        """Periodically drop events and log files older than the retention window."""
        while True:
            await asyncio.sleep(3600)
            try:
                removed = logging.prune_events(24 * 3600)
                if removed:
                    log.info("pruned %d event(s) older than 24h", removed)
                removed_logs = logging.prune_log_files(24 * 3600)
                if removed_logs:
                    log.info("pruned %d log file(s) older than 24h", removed_logs)
            except Exception as exc:  # never kill the pruner
                log.error("log pruning failed: %s", exc)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        audio.prune_debug_dir_startup(self.cfg)
        try:
            removed = logging.prune_events(24 * 3600)
            if removed:
                log.info("pruned %d event(s) older than 24h at startup", removed)
            removed_logs = logging.prune_log_files(24 * 3600)
            if removed_logs:
                log.info("pruned %d log file(s) older than 24h at startup", removed_logs)
        except Exception as exc:
            log.error("startup log pruning failed: %s", exc)
        await self.asr.start()
        if self.cfg.asr.offline_enabled:
            await self.asr.start_offline()
        await self.start_overlay()
        self.start_control_server()
        self._apply_keep_awake()
        # Warm both ASR engines before the first real clip so the first
        # translation isn't slow (Vulkan pipelines compile lazily under
        # --no-warmup). Non-fatal: failures are logged and skipped.
        await self.asr.warm_stream(self.cfg.incoming.source_language)
        if self.cfg.asr.offline_enabled:
            await self.asr.warm_offline()
        if self.cfg.incoming.enabled:
            self.incoming_task = asyncio.create_task(self.incoming())
        asyncio.create_task(self._events_pruner())
        log.info("controller running; press Ctrl-C to stop")
        try:
            await asyncio.Event().wait()
        finally:
            await self.asr.stop()


def main() -> None:
    cfg = load()
    logging.setup(cfg.log_dir, cfg.log_level)
    logging.install_hooks()
    controller = Controller(cfg)
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
