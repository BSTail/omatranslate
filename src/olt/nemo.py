"""NeMo-Speech.cpp ASR client.

Spawns `nemo-speech serve` as a subprocess and talks to it over HTTP and the
realtime transcription WebSocket. The WebSocket client is hand-rolled (RFC
6455, client frames only) so the plugin has no third-party Python dependency.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import urllib.request
import wave
from io import BytesIO
from pathlib import Path

from . import logging
from .config import ASRConfig, PathsConfig

log = logging.get()

_STREAM_WARMUP_CHUNK_MS = 160
_STREAM_WARMUP_TONE_S = 1.6
_STREAM_WARMUP_TRAILING_S = 1.6
_STREAM_WARMUP_IO_TIMEOUT_S = 5.0
_STREAM_WARMUP_AUDIO_TIMEOUT_S = 10.0
_STREAM_WARMUP_SESSION_TIMEOUT_S = 5.0
_STREAM_WARMUP_PROOF_TIMEOUT_S = 15.0
_STREAM_WARMUP_CLEAR_TIMEOUT_S = 2.0


class NemoASR:
    def __init__(self, paths: PathsConfig, asr_cfg: ASRConfig):
        self.paths = paths
        self.cfg = asr_cfg
        self.proc: asyncio.subprocess.Process | None = None
        self._ready = asyncio.Event()
        # Second (offline, higher-accuracy) model served on a distinct port.
        self.offline_proc: asyncio.subprocess.Process | None = None
        self._offline_ready = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    def _command(self) -> list[str]:
        cmd = [
            self.paths.nemo_speech,
            "serve",
            "--asr-model",
            self.cfg.model,
            "--device",
            self.cfg.device,
            "--host",
            self.cfg.host,
            "--port",
            str(self.cfg.port),
            "--no-ui",
            "--no-warmup",
            # Incoming translation listens to a continuous monitor stream and
            # never sends input_audio_buffer.commit, so end-of-utterance
            # detection must be on for the server to emit `.completed` finals
            # on trailing silence (otherwise no final ever arrives).
            "--asr.endpointing.enable=true",
        ]
        return cmd

    async def start(self, attempts: int = 3) -> None:
        """Start the server, retrying on the known intermittent warmup crash.

        The NeMo Vulkan backend can abort during warmup with
        `GGML_ASSERT(ne3 == ne13) failed` (SIGABRT). It is nondeterministic,
        so we simply retry a few times.
        """
        env = os.environ.copy()
        if self.cfg.preload_stdcxx:
            env["LD_PRELOAD"] = "/usr/lib/libstdc++.so.6"
        for attempt in range(1, attempts + 1):
            self._ready.clear()
            self.proc = await asyncio.create_subprocess_exec(
                *self._command(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            log.info("started nemo-speech serve (pid %s, attempt %d/%d)",
                     self.proc.pid, attempt, attempts)
            watcher = asyncio.create_task(self._watch_stderr())
            try:
                await self._wait_ready()
                return
            except RuntimeError as exc:
                await watcher
                log.warning("nemo-speech failed to start (attempt %d/%d): %s",
                            attempt, attempts, exc)
                if attempt == attempts:
                    raise
                await asyncio.sleep(2)

    async def _watch_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            log.info("nemo: %s", text)

    async def _wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is None or self.proc.returncode is not None:
                raise RuntimeError(
                    f"nemo-speech exited early with code {self.proc.returncode if self.proc else '?'}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.cfg.host, self.cfg.port), timeout=1.0
                )
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.5)
                continue
            writer.close()
            self._ready.set()
            log.info("nemo-speech is ready on %s:%s", self.cfg.host, self.cfg.port)
            return
        raise TimeoutError("nemo-speech did not become ready in time")

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
            log.info("stopped nemo-speech serve")
        await self.stop_offline()

    async def restart(self) -> None:
        """Stop and restart the streaming server after a crash.

        `start()` only retries when the process fails *during* startup; the
        stderr watcher merely logs lines, so a mid-stream SIGABRT is not
        detected until a client gets ECONNRESET. This re-runs the retry loop
        with a fresh process.
        """
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        log.info("restarting nemo-speech serve")
        await self.start()

    # -- offline (two-tier) server -----------------------------------------

    async def start_offline(self) -> None:
        """Start a second serve process with the offline accuracy model.

        Parakeet TDT cannot run `streaming_recognize` in this runtime (the
        runner throws "offline-only"), so it is served as a plain HTTP
        transcription endpoint on its own port and fed whole final utterances.
        """
        if self.offline_proc is not None and self.offline_proc.returncode is None:
            return
        env = os.environ.copy()
        if self.cfg.preload_stdcxx:
            env["LD_PRELOAD"] = "/usr/lib/libstdc++.so.6"
        self._offline_ready.clear()
        self.offline_proc = await asyncio.create_subprocess_exec(
            self.paths.nemo_speech,
            "serve",
            "--asr-model",
            self.cfg.offline_model,
            "--device",
            self.cfg.device,
            "--host",
            self.cfg.host,
            "--port",
            str(self.cfg.offline_port),
            "--no-ui",
            "--no-warmup",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        asyncio.create_task(self._watch_offline_stderr())
        log.info("started offline nemo-speech serve (pid %s, model %s)",
                 self.offline_proc.pid, self.cfg.offline_model)
        await self._wait_offline_ready()

    async def _watch_offline_stderr(self) -> None:
        assert self.offline_proc is not None and self.offline_proc.stderr is not None
        while True:
            line = await self.offline_proc.stderr.readline()
            if not line:
                break
            log.info("nemo-offline: %s", line.decode(errors="replace").rstrip())

    async def _wait_offline_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.offline_proc is None or self.offline_proc.returncode is not None:
                raise RuntimeError(
                    "offline nemo-speech exited early with code "
                    f"{self.offline_proc.returncode if self.offline_proc else '?'}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.cfg.host, self.cfg.offline_port),
                    timeout=1.0,
                )
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.5)
                continue
            writer.close()
            self._offline_ready.set()
            log.info("offline nemo-speech is ready on %s:%s",
                     self.cfg.host, self.cfg.offline_port)
            return
        raise TimeoutError("offline nemo-speech did not become ready in time")

    async def stop_offline(self) -> None:
        if self.offline_proc is not None and self.offline_proc.returncode is None:
            self.offline_proc.terminate()
            try:
                await asyncio.wait_for(self.offline_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.offline_proc.kill()
                await self.offline_proc.wait()
            log.info("stopped offline nemo-speech serve")

    async def transcribe_offline(self, pcm16: bytes, language: str) -> str:
        """Transcribe a whole utterance via the offline model's HTTP endpoint.

        Returns the transcript text, or "" on any failure (the streaming draft
        remains authoritative in that case).
        """
        if self.offline_proc is None or self.offline_proc.returncode is not None:
            raise RuntimeError("offline nemo-speech is not running")
        wav = _pcm16_to_wav(pcm16, 16000)
        boundary = "----oltboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="u.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            "text\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"http://{self.cfg.host}:{self.cfg.offline_port}/v1/audio/transcriptions",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=60)
        except Exception as exc:
            log.error("offline transcription failed: %s", exc)
            raise
        return resp.read().decode().strip()

    # -- WebSocket ---------------------------------------------------------

    async def connect_stream(
        self,
        language: str,
        sample_rate: int = 16000,
        endpointing_ms: int | None = None,
        speech_contexts: list[dict] | None = None,
    ) -> "ASRStream":
        reader, writer = await asyncio.open_connection(self.cfg.host, self.cfg.port)
        await _ws_handshake(writer, reader, self.cfg.host, self.cfg.port)
        stream = ASRStream(reader, writer)
        session = {"language": language, "sample_rate": sample_rate}
        if endpointing_ms is not None:
            session["endpointing_ms"] = endpointing_ms
        if speech_contexts:
            session["speech_contexts"] = speech_contexts
        await stream.send_json({"type": "session.update", "session": session})
        return stream

    async def warm_stream(self, language: str) -> None:
        """Compile the streaming model's Vulkan pipelines before first use.

        `serve` runs with `--no-warmup` (the built-in warmup path is the one
        that intermittently SIGABRTs), so exercise a throwaway stream and wait
        for an ASR event that proves the server processed it. The stream is
        discarded, so warmup text can never reach the production transcript.
        """
        stream = None
        event_task: asyncio.Task | None = None
        try:
            stream = await asyncio.wait_for(
                self.connect_stream(language, endpointing_ms=1000),
                timeout=_STREAM_WARMUP_IO_TIMEOUT_S,
            )
            session_ready = asyncio.Event()
            inference_seen = asyncio.Event()
            cleared = asyncio.Event()
            proof: dict[str, object] = {}

            async def consume_events() -> None:
                async for event in stream.events():
                    event_type = event.get("type")
                    if event_type == "session.updated":
                        session_ready.set()
                    elif event_type == "input_audio_buffer.cleared":
                        cleared.set()
                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        proof.update(type=event_type, audio_processed=event.get("audio_processed"))
                        inference_seen.set()
                    elif event_type == "conversation.item.input_audio_transcription.delta":
                        processed = event.get("audio_processed")
                        if isinstance(processed, (int, float)) and processed > 0:
                            proof.update(type=event_type, audio_processed=processed)
                            inference_seen.set()

            event_task = asyncio.create_task(
                consume_events(), name="olt-stream-warmup-events"
            )
            await asyncio.wait_for(
                session_ready.wait(), timeout=_STREAM_WARMUP_SESSION_TIMEOUT_S
            )

            # Pace production-sized frames so the throwaway session exercises
            # the same incremental path as incoming audio. Trailing silence can
            # trigger endpointing; commit guarantees a final drain if it does not.
            rate = 16000
            chunk_samples = int(rate * _STREAM_WARMUP_CHUNK_MS / 1000)
            tone_samples = int(rate * _STREAM_WARMUP_TONE_S)
            silence_samples = int(rate * _STREAM_WARMUP_TRAILING_S)
            import math as _math

            async def send_warmup_audio() -> None:
                deadline = asyncio.get_running_loop().time()
                for start in range(0, tone_samples, chunk_samples):
                    end = min(start + chunk_samples, tone_samples)
                    chunk = bytearray()
                    for i in range(start, end):
                        sample = int(
                            12000.0 * _math.sin(2.0 * _math.pi * 440.0 * i / rate)
                        )
                        chunk += struct.pack("<h", sample)
                    await stream.send_audio(bytes(chunk))
                    deadline += (end - start) / rate
                    await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
                silence = bytes(chunk_samples * 2)
                for start in range(0, silence_samples, chunk_samples):
                    count = min(chunk_samples, silence_samples - start)
                    await stream.send_audio(silence[:count * 2])
                    deadline += count / rate
                    await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))

            await asyncio.wait_for(
                send_warmup_audio(), timeout=_STREAM_WARMUP_AUDIO_TIMEOUT_S
            )
            await asyncio.wait_for(
                stream.commit(), timeout=_STREAM_WARMUP_IO_TIMEOUT_S
            )
            await asyncio.wait_for(
                inference_seen.wait(), timeout=_STREAM_WARMUP_PROOF_TIMEOUT_S
            )
            await asyncio.wait_for(
                stream.clear(), timeout=_STREAM_WARMUP_IO_TIMEOUT_S
            )
            try:
                await asyncio.wait_for(
                    cleared.wait(), timeout=_STREAM_WARMUP_CLEAR_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                log.warning("streaming ASR warm-up clear acknowledgement timed out")
            log.info(
                "streaming ASR warmed (event %s, audio_processed %r)",
                proof.get("type"), proof.get("audio_processed"),
            )
        except Exception as exc:
            log.warning("streaming ASR warm-up failed (non-fatal): %s", exc)
        finally:
            if event_task is not None:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            if stream is not None:
                try:
                    await asyncio.wait_for(
                        stream.close(), timeout=_STREAM_WARMUP_IO_TIMEOUT_S
                    )
                except Exception as exc:
                    log.warning("streaming ASR warm-up close failed: %s", exc)

    async def warm_offline(self) -> None:
        """Compile the offline model's Vulkan pipelines before first use.

        Same rationale as `warm_stream`: `--no-warmup` defers pipeline
        compilation to the first transcription, making the first refine ~2x
        slower. One tiny transcription forces it now.
        """
        try:
            if self.offline_proc is None or self.offline_proc.returncode is not None:
                return
            rate = 16000
            n = int(rate * 0.5)
            import math as _math

            tone = bytearray()
            for i in range(n):
                s = int(12000.0 * _math.sin(2.0 * _math.pi * 440.0 * i / rate))
                tone += struct.pack("<h", s)
            await self.transcribe_offline(bytes(tone), "es")
            log.info("offline ASR warmed (pipeline compiled)")
        except Exception as exc:
            log.warning("offline ASR warm-up failed (non-fatal): %s", exc)


class ASRStream:
    """One realtime transcription session over the WebSocket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._closed = False

    async def send_audio(self, pcm16: bytes) -> None:
        await self._send_frame(pcm16, opcode=2)

    async def send_json(self, obj) -> None:
        await self._send_frame(json.dumps(obj).encode(), opcode=1)

    async def commit(self) -> None:
        await self.send_json({"type": "input_audio_buffer.commit"})

    async def clear(self) -> None:
        await self.send_json({"type": "input_audio_buffer.clear"})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_frame(b"", opcode=8)
        except OSError:
            pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass

    async def events(self):
        """Yield parsed JSON text events until the socket closes."""
        while True:
            frame = await self._recv_frame()
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 1:  # text
                try:
                    yield json.loads(payload.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A desynced frame (e.g. capture teardown) can hand us a
                    # raw WebSocket header byte instead of a JSON text payload.
                    # Skip it rather than killing the incoming loop.
                    log.warning("dropping malformed ASR frame (%d bytes)", len(payload))
                    continue
            elif opcode == 8:  # close
                return

    async def _send_frame(self, payload: bytes, opcode: int) -> None:
        mask = os.urandom(4)
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.writer.write(bytes(header) + masked)
        await self.writer.drain()

    async def _recv_frame(self):
        try:
            hdr = await self.reader.readexactly(2)
        except (asyncio.IncompleteReadError, OSError):
            return None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        mask = await self.reader.readexactly(4) if masked else None
        data = await self.reader.readexactly(ln)
        if mask is not None:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return opcode, data


async def _ws_handshake(
    writer: asyncio.StreamWriter, reader: asyncio.StreamReader, host: str, port: int
) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /v1/realtime HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode())
    await writer.drain()
    # Read only up to the end of the HTTP headers. Using read(4096) here would
    # over-read and swallow the first WebSocket frame's bytes (the server sends
    # `session.created` immediately after the 101), desyncing the frame parser
    # and producing "malformed frame" warnings. readuntil() stops exactly at the
    # header terminator, leaving any frame bytes in the stream buffer.
    buf = await reader.readuntil(b"\r\n\r\n")
    status_line = buf.split(b"\r\n")[0].decode(errors="replace")
    if "101" not in status_line:
        raise RuntimeError(f"WebSocket handshake failed: {status_line}")


def _pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()
