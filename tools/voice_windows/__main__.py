"""PowerShell: python -B -m tools.voice_windows [self-check|local-e2e|connect]."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import wave

from .audio import AudioDevice, decode_wav, synthetic_wav
from .client import ClientError, VoiceClient, browser_pair, microphone_turns, origin_checked, self_check, wss_connection


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never reflect accidentally pasted credentials/unknown argument values.
        self.exit(2, "invalid_cli_arguments; use --help\n")


def parse_args(argv=None):
    parser = Parser(description="CampusPulse native Windows harness; synthetic WAV by default.")
    parser.add_argument("mode", choices=["self-check", "local-e2e", "connect"], nargs="?", default="local-e2e")
    parser.add_argument("--backend", help="Explicit deployed HTTPS origin, no path/query/credentials")
    parser.add_argument("--wav", type=Path, help="PCM16 mono/stereo 16/48 kHz WAV, <=120 seconds")
    parser.add_argument("--output-wav", type=Path, help="Explicit new response file; never overwritten (24 kHz mono PCM16)")
    parser.add_argument("--microphone", action="store_true", help="Connect only: opt in to explicit start/end/stop turns")
    parser.add_argument("--playback", action="store_true", help="Connect only: opt in to audio output hardware")
    args = parser.parse_args(argv)
    if ((args.microphone or args.playback) and args.mode != "connect"
            or args.microphone and args.wav or args.mode == "connect" and not args.backend
            or args.mode != "connect" and args.backend
            or args.mode == "self-check" and (args.wav or args.output_wav)):
        parser.error("")
    return args


async def console_command():
    """Cancellable polling: never leave a detached input()/device worker thread."""
    if sys.platform != "win32":
        raise ClientError("microphone_console_requires_windows")
    import msvcrt
    line = ""
    print("Turn commands: start = capture; end = end utterance; stop = close. Enter submits.", flush=True)
    while True:
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == "\x03":
                return "stop"
            if char == "\r":
                if line in ("start", "end", "stop"):
                    return line
                line = ""
            elif char == "\b":
                line = line[:-1]
            elif char.isascii() and char.isalpha() and len(line) < 5:
                line += char
        await asyncio.sleep(.02)


async def execute(args):
    if args.mode == "self-check":
        return self_check()
    # Validate input before pairing/network/hardware. Default fixture stays memory only.
    pcm = None
    if not args.microphone:
        if args.wav and args.wav.stat().st_size > 64 * 1024 * 1024:
            raise ClientError("invalid_wav")
        pcm = decode_wav(args.wav.read_bytes() if args.wav else synthetic_wav())
    device = wav = output_file = None
    trace = lambda event: print(json.dumps(dict(mode=args.mode, evidence_class="windows_local", **event)), flush=True)
    try:
        if args.output_wav:
            output_file = args.output_wav.open("xb")
            wav = wave.open(output_file, "wb")
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        if args.mode == "connect":
            origin = origin_checked(args.backend)
            credential = await browser_pair(origin)
            # A single minimal optional audio distribution, imported after opt-in.
            if args.microphone or args.playback:
                device = AudioDevice()
            if args.playback:
                device.start_output()
        async def sink(chunk):
            if device and args.playback:
                await device.play(chunk)
            if wav:
                wav.writeframes(chunk)
        if args.mode == "local-e2e":
            from .local import local_e2e
            return await local_e2e(pcm, sink=sink, trace=trace)
        async with wss_connection(origin, credential) as socket:
            client = VoiceClient(socket, sink=sink, flush=device.flush if device else lambda: None, trace=trace)
            async def source(c):
                if args.microphone:
                    await microphone_turns(c, device, console_command)
                else:
                    await c.finish_wav(pcm)
                    if args.playback:
                        deadline = time.monotonic() + 2
                        while device.output_pcm:
                            if time.monotonic() > deadline:
                                raise ClientError("playback_drain_timeout")
                            await asyncio.sleep(.01)
            result = await client.run(source)
            result.update(mode="connect", evidence_class="windows_local",
                input_mode="live_windows_explicit_turns" if args.microphone else "wav_simulation",
                cloud_evidence="not_established_by_client", iphone_evidence="untested")
            return result
    finally:
        if device:
            device.close()
        if wav:
            wav.close()
        if output_file:
            output_file.close()


def main(argv=None):
    args = parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except KeyboardInterrupt:
        print('{"status":"stopped_by_operator"}')
        return 130
    except Exception:
        # Never print exceptions, tracebacks, raw transcripts, URLs or credentials.
        print('{"status":"failed","code":"voice_client_failed","evidence_class":"windows_local"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
