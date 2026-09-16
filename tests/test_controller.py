import asyncio
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from olt.config import Config
from olt.controller import Controller, SilenceGate


def pcm(value, ms=160):
    return struct.pack('<h', value) * (16 * ms)


class GateTests(unittest.TestCase):
    def gate(self, preroll_ms=400):
        return SilenceGate(200, 100, 320, 160, preroll_ms, 320)

    def test_reopen_does_not_duplicate_chunk(self):
        gate = self.gate()
        gate.process(pcm(0), 160)
        gate.process(pcm(500), 160)
        self.assertEqual(gate.process(pcm(600), 160), pcm(0) + pcm(500) + pcm(600))
        self.assertTrue(gate.open)

    def test_disconnected_loud_burst_resets_qualification_not_preroll(self):
        gate = self.gate()
        for value in (0, 500, 0, 600):
            gate.process(pcm(value), 160)
            self.assertFalse(gate.open)
        self.assertEqual(
            gate.process(pcm(700), 160),
            pcm(500, 80) + pcm(0) + pcm(600) + pcm(700),
        )

    def test_soft_onset_is_preserved(self):
        gate = self.gate()
        self.assertEqual(gate.process(pcm(0), 160), pcm(0))
        self.assertEqual(gate.process(pcm(150), 160), pcm(0))
        self.assertIsNone(gate.process(pcm(500), 160))
        self.assertFalse(gate.open)
        self.assertEqual(
            gate.process(pcm(600), 160),
            pcm(0, 80) + pcm(150) + pcm(500) + pcm(600),
        )

    def test_quiet_history_evicts_old_audio_and_stays_bounded(self):
        gate = self.gate()
        gate.process(pcm(0), 160)
        gate.process(pcm(500), 160)
        for value in range(1, 21):
            self.assertIsNone(gate.process(pcm(value), 160))
            self.assertEqual(len(gate._preroll), 400 * 32)
            self.assertFalse(gate.open)
        self.assertIsNone(gate.process(pcm(600), 160))
        self.assertEqual(
            gate.process(pcm(700), 160),
            pcm(19, 80) + pcm(20) + pcm(600) + pcm(700),
        )

    def test_repeated_reopens_flush_only_current_history(self):
        gate = self.gate()
        for value in (500, 700, 900):
            with self.subTest(value=value):
                self.assertEqual(gate.process(pcm(1), 160), pcm(0))
                self.assertEqual(gate.process(pcm(value), 160), pcm(0))
                self.assertFalse(gate.open)
                self.assertEqual(
                    gate.process(pcm(value + 100), 160),
                    pcm(1) + pcm(value) + pcm(value + 100),
                )
                self.assertTrue(gate.open)
                self.assertFalse(gate._preroll)
                self.assertEqual(gate.process(pcm(300), 160), pcm(300))

    def test_zero_and_short_preroll_preserve_current_chunk(self):
        for ms in (0, 80):
            with self.subTest(ms=ms):
                gate = self.gate(ms)
                gate.process(pcm(0), 160)
                gate.process(pcm(500), 160)
                self.assertEqual(len(gate._preroll), ms * 32)
                self.assertEqual(
                    gate.process(pcm(600), 160),
                    pcm(500, ms) + pcm(600),
                )

    def test_trailing_silence_is_bounded_then_wire_silent(self):
        gate = self.gate()
        self.assertEqual(gate.process(pcm(1), 160), pcm(0))
        self.assertEqual(gate.process(pcm(1), 160), pcm(0))
        for _ in range(20):
            self.assertIsNone(gate.process(pcm(1), 160))
        self.assertFalse(gate.open)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(Config())
        self.controller._incoming_ring = bytearray(pcm(1, 1000) + pcm(2, 1000))

    def test_absolute_bounds_not_tail_aligned(self):
        self.assertEqual(self.controller._snapshot_utterance_audio(0, 32000), pcm(1, 1000))
        self.assertEqual(self.controller._snapshot_utterance_audio(32000, 64000, 32000), pcm(1, 1000))

    def test_rejects_missing_invalid_and_truncated_bounds(self):
        for start, end, base in (
            (None, 32000, 0), (0, None, 0), (None, None, 0),
            (0, 32000, 2), (0, 64002, 0), (-2, 32000, 0),
            (32000, 0, 0), (0, 0, 0), (1, 32000, 0),
            (0, 32001, 0), (0.0, 32000, 0), (False, 32000, 0),
            (0, 8000, 0),
        ):
            with self.subTest(start=start, end=end, base=base):
                self.assertEqual(self.controller._snapshot_utterance_audio(start, end, base), b'')

    def test_snapshot_is_immutable(self):
        snapshot = self.controller._snapshot_utterance_audio(0, 32000)
        self.controller._incoming_ring.clear()
        self.assertEqual(snapshot, pcm(1, 1000))


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.c = Controller(Config())
        self.c.cfg.debug_capture = False
        self.c.overlay_send = Mock()
        self.c.asr = SimpleNamespace(transcribe_offline=AsyncMock(return_value='refined'))
        self.c.nmt = SimpleNamespace(translate=AsyncMock(return_value='translated'))
        self.events = patch('olt.controller.logging.log_event').start()
        self.addCleanup(patch.stopall)

    async def refine(self):
        await self.c._refine_card('in-1', 'original', 'original translation', 0,
                                  'es', 'en', pcm(1, 1000))

    async def test_refinement_translation_failure_preserves_pair(self):
        self.c.nmt.translate.side_effect = RuntimeError('failed')
        await self.refine()
        self.c.overlay_send.assert_not_called()
        self.events.assert_not_called()

    async def test_refinement_generation_rechecked_after_translation(self):
        async def translate(*args):
            self.c._incoming_gen += 1
            return 'translated'
        self.c.nmt.translate.side_effect = translate
        await self.refine()
        self.c.overlay_send.assert_not_called()
        self.events.assert_not_called()

    async def test_refinement_success(self):
        await self.refine()
        self.c.overlay_send.assert_called_once_with({
            'cmd': 'card', 'id': 'in-1', 'direction': 'in',
            'source': 'refined', 'target': 'translated', 'state': 'incoming',
        })

    async def test_refinement_empty_unchanged_failed_or_stale_asr(self):
        for result in ('', 'original', RuntimeError('failed')):
            with self.subTest(result=result):
                self.c.asr.transcribe_offline.side_effect = (
                    result if isinstance(result, Exception) else None)
                self.c.asr.transcribe_offline.return_value = result
                await self.refine()
        self.c.asr.transcribe_offline.side_effect = None
        self.c.asr.transcribe_offline.return_value = 'refined'
        self.c._incoming_gen = 1
        await self.refine()
        self.c.nmt.translate.assert_not_called()
        self.c.overlay_send.assert_not_called()

    async def test_empty_refined_translation_preserves_pair(self):
        self.c.nmt.translate.return_value = ''
        await self.refine()
        self.c.overlay_send.assert_not_called()

    async def test_overlay_start_sends_history(self):
        self.c.cfg.overlay.history = False
        proc = SimpleNamespace(stdin=Mock(), pid=123, stderr=Mock())
        self.c._watch_overlay_stderr = AsyncMock()
        with patch('olt.controller.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
            await self.c.start_overlay()
            await asyncio.sleep(0)
        self.c.overlay_send.assert_called_once_with({'cmd': 'history', 'enabled': False})

    async def test_final_diagnostics_with_and_without_deltas(self):
        for delta, card in ((1.1, 1.2), (None, None)):
            with self.subTest(delta=delta):
                await self.c._finalize_incoming('in-1', 'original', 0, 1, 2, b'', delta, card)
                record = self.events.call_args.args[0]
                self.assertEqual(record['first_delta_receipt_s'], delta)
                self.assertEqual(record['capture_start_s'], 1)
                self.assertIsNotNone(record['first_card_sent_s'])
                if card is not None:
                    self.assertEqual(record['first_card_sent_s'], card)

    async def run_stream(self, read_chunk, events, *, gated=False):
        self.c.cfg.incoming.gate_enabled = gated
        closed = asyncio.Event()
        operations = []
        stream = SimpleNamespace(
            send_audio=AsyncMock(side_effect=lambda chunk: operations.append(('send', chunk))),
            clear=AsyncMock(side_effect=lambda: operations.append(('clear',))),
            close=AsyncMock(side_effect=closed.set),
            operations=operations,
        )
        stream.events = lambda: events(stream, closed)
        self.c.asr.connect_stream = AsyncMock(return_value=stream)
        cap = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), read_chunk=read_chunk)
        with patch('olt.controller.audio.monitor_capture', return_value=cap), \
                patch('olt.controller.audio.prune_debug_dir'):
            await asyncio.wait_for(self.c._incoming_once(), 2)
        cap.stop.assert_awaited_once()
        self.assertTrue(closed.is_set())
        return stream

    async def test_pump_eof_and_errors_close_waiting_reader(self):
        async def events(stream, closed):
            await closed.wait()
            if False:
                yield {}
        for error in (None, OSError('read failed'), RuntimeError('read failed')):
            with self.subTest(error=error):
                await self.run_stream(AsyncMock(return_value=b'', side_effect=error), events)

    async def test_send_failure_closes_reader(self):
        async def events(stream, closed):
            stream.send_audio.side_effect = OSError('send failed')
            await closed.wait()
            if False:
                yield {}
        await self.run_stream(AsyncMock(return_value=pcm(500)), events)

    async def test_completed_while_open_requests_no_immediate_clear(self):
        sent = asyncio.Event()
        reads = 0
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads <= 3:
                if reads == 3:
                    sent.set()
                return pcm(500 + reads)
            await asyncio.Event().wait()
        async def events(stream, closed):
            while stream.send_audio.await_count < 1:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            await sent.wait()
            self.assertEqual(
                [call.args[0] for call in stream.send_audio.call_args_list],
                [pcm(501), pcm(502), pcm(503)],
            )
            stream.clear.assert_not_awaited()
        await self.run_stream(read_chunk, events, gated=True)

    async def test_clear_follows_trailing_silence_before_next_preroll(self):
        allow_wire_idle = asyncio.Event()
        reads = 0
        chunks = iter((pcm(500), pcm(0), pcm(0), pcm(0), pcm(600), b''))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 320
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 4:
                await allow_wire_idle.wait()
            return next(chunks)
        async def events(stream, closed):
            while stream.send_audio.await_count < 3:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_wire_idle.set()
            await closed.wait()
            if False:
                yield {}
        stream = await self.run_stream(read_chunk, events, gated=True)
        self.assertEqual(
            stream.operations,
            [
                ('send', pcm(500)),
                ('send', pcm(0)),
                ('send', pcm(0)),
                ('clear',),
                ('send', pcm(0, 400) + pcm(600)),
            ],
        )

    async def test_rapid_reopen_defers_clear_without_audio_loss(self):
        second_sent = asyncio.Event()
        allow_second_close = asyncio.Event()
        reads = 0
        chunks = iter((
            pcm(500), pcm(0), pcm(600),
            pcm(0), pcm(0), pcm(0), b'',
        ))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 320
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 3:
                second_sent.set()
            elif reads == 4:
                await allow_second_close.wait()
            return next(chunks)
        async def events(stream, closed):
            await second_sent.wait()
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_second_close.set()
            await closed.wait()
            if False:
                yield {}
        stream = await self.run_stream(read_chunk, events, gated=True)
        self.assertEqual(
            stream.operations,
            [
                ('send', pcm(500)),
                ('send', pcm(0)),
                ('send', pcm(0) + pcm(600)),
                ('send', pcm(0)),
                ('send', pcm(0)),
            ],
        )

    async def test_rapid_reopen_clears_only_after_later_completion(self):
        allow_reopen = asyncio.Event()
        allow_wire_idle = asyncio.Event()
        reads = 0
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 320
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 3:
                await allow_reopen.wait()
            elif reads == 6:
                await allow_wire_idle.wait()
            values = (500, 0, 600, 0, 0, 0)
            return pcm(values[reads - 1]) if reads <= len(values) else b''
        async def events(stream, closed):
            while stream.send_audio.await_count < 2:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_reopen.set()
            while stream.send_audio.await_count < 5:
                await asyncio.sleep(0)
            stream.clear.assert_not_awaited()
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'second'}
            allow_wire_idle.set()
            await closed.wait()
            if False:
                yield {}
        stream = await self.run_stream(read_chunk, events, gated=True)
        self.assertEqual(
            stream.operations,
            [
                ('send', pcm(500)),
                ('send', pcm(0)),
                ('send', pcm(0) + pcm(600)),
                ('send', pcm(0)),
                ('send', pcm(0)),
                ('clear',),
            ],
        )

    async def test_clear_ack_is_diagnostic_only(self):
        allow_wire_idle = asyncio.Event()
        reads = 0
        chunks = iter((pcm(500), pcm(0), pcm(0), b''))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 160
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 3:
                await allow_wire_idle.wait()
            return next(chunks)
        async def events(stream, closed):
            while stream.send_audio.await_count < 2:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_wire_idle.set()
            while stream.clear.await_count < 1:
                await asyncio.sleep(0)
            yield {'type': 'input_audio_buffer.cleared'}
            yield {'type': 'conversation.item.input_audio_transcription.delta',
                   'delta': 'next'}
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'next'}
        with patch('olt.controller.log.info') as info:
            await self.run_stream(read_chunk, events, gated=True)
        self.assertEqual(self.c._finalize_incoming.await_count, 2)
        drafts = [call.args[0] for call in self.c.overlay_send.call_args_list]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]['source'], 'next')
        self.assertTrue(any('clear acknowledged' in call.args[0]
                            for call in info.call_args_list))

    async def test_clear_failure_closes_session_for_reconnect(self):
        allow_wire_idle = asyncio.Event()
        reads = 0
        chunks = iter((pcm(500), pcm(0), pcm(0)))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 160
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 3:
                await allow_wire_idle.wait()
            return next(chunks)
        async def events(stream, closed):
            stream.clear.side_effect = OSError('clear failed')
            while stream.send_audio.await_count < 2:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_wire_idle.set()
            await closed.wait()
            if False:
                yield {}
        with patch('olt.controller.log.error') as error:
            stream = await self.run_stream(read_chunk, events, gated=True)
        stream.close.assert_awaited()
        self.assertTrue(any('clear failed' in call.args[0]
                            for call in error.call_args_list))

    async def test_clear_timeout_closes_session_for_reconnect(self):
        allow_wire_idle = asyncio.Event()
        reads = 0
        chunks = iter((pcm(500), pcm(0), pcm(0)))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.incoming.multimedia_endpointing_ms = 160
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            reads += 1
            if reads == 3:
                await allow_wire_idle.wait()
            return next(chunks)
        async def block_clear():
            await asyncio.Event().wait()
        async def events(stream, closed):
            stream.clear.side_effect = block_clear
            while stream.send_audio.await_count < 2:
                await asyncio.sleep(0)
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
            allow_wire_idle.set()
            await closed.wait()
            if False:
                yield {}
        with patch('olt.controller._STREAM_CLEAR_TIMEOUT_S', 0.01), \
                patch('olt.controller.log.error') as error:
            stream = await self.run_stream(read_chunk, events, gated=True)
        stream.close.assert_awaited()
        self.assertTrue(any('clear failed' in call.args[0]
                            for call in error.call_args_list))

    async def test_non_multimedia_completion_does_not_clear(self):
        self.c.cfg.incoming.multimedia = False
        self.c._finalize_incoming = AsyncMock()
        async def events(stream, closed):
            yield {'type': 'conversation.item.input_audio_transcription.completed',
                   'transcript': 'first'}
        stream = await self.run_stream(AsyncMock(return_value=b''), events, gated=True)
        stream.clear.assert_not_awaited()

    async def test_gate_sent_capture_contains_exact_open_interval(self):
        chunks = iter((pcm(0), pcm(500), pcm(600), pcm(0), b''))
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.debug_capture = True
        with tempfile.TemporaryDirectory() as tmp:
            self.c.cfg.debug_dir = tmp
            async def events(stream, closed):
                await closed.wait()
                if False:
                    yield {}
            stream = await self.run_stream(AsyncMock(side_effect=chunks), events, gated=True)
            sent = list((Path(tmp) / 'sent').glob('sent-g0-in-1-*.wav'))
            self.assertEqual(len(sent), 1)
            with wave.open(str(sent[0]), 'rb') as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                                 (1, 2, 16000))
                captured = wav.readframes(wav.getnframes())
            expected = pcm(0) + pcm(500) + pcm(600)
            self.assertEqual(captured, expected)
            self.assertEqual(
                [call.args[0] for call in stream.send_audio.call_args_list],
                [pcm(0), pcm(0) + pcm(500), pcm(600), pcm(0)],
            )

    async def test_gate_sent_capture_finalizes_on_cancellation(self):
        opened = asyncio.Event()
        reads = 0
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c.cfg.debug_capture = True
        with tempfile.TemporaryDirectory() as tmp:
            self.c.cfg.debug_dir = tmp
            async def read_chunk(ms):
                nonlocal reads
                reads += 1
                if reads == 1:
                    return pcm(0)
                if reads == 2:
                    return pcm(500)
                opened.set()
                await asyncio.Event().wait()
            async def events(stream, closed):
                await closed.wait()
                if False:
                    yield {}
            closed = asyncio.Event()
            stream = SimpleNamespace(
                send_audio=AsyncMock(), close=AsyncMock(side_effect=closed.set),
            )
            stream.events = lambda: events(stream, closed)
            self.c.asr.connect_stream = AsyncMock(return_value=stream)
            cap = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), read_chunk=read_chunk)
            with patch('olt.controller.audio.monitor_capture', return_value=cap):
                task = asyncio.create_task(self.c._incoming_once())
                await asyncio.wait_for(opened.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            sent = list((Path(tmp) / 'sent').glob('sent-g0-in-1-*.wav'))
            self.assertEqual(len(sent), 1)
            with wave.open(str(sent[0]), 'rb') as wav:
                self.assertEqual(wav.readframes(wav.getnframes()), pcm(0) + pcm(500))

    async def test_ambiguous_finals_skip_refinement_and_reset_diagnostics(self):
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            await asyncio.sleep(0)
            return pcm(500, 1000)
        async def events(stream, closed):
            # Overflow the ring while supplying audio beyond a delayed final.
            while stream.send_audio.await_count < 22:
                await asyncio.sleep(0)
            for processed in (1.0, 2.0):
                yield {'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'hello'}
                yield {'type': 'conversation.item.input_audio_transcription.completed',
                       'transcript': 'hello', 'audio_processed': processed}
                await asyncio.sleep(0)
        await self.run_stream(read_chunk, events)
        self.assertEqual(len(self.c._incoming_ring), 20 * 32000)
        self.assertEqual(self.c._finalize_incoming.await_count, 2)
        calls = self.c._finalize_incoming.call_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        for call in calls:
            self.assertEqual(call.args[5], b'')
            self.assertLessEqual(call.args[6], call.args[7])
        self.assertLessEqual(calls[0].args[7], calls[1].args[6])

    async def test_delayed_asr_after_final_warns_once_without_losing_stream(self):
        final_seen = asyncio.Event()
        reads = 0
        self.c.cfg.incoming.gate_close_ms = 160
        self.c.cfg.incoming.gate_open_ms = 160
        self.c._finalize_incoming = AsyncMock()
        async def read_chunk(ms):
            nonlocal reads
            await final_seen.wait()
            reads += 1
            if reads <= 2:
                return pcm(0 if reads == 1 else 500)
            await asyncio.Event().wait()
        async def events(stream, closed):
            yield {'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'hello'}
            yield {'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'hello'}
            final_seen.set()
            await asyncio.sleep(0.35)
            self.assertFalse(closed.is_set())
            self.assertEqual(
                [call.args[0] for call in stream.send_audio.call_args_list],
                [pcm(0), pcm(0) + pcm(500)],
            )
            yield {'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'Te amo'}
            yield {'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Te amo'}
            await asyncio.sleep(0)
        with patch('olt.controller._ASR_DELAY_WARNING_S', 0), \
                patch('olt.controller.log.warning') as warning:
            await self.run_stream(read_chunk, events, gated=True)
        warning.assert_called_once()
        self.assertEqual(self.c._finalize_incoming.call_args.args[1], 'Te amo')

    async def test_delay_monitor_spares_cold_stream_and_disarms_on_delta(self):
        for cold in (True, False):
            with self.subTest(cold=cold):
                ready = asyncio.Event()
                reopened = asyncio.Event()
                reads = 0
                self.c.cfg.incoming.gate_close_ms = 160
                self.c.cfg.incoming.gate_open_ms = 160
                async def read_chunk(ms):
                    nonlocal reads
                    await ready.wait()
                    reads += 1
                    if reads <= 2:
                        return pcm(0 if reads == 1 else 500)
                    reopened.set()
                    await asyncio.Event().wait()
                async def events(stream, closed):
                    if not cold:
                        yield {'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'first'}
                    ready.set()
                    await reopened.wait()
                    if not cold:
                        yield {'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'second'}
                    await asyncio.sleep(0.15)
                    self.assertFalse(closed.is_set())
                with patch('olt.controller._ASR_DELAY_WARNING_S', 0), \
                        patch('olt.controller.log.warning') as warning:
                    await self.run_stream(read_chunk, events, gated=True)
                self.assertEqual(warning.call_count, 1 if cold else 0)

    async def test_incoming_reconnects_after_session_exit(self):
        self.c._incoming_once = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await self.c.incoming()
        self.assertEqual(self.c._incoming_once.await_count, 2)


if __name__ == '__main__':
    unittest.main()
