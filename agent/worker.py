import asyncio
import os
import time
import json
import uuid
import logging
import httpx
import numpy as np
import webrtcvad
from typing import Optional, AsyncIterator

"""
Pipecat-like Minimal Agent Worker (prototype)
Features:
- Connects to LiveKit room (placeholder LiveKit SDK usage).
- Subscribes to first remote audio track (PSTN caller).
- Performs frame-based VAD to detect speech segments.
- Streams audio to STT provider (Deepgram) for transcript (simplified partial handling).
- On final transcript, generates reply text (echo or scripted).
- Streams TTS (ElevenLabs) back into a published audio track (Opus or PCM placeholder).
- Barge-in: if new speech detected while TTS playing, cancels current TTS task immediately.
- Latency instrumentation:
    speech_start_ts
    stt_first_partial_ts
    stt_final_ts
    agent_decision_ts
    tts_first_byte_ts
    playback_start_ts
  Sends JSON event to METRICS_ENDPOINT for each conversation turn.

NOTE: This is a high-level prototype; integration points with actual Pipecat/LiveKit SDK may need adjustment.
"""

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

# Environment
LIVEKIT_HOST = os.getenv("LIVEKIT_HOST", "http://livekit:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
METRICS_ENDPOINT = os.getenv("METRICS_ENDPOINT", "http://metrics:9100/ingest")

STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram")
STT_API_KEY = os.getenv("STT_API_KEY")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-US")

TTS_PROVIDER = os.getenv("TTS_PROVIDER", "elevenlabs")
TTS_API_KEY = os.getenv("TTS_API_KEY")
TTS_VOICE_ID = os.getenv("TTS_VOICE_ID", "default")

AGENT_RESPONSE_MODE = os.getenv("AGENT_RESPONSE_MODE", "echo")
AGENT_SCRIPTED_LINES = os.getenv("AGENT_SCRIPTED_LINES", "Hello.|How can I help?|Goodbye.").split("|")
AGENT_BARGE_IN = os.getenv("AGENT_BARGE_IN", "true").lower() == "true"

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * (FRAME_MS / 1000.0)) * 2  # 16-bit mono PCM

class VADDetector:
    def __init__(self, aggressiveness: int = 2):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.active = False
        self.speech_start_ts: Optional[float] = None

    def process(self, frame: bytes, ts: float) -> Optional[float]:
        is_speech = self.vad.is_speech(frame, SAMPLE_RATE)
        if is_speech and not self.active:
            self.active = True
            self.speech_start_ts = ts
            return ts
        if not is_speech and self.active:
            # Could implement hangover; simplified immediate end
            self.active = False
        return None

class STTStream:
    def __init__(self):
        self.first_partial_ts: Optional[float] = None
        self.final_transcript: Optional[str] = None
        self._final = asyncio.Event()
        self._closed = False

    async def feed_audio(self, pcm_frame: bytes):
        # Placeholder: In real implementation audio is sent over websocket to STT provider.
        pass

    async def simulate_stt(self, transcript_accumulator: list[str]):
        # Fake partials & final creation for demo/testing when no real STT
        await asyncio.sleep(0.05)
        if not self.first_partial_ts:
            self.first_partial_ts = time.time()
        # After some delay finalize
        await asyncio.sleep(0.25)
        self.final_transcript = " ".join(transcript_accumulator).strip()
        self._final.set()

    async def wait_final(self, timeout: float = 5.0) -> Optional[str]:
        try:
            await asyncio.wait_for(self._final.wait(), timeout=timeout)
            return self.final_transcript
        except asyncio.TimeoutError:
            return None

    def close(self):
        self._closed = True

class TTSStream:
    def __init__(self):
        self.first_audio_ts: Optional[float] = None
        self._cancel = False

    async def stream_tts(self, text: str) -> AsyncIterator[bytes]:
        # Placeholder streaming TTS: yield short PCM chunks
        # In real code call ElevenLabs streaming API
        duration_sec = min(2.5, 0.06 * len(text))
        total_frames = int(duration_sec * 1000 / FRAME_MS)
        for i in range(total_frames):
            if self._cancel:
                break
            # Dummy sine wave tone (440Hz)
            t = np.arange(0, FRAME_BYTES // 2) / SAMPLE_RATE
            tone = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
            # Convert float32 -> int16 PCM
            pcm16 = (tone * 32767).astype(np.int16).tobytes()
            if self.first_audio_ts is None:
                self.first_audio_ts = time.time()
            yield pcm16
            await asyncio.sleep(FRAME_MS / 1000.0)

    def cancel(self):
        self._cancel = True

class LatencyTurn:
    def __init__(self):
        self.speech_start_ts: Optional[float] = None
        self.stt_first_partial_ts: Optional[float] = None
        self.stt_final_ts: Optional[float] = None
        self.agent_decision_ts: Optional[float] = None
        self.tts_first_byte_ts: Optional[float] = None
        self.playback_start_ts: Optional[float] = None

    def to_dict(self):
        return {
            "speech_start_ms": self._ms(self.speech_start_ts),
            "stt_first_partial_ms": self._ms(self.stt_first_partial_ts),
            "stt_final_ms": self._ms(self.stt_final_ts),
            "agent_decision_ms": self._ms(self.agent_decision_ts),
            "tts_first_byte_ms": self._ms(self.tts_first_byte_ts),
            "playback_start_ms": self._ms(self.playback_start_ts),
            "round_trip_ms": self._delta(self.speech_start_ts, self.playback_start_ts),
        }

    def _ms(self, ts):
        return int(ts * 1000) if ts else None

    def _delta(self, a, b):
        if a and b:
            return int((b - a) * 1000)
        return None

async def post_metrics(event: dict):
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.post(METRICS_ENDPOINT, json=event)
    except Exception as e:
        log.debug(f"metrics post failed: {e}")

class AgentSession:
    def __init__(self, room: str, identity: str):
        self.room = room
        self.identity = identity
        self.vad = VADDetector()
        self.current_tts: Optional[TTSStream] = None
        self.tts_task: Optional[asyncio.Task] = None
        self.active_turn: Optional[LatencyTurn] = None
        self.script_index = 0

    def next_script_line(self) -> str:
        line = AGENT_SCRIPTED_LINES[self.script_index % len(AGENT_SCRIPTED_LINES)]
        self.script_index += 1
        return line

    async def handle_audio_frame(self, pcm_frame: bytes):
        ts = time.time()
        speech_start = self.vad.process(pcm_frame, ts)
        if speech_start:
            log.debug("Speech start detected")
            # Barge-in: cancel TTS if currently speaking
            if AGENT_BARGE_IN and self.current_tts and self.tts_task and not self.tts_task.done():
                log.info("Barge-in: cancelling TTS")
                self.current_tts.cancel()
            self.active_turn = LatencyTurn()
            self.active_turn.speech_start_ts = speech_start
            # Start a new STT stream (simplified stub)
            asyncio.create_task(self._run_stt_collect([self._frame_energy(pcm_frame)]))

    async def _run_stt_collect(self, transcript_accum):
        stt = STTStream()
        # Simulated partial -> final timeline
        await stt.simulate_stt(transcript_accum)
        if stt.first_partial_ts and self.active_turn and not self.active_turn.stt_first_partial_ts:
            self.active_turn.stt_first_partial_ts = stt.first_partial_ts
        final_text = await stt.wait_final()
        if final_text and self.active_turn:
            self.active_turn.stt_final_ts = time.time()
            reply = self._build_reply(final_text)
            self.active_turn.agent_decision_ts = time.time()
            await self._start_tts(reply)

    def _build_reply(self, transcript: str) -> str:
        if AGENT_RESPONSE_MODE == "echo":
            return f"You said: {transcript}. Nice!"
        else:
            return self.next_script_line()

    async def _start_tts(self, text: str):
        self.current_tts = TTSStream()
        tts_stream = self.current_tts

        async def run():
            first_chunk = True
            async for chunk in tts_stream.stream_tts(text):
                if first_chunk and self.active_turn:
                    self.active_turn.tts_first_byte_ts = tts_stream.first_audio_ts
                    # playback_start_ts approximated as same for prototype
                    self.active_turn.playback_start_ts = tts_stream.first_audio_ts
                    # Emit metrics
                    await post_metrics({
                        "type": "latency_turn",
                        "room": self.room,
                        "identity": self.identity,
                        "timestamp": int(time.time() * 1000),
                        **self.active_turn.to_dict()
                    })
                    first_chunk = False
                # TODO: publish audio chunk to LiveKit track
                await asyncio.sleep(0)  # yield control
            log.debug("TTS stream ended")
        self.tts_task = asyncio.create_task(run())

    def _frame_energy(self, pcm_frame: bytes) -> str:
        # crude placeholder "transcript" token
        return "audio"

async def fake_livekit_audio_source(session: AgentSession):
    """
    Simulated incoming PCM frames for development without LiveKit.
    Generates random speech bursts.
    """
    while True:
        # Generate random 'speech' burst each 3 seconds
        for _ in range(int(1000 / FRAME_MS) * 1):  # 1 second of frames
            pcm = (np.random.rand(FRAME_BYTES // 2) * 2 - 1.0)
            pcm16 = (pcm * 32767).astype(np.int16).tobytes()
            await session.handle_audio_frame(pcm16)
            await asyncio.sleep(FRAME_MS / 1000)
        await asyncio.sleep(2)

async def main():
    identity = f"agent-{uuid.uuid4().hex[:6]}"
    room = os.getenv("AGENT_ROOM", "dev-room")
    session = AgentSession(room=room, identity=identity)
    log.info(f"Agent starting in room={room} identity={identity}")
    # Placeholder: connect to LiveKit & publish outbound track
    # For now run simulated incoming audio
    await fake_livekit_audio_source(session)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Agent shutdown requested")
