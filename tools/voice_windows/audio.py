"""Bounded PCM handling. Only explicit opt-in imports sounddevice."""
from __future__ import annotations

import asyncio
from collections import deque
import io
import math
import queue
import struct
import threading
import time
import wave

from campuspulse_recovery.voice.protocol import ProtocolError


def synthetic_wav(seconds=.4):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        w.writeframes(b"".join(struct.pack("<h", int(1500 * math.sin(i * 2 * math.pi * 440 / 16000)))
                               for i in range(int(16000 * seconds))))
    return out.getvalue()


def decode_wav(raw: bytes) -> bytes:
    """PCM16 mono/stereo 16/48 kHz only; 48k uses a three-sample box filter.

    Deliberately limited offline conversion, not a high quality arbitrary-rate
    resampler. Reject truncated containers, huge files and partial sample groups.
    """
    try:
        if len(raw) > 64 * 1024 * 1024 or len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ValueError()
        if struct.unpack_from("<I", raw, 4)[0] + 8 != len(raw):
            raise ValueError()
        cursor, data_sizes, formats = 12, [], 0
        while cursor < len(raw):
            if cursor + 8 > len(raw):
                raise ValueError()
            kind, size = raw[cursor:cursor + 4], struct.unpack_from("<I", raw, cursor + 4)[0]
            cursor += 8 + size + size % 2
            if cursor > len(raw):
                raise ValueError()
            if kind == b"data":
                data_sizes.append(size)
            elif kind == b"fmt ":
                formats += 1
        if len(data_sizes) != 1 or formats != 1:
            raise ValueError()
        with wave.open(io.BytesIO(raw), "rb") as w:
            channels, width, rate, frames, comp, _ = w.getparams()
            if channels not in (1, 2) or width != 2 or rate not in (16000, 48000) or comp != "NONE":
                raise ValueError()
            if not 0 < frames <= rate * 120 or frames % (rate // 16000):
                raise ValueError()
            if data_sizes[0] != frames * channels * width:
                raise ValueError()
            pcm = w.readframes(frames)
            if len(pcm) != frames * channels * 2:
                raise ValueError()
        if channels == 1 and rate == 16000:
            return pcm
        factor = rate // 16000
        result, total, offset = bytearray(frames // factor * 2), 0, 0
        for index, samples in enumerate(struct.iter_unpack("<" + "h" * channels, pcm)):
            total += sum(samples) // channels
            if (index + 1) % factor == 0:
                struct.pack_into("<h", result, offset, total // factor)
                offset, total = offset + 2, 0
        return bytes(result)
    except (ValueError, EOFError, wave.Error, struct.error):
        raise ValueError("invalid_wav") from None


class Pacer:
    """Reset to actual send time after a stall: never repay lateness in a burst."""
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep, self.deadline = clock, sleep, 0.

    async def wait(self, duration):
        await self.sleep(max(0, self.deadline - self.clock()))
        self.deadline = self.clock() + duration


class OutputBuffer:
    """Includes in-flight PCM in the 200 ms cap; flush cancels its sink task."""
    def __init__(self, sink, flush=lambda: None):
        self.sink, self.flush_sink = sink, flush
        self.items, self.changed = deque(), asyncio.Event()
        self.bytes = self.peak = self.consumed = self.dropped = 0
        self.inflight = None

    def put(self, frame):
        if self.bytes + len(frame.pcm) > 9600 or len(self.items) >= 32:
            raise ProtocolError("backpressure")
        self.items.append(frame)
        self.bytes += len(frame.pcm)
        self.peak = max(self.peak, self.bytes)
        self.changed.set()

    def flush(self):
        self.dropped += sum(len(f.pcm) for f in self.items)
        self.bytes -= sum(len(f.pcm) for f in self.items)
        self.items.clear()
        if self.inflight:
            self.inflight.cancel()
        self.flush_sink()

    async def run(self):
        pacer = Pacer()
        try:
            while True:
                if not self.items:
                    self.changed.clear()
                    await self.changed.wait()
                    continue
                frame = self.items.popleft()
                async def play():
                    await pacer.wait(len(frame.pcm) / 48000)
                    await self.sink(frame.pcm)
                self.inflight = asyncio.create_task(play())
                try:
                    await self.inflight
                    self.consumed += len(frame.pcm)
                except asyncio.CancelledError:
                    self.dropped += len(frame.pcm)
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.bytes -= len(frame.pcm)
                    self.inflight = None
        finally:
            self.flush()


class AudioDevice:
    """Callback I/O avoids unjoinable blocking device read/write worker threads.

    Input is opened only during explicit start/end turns. Output only on playback
    opt-in. Callback queues are bounded, including callbacks from PortAudio.
    """
    def __init__(self, module=None):
        if module is None:
            import sounddevice as module
        self.module = module
        self.input = self.output = None
        self.capture = queue.Queue(maxsize=10)
        self.output_pcm = bytearray()
        self.lock = threading.Lock()
        self.failed = False

    def start_input(self):
        if self.input is not None:
            raise ValueError("input_already_started")
        while not self.capture.empty():
            self.capture.get_nowait()
        def callback(data, frames, timing, status):
            if status or frames != 320:
                self.failed = True
                return
            try:
                self.capture.put_nowait(bytes(data))
            except queue.Full:
                self.failed = True
        self.input = self.module.RawInputStream(samplerate=16000, blocksize=320,
            channels=1, dtype="int16", callback=callback)
        self.input.start()

    def end_input(self):
        if self.input is not None:
            stream, self.input = self.input, None
            try:
                stream.abort()
            finally:
                stream.close()
        while not self.capture.empty():
            self.capture.get_nowait()

    async def read(self):
        while True:
            if self.failed:
                raise ProtocolError("backpressure")
            try:
                return self.capture.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.005)

    def start_output(self):
        def callback(out, frames, timing, status):
            if status:
                self.failed = True
            count = frames * 2
            with self.lock:
                data = bytes(self.output_pcm[:count])
                del self.output_pcm[:count]
            out[:] = data + bytes(count - len(data))
        self.output = self.module.RawOutputStream(samplerate=24000, blocksize=480,
            channels=1, dtype="int16", callback=callback)
        self.output.start()

    async def play(self, pcm):
        if self.failed:
            raise ProtocolError("backpressure")
        with self.lock:
            if len(self.output_pcm) + len(pcm) > 9600:
                raise ProtocolError("backpressure")
            self.output_pcm.extend(pcm)


        while True:
            if self.failed:
                raise ProtocolError("backpressure")
            with self.lock:
                if not self.output_pcm:
                    return
            await asyncio.sleep(.005)

    def flush(self):
        with self.lock:
            self.output_pcm.clear()

    def close(self):
        self.end_input()
        self.flush()
        if self.output is not None:
            stream, self.output = self.output, None
            try:
                stream.abort()
            finally:
                stream.close()
