"""PinchTranslatorService — Pipecat FrameProcessor for real-time speech-to-speech translation.

Pinch accepts audio in one language and returns audio in another language, using a
LiveKit room internally for media transport.  This module bridges Pipecat's frame
pipeline with that LiveKit room:

  InputAudioRawFrame  --> [resample to 48 kHz] --> Pinch LiveKit room
  Pinch LiveKit room  --> [resample to pipeline rate] --> OutputAudioRawFrame
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService

from .models import TranscriptEvent, TranslatorOptions

try:
    from livekit import rtc as lk_rtc
except ImportError:  # pragma: no cover
    lk_rtc = None  # type: ignore[assignment]

_PINCH_SAMPLE_RATE = 48_000
_PINCH_CHANNELS = 1
_RETRY_DELAYS = (1.0, 2.0, 4.0)


class PinchError(Exception):
    """Base class for all Pinch plugin errors."""


class PinchAuthError(PinchError):
    """Raised when the Pinch API returns HTTP 401 (invalid or missing API key)."""


class PinchRateLimitError(PinchError):
    """Raised when the Pinch API returns HTTP 429 (rate-limited)."""


class PinchSessionError(PinchError):
    """Raised for any other session-creation or LiveKit-connection failure."""


def _resample_pcm(data: bytes, in_rate: int, out_rate: int) -> bytes:
    """Resample 16-bit little-endian mono PCM from *in_rate* to *out_rate*.

    Tries the best available backend in order:

    1. ``soxr`` + ``numpy`` — highest quality (``pip install pipecat-plugins-pinch[quality]``)
    2. ``audioop`` — standard library, removed in Python 3.13
    3. Pure-Python linear interpolation — always available, lowest quality
    """
    if in_rate == out_rate:
        return data

    try:
        import numpy as np
        import soxr

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        resampled = soxr.resample(samples, in_rate, out_rate)
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
    except ImportError:
        pass

    try:
        import audioop  # type: ignore[import]

        converted, _ = audioop.ratecv(data, 2, _PINCH_CHANNELS, in_rate, out_rate, None)
        return converted
    except (ImportError, Exception):
        pass

    import struct

    n_in = len(data) // 2
    if n_in == 0:
        return data
    samples_in = struct.unpack(f"<{n_in}h", data)
    ratio = in_rate / out_rate
    n_out = max(1, int(n_in / ratio))
    samples_out: list[int] = []
    for i in range(n_out):
        src = i * ratio
        lo = int(src)
        hi = min(lo + 1, n_in - 1)
        frac = src - lo
        val = int(samples_in[lo] * (1 - frac) + samples_in[hi] * frac)
        samples_out.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{n_out}h", *samples_out)


class PinchTranslatorService(AIService):
    """Pipecat ``AIService`` that performs real-time speech-to-speech translation via Pinch.

    Audio flow::

        InputAudioRawFrame
            └─ resample → 48 kHz
                └─ push into Pinch LiveKit room (microphone track)
                    └─ receive translated audio track from Pinch
                        └─ resample → pipeline output sample rate
                            └─ OutputAudioRawFrame (downstream)

    Transcript events arrive over the LiveKit data channel and are forwarded as
    :class:`pipecat.frames.frames.TranscriptionFrame` /
    :class:`pipecat.frames.frames.InterimTranscriptionFrame`.

    Args:
        options: :class:`~pipecat.plugins.pinch.models.TranslatorOptions`
            containing source/target language and voice type.
        api_key: Pinch API key.  Falls back to the ``PINCH_API_KEY``
            environment variable when *None*.
        **kwargs: Forwarded to :class:`pipecat.services.ai_service.AIService`.

    Raises:
        ValueError: When no API key is available.
    """

    def __init__(
        self,
        options: TranslatorOptions,
        api_key: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        resolved_key = api_key or os.environ.get("PINCH_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "No Pinch API key provided.  Pass api_key= or set the PINCH_API_KEY "
                "environment variable."
            )

        self._api_key = resolved_key
        self._options = options

        self._in_sample_rate: int = _PINCH_SAMPLE_RATE
        self._out_sample_rate: int = _PINCH_SAMPLE_RATE

        self._pinch_room: lk_rtc.Room | None = None
        self._audio_source: lk_rtc.AudioSource | None = None
        self._local_track: lk_rtc.LocalAudioTrack | None = None

        self._receive_task: asyncio.Task | None = None

        self._resample_state_in: object = None
        self._resample_state_out: object = None

    async def start(self, frame: StartFrame) -> None:
        """Initialise Pinch session and connect to the LiveKit room."""
        await super().start(frame)

        self._in_sample_rate = getattr(frame, "audio_in_sample_rate", _PINCH_SAMPLE_RATE)
        self._out_sample_rate = getattr(frame, "audio_out_sample_rate", _PINCH_SAMPLE_RATE)

        try:
            session = await self._create_session()
            logger.info("Pinch session created — room: {room}", room=session.get("room_name"))
            await self._connect_with_retry(session["url"], session["token"])
            logger.info("Connected to Pinch LiveKit room.")

            self._receive_task = self.create_task(self._receive_translated_audio())

        except PinchError:
            raise
        except Exception as exc:
            await self.push_frame(ErrorFrame(str(exc)))
            raise PinchSessionError(f"Failed to start Pinch session: {exc}") from exc

    async def stop(self, frame: EndFrame) -> None:
        """Gracefully disconnect from the Pinch LiveKit room."""
        await super().stop(frame)
        await self._cleanup()

    async def cancel(self, frame: CancelFrame) -> None:
        """Immediately tear down the Pinch LiveKit room connection."""
        await super().cancel(frame)
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._receive_task is not None:
            with contextlib.suppress(Exception):
                await self.cancel_task(self._receive_task)
            self._receive_task = None

        if self._pinch_room is not None:
            with contextlib.suppress(Exception):
                await self._pinch_room.disconnect()
            self._pinch_room = None

        self._audio_source = None
        self._local_track = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            await self._send_audio_to_pinch(frame)

        await self.push_frame(frame, direction)

    async def _create_session(self) -> dict:
        """Create a Pinch translation session via the SDK."""
        from pinch import PinchClient, SessionParams
        from pinch.errors import (
            PinchAuthError as _SdkAuthError,
            PinchError as _SdkError,
            PinchRateLimitError as _SdkRateLimitError,
        )

        params = SessionParams(
            source_language=self._options.source_language,
            target_language=self._options.target_language,
            voice_type=self._options.voice_type,
        )
        try:
            client = PinchClient(api_key=self._api_key)
            session_info = await asyncio.to_thread(client.create_session, params)
        except _SdkAuthError as exc:
            raise PinchAuthError(str(exc)) from exc
        except _SdkRateLimitError as exc:
            raise PinchRateLimitError(str(exc)) from exc
        except _SdkError as exc:
            raise PinchSessionError(str(exc)) from exc

        return {
            "url": session_info.url,
            "token": session_info.token,
            "room_name": session_info.room_name,
        }

    async def _connect_with_retry(self, url: str, token: str) -> None:
        """Connect to the Pinch LiveKit room with exponential back-off retry."""
        if lk_rtc is None:
            raise PinchSessionError(
                "The 'livekit' package is not installed.  Run: pip install 'livekit>=0.11.0'"
            )

        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                room = lk_rtc.Room()

                room.on("data_received", self._on_pinch_data_received)

                await room.connect(url, token)

                audio_source = lk_rtc.AudioSource(
                    sample_rate=_PINCH_SAMPLE_RATE,
                    num_channels=_PINCH_CHANNELS,
                )
                local_track = lk_rtc.LocalAudioTrack.create_audio_track("microphone", audio_source)
                publish_opts = lk_rtc.TrackPublishOptions(
                    source=lk_rtc.TrackSource.SOURCE_MICROPHONE
                )
                await room.local_participant.publish_track(local_track, publish_opts)

                self._pinch_room = room
                self._audio_source = audio_source
                self._local_track = local_track
                return

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Pinch LiveKit connect attempt {attempt}/{total} failed: {exc}. "
                    "Retrying in {delay}s …",
                    attempt=attempt,
                    total=len(_RETRY_DELAYS),
                    exc=exc,
                    delay=delay,
                )
                await asyncio.sleep(delay)

        raise PinchSessionError(
            f"Failed to connect to Pinch LiveKit room after {len(_RETRY_DELAYS)} attempts: "
            f"{last_exc}"
        ) from last_exc

    async def _send_audio_to_pinch(self, frame: InputAudioRawFrame) -> None:
        """Resample *frame* to 48 kHz and push it into Pinch's LiveKit room."""
        if self._audio_source is None:
            return

        try:
            pcm = _resample_pcm(frame.audio, frame.sample_rate, _PINCH_SAMPLE_RATE)

            samples_per_channel = len(pcm) // 2
            audio_frame = lk_rtc.AudioFrame(
                data=pcm,
                sample_rate=_PINCH_SAMPLE_RATE,
                num_channels=_PINCH_CHANNELS,
                samples_per_channel=samples_per_channel,
            )
            await self._audio_source.capture_frame(audio_frame)
        except Exception as exc:
            logger.error("Error sending audio to Pinch: {exc}", exc=exc)

    async def _receive_translated_audio(self) -> None:
        if self._pinch_room is None or lk_rtc is None:
            return

        loop = asyncio.get_event_loop()

        subscribed_streams: list[asyncio.Task] = []

        async def _stream_track(audio_stream: lk_rtc.AudioStream) -> None:
            try:
                async for audio_event in audio_stream:
                    af: lk_rtc.AudioFrame = audio_event.frame
                    pcm = bytes(af.data)
                    resampled = _resample_pcm(pcm, af.sample_rate, self._out_sample_rate)
                    out_frame = OutputAudioRawFrame(
                        audio=resampled,
                        sample_rate=self._out_sample_rate,
                        num_channels=_PINCH_CHANNELS,
                    )
                    await self.push_frame(out_frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Error streaming translated audio from Pinch: {exc}", exc=exc)

        for participant in self._pinch_room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = pub.track
                if isinstance(track, lk_rtc.RemoteAudioTrack):
                    stream = lk_rtc.AudioStream(track)
                    task = self.create_task(_stream_track(stream))
                    subscribed_streams.append(task)

        pending_tracks: asyncio.Queue[lk_rtc.RemoteAudioTrack] = asyncio.Queue()

        def _on_track_subscribed(
            track,
            publication,
            participant,
        ) -> None:
            if isinstance(track, lk_rtc.RemoteAudioTrack):
                loop.call_soon_threadsafe(pending_tracks.put_nowait, track)

        self._pinch_room.on("track_subscribed", _on_track_subscribed)

        try:
            while True:
                try:
                    track = await asyncio.wait_for(pending_tracks.get(), timeout=1.0)
                    stream = lk_rtc.AudioStream(track)
                    task = self.create_task(_stream_track(stream))
                    subscribed_streams.append(task)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            for task in subscribed_streams:
                with contextlib.suppress(Exception):
                    await self.cancel_task(task)
            raise

    def _on_pinch_data_received(self, data_packet) -> None:
        try:
            raw: bytes = (
                data_packet.data
                if isinstance(data_packet.data, (bytes, bytearray))
                else data_packet
            )
            payload = json.loads(raw)

            event = TranscriptEvent(
                type=payload.get("type", ""),
                text=payload.get("text", ""),
                is_final=bool(payload.get("is_final", False)),
                language_detected=payload.get("language_detected", ""),
                timestamp=float(payload.get("timestamp", time.time())),
                confidence=float(payload.get("confidence", 0.0)),
            )
        except Exception as exc:
            logger.warning("Could not parse Pinch data packet: {exc}", exc=exc)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop; dropping transcript event.")
            return

        if event.is_translated:
            if event.is_final:
                frame: Frame = TranscriptionFrame(
                    text=event.text,
                    user_id="",
                    timestamp=str(event.timestamp),
                    language=event.language_detected,
                )
            else:
                frame = InterimTranscriptionFrame(
                    text=event.text,
                    user_id="",
                    timestamp=str(event.timestamp),
                    language=event.language_detected,
                )
            loop.create_task(self.push_frame(frame))

        elif event.is_original:
            if event.is_final:
                orig_frame: Frame = TranscriptionFrame(
                    text=event.text,
                    user_id="original",
                    timestamp=str(event.timestamp),
                    language=event.language_detected,
                )
            else:
                orig_frame = InterimTranscriptionFrame(
                    text=event.text,
                    user_id="original",
                    timestamp=str(event.timestamp),
                    language=event.language_detected,
                )
            loop.create_task(self.push_frame(orig_frame))
