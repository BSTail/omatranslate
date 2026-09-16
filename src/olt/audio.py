"""Audio capture via PipeWire/PulseAudio using `parec`.

`parec` is used instead of a Python audio library so the plugin has no native
Python audio dependency. We spawn it and read raw PCM16 from stdout.

- outgoing: capture the default microphone while push-to-talk is held.
- incoming: capture a monitor source (the call app's output).

Debug capture (testing aid): when enabled, each capture session is written to
a timestamped WAV file under `debug_dir`. Files are closed on stop and pruned
to `debug_keep` newest files. The `wave` module only supports read/write modes,
so we never append; every session gets its own file.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
import wave
from pathlib import Path

from . import logging
from .config import Config
from .preprocess import Preprocessor

log = logging.get()

RATE = 16000
FORMAT = "s16le"
CHANNELS = 1
# Chunks below this RMS are considered silence (digital silence for a clean
# pipe is 0; real-world monitor noise is far lower than normal speech).
SILENCE_RMS = 100.0


class DebugPcmWriter:
    """Best-effort bounded diagnostic writer for PCM already sent to ASR."""

    def __init__(self, debug_dir: Path, tag: str):
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = debug_dir / f"{tag}-{stamp}.wav"
        self.bytes_written = 0
        self._wav: wave.Wave_write | None = wave.open(str(self.path), "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(2)
        self._wav.setframerate(RATE)

    def write(self, pcm: bytes) -> None:
        if self._wav is None or not pcm:
            return
        try:
            self._wav.writeframes(pcm)
            self.bytes_written += len(pcm)
        except (OSError, wave.Error) as exc:
            log.warning("sent-audio capture write failed for %s: %s", self.path, exc)

    def close(self) -> tuple[Path, int]:
        wav = self._wav
        self._wav = None
        if wav is not None:
            try:
                wav.close()
            except OSError as exc:
                log.warning("sent-audio capture close failed for %s: %s", self.path, exc)
        if self.bytes_written == 0:
            try:
                self.path.unlink()
            except OSError:
                pass
        return self.path, self.bytes_written


def prune_debug_captures(debug_dir: Path, keep: int) -> None:
    """Keep only the newest `keep` capture files in the debug directory.

    Also removes zero-byte captures left behind by a crashed/upgraded capture
    (e.g. a `parec` that never delivered audio), which would otherwise linger
    forever and look like valid recordings during analysis.
    """
    try:
        files = sorted(
            (p for p in debug_dir.glob("*.wav") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    try:
        for empty in debug_dir.glob("*.wav"):
            if empty.is_file() and empty.stat().st_size == 0:
                empty.unlink()
    except OSError:
        pass


def prune_debug_silent(debug_dir: Path) -> None:
    """Delete debug WAVs whose audio is entirely below the silence threshold.

    Retention (`debug_keep`) alone is not enough: every incoming final closes
    its capture and opens a new one, so an hour of idle monitor time produces
    dozens of near-silent files that crowd out the few captures that actually
    contain speech. This runs at service startup so a long idle stretch can
    never evict the recordings we care about.
    """
    try:
        candidates = [p for p in debug_dir.glob("*.wav") if p.is_file()]
    except OSError:
        return
    for path in candidates:
        try:
            with wave.open(str(path), "rb") as r:
                n = r.getnframes()
                if n == 0:
                    r.close()
                    path.unlink()
                    continue
                raw = r.readframes(n)
        except (OSError, wave.Error):
            continue
        samples = struct.unpack(f"<{n}h", raw)
        win = RATE // 2
        loud = False
        for i in range(0, n - win + 1, win):
            seg = samples[i:i + win]
            rms = math.sqrt(sum(v * v for v in seg) / win)
            if rms > SILENCE_RMS:
                loud = True
                break
        if not loud:
            try:
                path.unlink()
            except OSError:
                pass


class Capture:
    def __init__(
        self,
        device: str,
        debug_dir: Path | None = None,
        tag: str = "capture",
        preprocess: Preprocessor | None = None,
        roll_sec: int = 0,
        silence_sec: int = 0,
    ):
        self.device = device
        self.debug_dir = debug_dir
        self.tag = tag
        self.preprocess = preprocess
        self.roll_sec = max(0, roll_sec)
        self.silence_sec = max(0, silence_sec)
        self.proc: asyncio.subprocess.Process | None = None
        self._wav: wave.Wave_write | None = None
        self._wav_path: Path | None = None
        self._wav_frames: int = 0
        self._t_open: float = 0.0

    async def start(self) -> None:
        # `parec` (libpulse 17 on PipeWire) resolves @DEFAULT_MONITOR@ and
        # @DEFAULT_SOURCE@ correctly and reads the monitor mix at full level.
        # `--latency-msec 10` shrinks the Pulse buffer (fragsize 320 bytes =
        # 20 ms) so capture latency stays low; the default fragsize (64000
        # bytes = 2 s) adds ~2 s of buffered latency to every stream.
        cmd = [
            "parec",
            "--device",
            self.device,
            "--raw",
            "--rate",
            str(RATE),
            "--format",
            FORMAT,
            "--channels",
            str(CHANNELS),
            "--latency-msec",
            "10",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if self.debug_dir is not None:
            self._open_wav()
        log.info("capture started on %s", self.device)

    def _open_wav(self) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._wav_path = self.debug_dir / f"{self.tag}-{stamp}.wav"
        self._wav = wave.open(str(self._wav_path), "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(2)
        self._wav.setframerate(RATE)
        self._wav_frames = 0
        self._t_open = time.monotonic()
        log.info("capture dump enabled: %s", self._wav_path)

    def _close_wav(self, trim: bool) -> None:
        wav = self._wav
        self._wav = None
        path = self._wav_path
        self._wav_path = None
        if wav is None:
            return
        # Flush the final partial frame before reading back for trimming.
        try:
            wav.close()
        except OSError:
            return
        if path is None:
            return
        if trim and self.silence_sec > 0:
            self._trim_silence(path)
        # A capture with no retained audio is useless; drop it.
        if self._wav_frames == 0:
            try:
                path.unlink()
            except OSError:
                pass

    def _trim_silence(self, path: Path) -> None:
        """Drop any retained audio that is below the silence threshold.

        Computes RMS per half-second window, finds the first and last window
        with speech-level energy, keeps `silence_sec` of context on either side,
        and rewrites the file in place. If everything is silent the file is
        removed, since it carries nothing useful for waveform analysis.
        """
        try:
            with wave.open(str(path), "rb") as r:
                n = r.getnframes()
                if n == 0:
                    r.close()
                    path.unlink()
                    return
                raw = r.readframes(n)
        except (OSError, wave.Error):
            return
        samples = struct.unpack(f"<{n}h", raw)
        win = RATE // 2  # 0.5 s windows
        keep_silence = self.silence_sec * RATE
        first = last = -1
        for i in range(0, n - win + 1, win):
            seg = samples[i:i + win]
            rms = math.sqrt(sum(v * v for v in seg) / win)
            if rms > SILENCE_RMS:
                if first < 0:
                    first = i
                last = i
        if first < 0:
            try:
                path.unlink()
            except OSError:
                pass
            return
        start = max(0, first - keep_silence)
        end = min(n, last + win + keep_silence)
        trimmed = raw[start * 2:end * 2]
        if not trimmed:
            try:
                path.unlink()
            except OSError:
                pass
            return
        try:
            with wave.open(str(path), "wb") as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(trimmed)
        except OSError:
            pass

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self._close_wav(trim=True)
        log.info("capture stopped on %s", self.device)

    async def read_chunk(self, ms: int = 160) -> bytes:
        """Read `ms` milliseconds of PCM16 (mono 16 kHz)."""
        assert self.proc is not None and self.proc.stdout is not None
        nbytes = int(RATE * 2 * ms / 1000)
        try:
            chunk = await self.proc.stdout.readexactly(nbytes)
        except asyncio.IncompleteReadError as exc:
            chunk = exc.partial
        if self.preprocess is not None:
            chunk = self.preprocess.process(chunk)
        if self._wav is not None and chunk:
            self._wav.writeframes(chunk)
            self._wav_frames += len(chunk) // 2
            # Roll the file once it exceeds the configured duration so idle
            # monitor sessions never grow without bound.
            if (
                self.roll_sec > 0
                and time.monotonic() - self._t_open >= self.roll_sec
            ):
                self._close_wav(trim=True)
                self._open_wav()
        return chunk


def mic_capture(cfg: Config) -> Capture:
    debug_dir = Path(cfg.debug_dir) if cfg.debug_capture else None
    pp = Preprocessor(cfg.preprocess_enable, cfg.highpass_hz, cfg.preamp_db)
    return Capture(
        "@DEFAULT_SOURCE@",
        debug_dir,
        tag="out",
        preprocess=pp,
        roll_sec=cfg.debug_roll_sec,
        silence_sec=cfg.debug_silence_sec,
    )


def monitor_capture(cfg: Config) -> Capture:
    debug_dir = Path(cfg.debug_dir) if cfg.debug_capture else None
    pp = Preprocessor(cfg.preprocess_enable, cfg.highpass_hz, cfg.preamp_db)
    return Capture(
        cfg.incoming.source_device,
        debug_dir,
        tag="in",
        preprocess=pp,
        roll_sec=cfg.debug_roll_sec,
        silence_sec=cfg.debug_silence_sec,
    )


def prune_debug_dir(cfg: Config) -> None:
    if cfg.debug_capture:
        prune_debug_captures(Path(cfg.debug_dir), cfg.debug_keep)
        prune_debug_captures(Path(cfg.debug_dir) / "sent", cfg.debug_keep)


def prune_debug_dir_startup(cfg: Config) -> None:
    """Startup cleanup: drop silent captures before retention runs.

    Retention counts files, not content, so a long idle stretch can evict the
    few speech-bearing captures. Removing all-silent files first makes the
    `debug_keep` budget go to captures that actually contain audio.
    """
    if cfg.debug_capture:
        prune_debug_silent(Path(cfg.debug_dir))
        prune_debug_captures(Path(cfg.debug_dir), cfg.debug_keep)
        prune_debug_captures(Path(cfg.debug_dir) / "sent", cfg.debug_keep)
