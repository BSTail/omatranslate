import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from olt.config import ASRConfig, PathsConfig
from olt.nemo import NemoASR


class FakeWarmStream:
    def __init__(self, *, prove=True, acknowledge_clear=True, proof_type="completed",
                 block_close=False):
        self.acknowledge_clear = acknowledge_clear
        self.proof_type = proof_type
        self.block_close = block_close
        self.events_started = asyncio.Event()
        self.commit_called = asyncio.Event()
        self.proof_allowed = asyncio.Event()
        self.clear_called = asyncio.Event()
        self.reader_finished = asyncio.Event()
        self.calls = []
        if prove:
            self.proof_allowed.set()

    async def send_audio(self, pcm):
        self.calls.append(("audio", pcm))

    async def commit(self):
        self.calls.append(("commit",))
        self.commit_called.set()

    async def clear(self):
        self.calls.append(("clear",))
        self.clear_called.set()

    async def close(self):
        self.calls.append(("close",))
        if self.block_close:
            await asyncio.Event().wait()

    async def events(self):
        try:
            self.events_started.set()
            yield {"type": "session.updated"}
            await self.commit_called.wait()
            await self.proof_allowed.wait()
            if self.proof_type == "delta":
                yield {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "delta": "warm",
                    "audio_processed": 1.6,
                }
            else:
                yield {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "",
                    "audio_processed": 3.2,
                }
            await self.clear_called.wait()
            if self.acknowledge_clear:
                yield {"type": "input_audio_buffer.cleared"}
            await asyncio.Event().wait()
        finally:
            self.reader_finished.set()


class StreamingWarmupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.asr = NemoASR(PathsConfig(), ASRConfig())

    async def run_warmup(self, stream, *, proof_timeout=1.0):
        self.asr.connect_stream = AsyncMock(return_value=stream)
        original_sleep = asyncio.sleep

        async def no_pacing_delay(_delay):
            await original_sleep(0)

        with patch("olt.nemo.asyncio.sleep", side_effect=no_pacing_delay), \
                patch("olt.nemo._STREAM_WARMUP_PROOF_TIMEOUT_S", proof_timeout), \
                patch("olt.nemo._STREAM_WARMUP_CLEAR_TIMEOUT_S", 0.1), \
                patch("olt.nemo._STREAM_WARMUP_IO_TIMEOUT_S", 0.1):
            await self.asr.warm_stream("es-ES")

    async def test_does_not_clear_or_close_before_inference_proof(self):
        stream = FakeWarmStream(prove=False)
        self.asr.connect_stream = AsyncMock(return_value=stream)
        original_sleep = asyncio.sleep

        async def no_pacing_delay(_delay):
            await original_sleep(0)

        with patch("olt.nemo.asyncio.sleep", side_effect=no_pacing_delay), \
                patch("olt.nemo._STREAM_WARMUP_PROOF_TIMEOUT_S", 1.0):
            task = asyncio.create_task(self.asr.warm_stream("es-ES"))
            await asyncio.wait_for(stream.commit_called.wait(), 1)
            self.assertNotIn(("clear",), stream.calls)
            self.assertNotIn(("close",), stream.calls)
            stream.proof_allowed.set()
            await asyncio.wait_for(task, 1)
        self.assertIn(("clear",), stream.calls)
        self.assertIn(("close",), stream.calls)
        self.assertTrue(stream.reader_finished.is_set())

    async def test_success_clears_and_closes_after_processing_event(self):
        stream = FakeWarmStream(proof_type="delta")
        await self.run_warmup(stream)
        kinds = [call[0] for call in stream.calls]
        self.assertEqual(kinds.count("audio"), 20)
        self.assertLess(kinds.index("commit"), kinds.index("clear"))
        self.assertLess(kinds.index("clear"), kinds.index("close"))
        self.assertTrue(stream.reader_finished.is_set())
        self.assertFalse(any(t.get_name() == "olt-stream-warmup-events"
                             for t in asyncio.all_tasks() if not t.done()))

    async def test_blocking_close_is_bounded_after_reader_cleanup(self):
        stream = FakeWarmStream(block_close=True)
        with patch("olt.nemo.log.warning") as warning:
            await self.run_warmup(stream)
        self.assertTrue(stream.reader_finished.is_set())
        self.assertEqual(stream.calls[-1], ("close",))
        self.assertTrue(any("close failed" in call.args[0]
                            for call in warning.call_args_list))
        self.assertFalse(any(t.get_name() == "olt-stream-warmup-events"
                             for t in asyncio.all_tasks() if not t.done()))

    async def test_processing_timeout_is_nonfatal_and_leaves_no_reader(self):
        stream = FakeWarmStream(prove=False)
        with patch("olt.nemo.log.warning") as warning:
            await self.run_warmup(stream, proof_timeout=0.01)
        self.assertNotIn(("clear",), stream.calls)
        self.assertEqual(stream.calls[-1], ("close",))
        self.assertTrue(stream.reader_finished.is_set())
        self.assertTrue(any("non-fatal" in call.args[0] for call in warning.call_args_list))
        self.assertFalse(any(t.get_name() == "olt-stream-warmup-events"
                             for t in asyncio.all_tasks() if not t.done()))


if __name__ == "__main__":
    unittest.main()
