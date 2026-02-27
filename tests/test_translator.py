"""Comprehensive tests for pipecat-plugins-pinch.

Tests are organised into the following sections:
  1.  TranslatorOptions validation
  2.  TranscriptEvent properties
  3.  PinchTranslatorService.__init__ (API-key handling)
  4.  _create_session  (HTTP behaviour, mocked with aiohttp)
  5.  process_frame    (frame routing / pass-through)
  6.  _resample_pcm    (resampler utility)
  7.  Error hierarchy  (exception class relationships)
  8.  _on_pinch_data_received  (transcript parsing → Pipecat frames)
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_livekit_stub() -> None:
    """Insert a minimal livekit.rtc stub into sys.modules."""
    livekit_mod = types.ModuleType("livekit")
    rtc_mod = types.ModuleType("livekit.rtc")

    class _Room:
        def __init__(self):
            self._handlers: dict = {}
            self.remote_participants: dict = {}
            self.local_participant = _LocalParticipant()

        def on(self, event, cb=None):
            if cb is None:
                def _dec(f):
                    self._handlers[event] = f
                    return f

                return _dec
            self._handlers[event] = cb

        async def connect(self, url, token):
            pass

        async def disconnect(self):
            pass

    class _LocalParticipant:
        async def publish_track(self, track, options=None):
            pass

    class _AudioSource:
        def __init__(self, sample_rate=48000, num_channels=1):
            self.sample_rate = sample_rate
            self.num_channels = num_channels

        async def capture_frame(self, frame):
            pass

    class _LocalAudioTrack:
        @staticmethod
        def create_audio_track(name, source):
            return _LocalAudioTrack()

    class _RemoteAudioTrack:
        pass

    class _AudioFrame:
        def __init__(self, data, sample_rate, num_channels, samples_per_channel):
            self.data = data
            self.sample_rate = sample_rate
            self.num_channels = num_channels
            self.samples_per_channel = samples_per_channel

    class _AudioStream:
        def __init__(self, track):
            self._track = track

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _TrackPublishOptions:
        def __init__(self, source=None):
            self.source = source

    class _TrackSource:
        SOURCE_MICROPHONE = "microphone"

    rtc_mod.Room = _Room
    rtc_mod.AudioSource = _AudioSource
    rtc_mod.LocalAudioTrack = _LocalAudioTrack
    rtc_mod.RemoteAudioTrack = _RemoteAudioTrack
    rtc_mod.AudioFrame = _AudioFrame
    rtc_mod.AudioStream = _AudioStream
    rtc_mod.TrackPublishOptions = _TrackPublishOptions
    rtc_mod.TrackSource = _TrackSource

    livekit_mod.rtc = rtc_mod
    sys.modules.setdefault("livekit", livekit_mod)
    sys.modules.setdefault("livekit.rtc", rtc_mod)


_make_livekit_stub()


def _make_pipecat_stubs() -> None:
    frames_mod = types.ModuleType("pipecat.frames.frames")

    class Frame:
        pass

    class AudioRawFrame(Frame):
        def __init__(self, audio=b"", sample_rate=16000, num_channels=1):
            self.audio = audio
            self.sample_rate = sample_rate
            self.num_channels = num_channels

    class InputAudioRawFrame(AudioRawFrame):
        pass

    class OutputAudioRawFrame(AudioRawFrame):
        pass

    class StartFrame(Frame):
        def __init__(self, audio_in_sample_rate=16000, audio_out_sample_rate=16000):
            self.audio_in_sample_rate = audio_in_sample_rate
            self.audio_out_sample_rate = audio_out_sample_rate

    class EndFrame(Frame):
        pass

    class CancelFrame(Frame):
        pass

    class ErrorFrame(Frame):
        def __init__(self, error=""):
            self.error = error

    class TranscriptionFrame(Frame):
        def __init__(self, text="", user_id="", timestamp="", language=""):
            self.text = text
            self.user_id = user_id
            self.timestamp = timestamp
            self.language = language

    class InterimTranscriptionFrame(Frame):
        def __init__(self, text="", user_id="", timestamp="", language=""):
            self.text = text
            self.user_id = user_id
            self.timestamp = timestamp
            self.language = language

    class UserStartedSpeakingFrame(Frame):
        pass

    class UserStoppedSpeakingFrame(Frame):
        pass

    for cls in (
        Frame,
        AudioRawFrame,
        InputAudioRawFrame,
        OutputAudioRawFrame,
        StartFrame,
        EndFrame,
        CancelFrame,
        ErrorFrame,
        TranscriptionFrame,
        InterimTranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    ):
        setattr(frames_mod, cls.__name__, cls)

    sys.modules.setdefault("pipecat.frames.frames", frames_mod)

    fp_mod = types.ModuleType("pipecat.processors.frame_processor")

    class FrameDirection:
        DOWNSTREAM = "downstream"
        UPSTREAM = "upstream"

    fp_mod.FrameDirection = FrameDirection
    fp_mod.FrameProcessor = object
    sys.modules.setdefault("pipecat.processors.frame_processor", fp_mod)

    svc_mod = types.ModuleType("pipecat.services.ai_service")

    class AIService:
        """Minimal AIService stub for testing."""

        def __init__(self, **kwargs):
            self._pushed_frames: list = []
            self._tasks: list = []

        async def start(self, frame):
            pass

        async def stop(self, frame):
            pass

        async def cancel(self, frame):
            pass

        async def process_frame(self, frame, direction):
            pass

        async def push_frame(self, frame, direction=None):
            self._pushed_frames.append(frame)

        def create_task(self, coro):
            task = asyncio.ensure_future(coro)
            self._tasks.append(task)
            return task

        async def cancel_task(self, task):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    svc_mod.AIService = AIService
    sys.modules.setdefault("pipecat.services.ai_service", svc_mod)

    loguru_mod = types.ModuleType("loguru")

    class _Logger:
        def bind(self, **_):
            return self

        def info(self, *a, **kw):
            pass

        def debug(self, *a, **kw):
            pass

        def warning(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

    loguru_mod.logger = _Logger()
    sys.modules.setdefault("loguru", loguru_mod)


_make_pipecat_stubs()

from pipecat.plugins.pinch.models import TranscriptEvent, TranslatorOptions  # noqa: E402
from pipecat.plugins.pinch.translator import (  # noqa: E402
    PinchAuthError,
    PinchError,
    PinchRateLimitError,
    PinchSessionError,
    PinchTranslatorService,
    _resample_pcm,
)

_frames = sys.modules["pipecat.frames.frames"]
InputAudioRawFrame = _frames.InputAudioRawFrame
OutputAudioRawFrame = _frames.OutputAudioRawFrame
StartFrame = _frames.StartFrame
EndFrame = _frames.EndFrame
CancelFrame = _frames.CancelFrame
ErrorFrame = _frames.ErrorFrame
TranscriptionFrame = _frames.TranscriptionFrame
InterimTranscriptionFrame = _frames.InterimTranscriptionFrame
FrameDirection = sys.modules["pipecat.processors.frame_processor"].FrameDirection


def _make_pcm(n_samples: int = 160, value: int = 1000) -> bytes:
    """Return *n_samples* 16-bit LE PCM samples all set to *value*."""
    return struct.pack(f"<{n_samples}h", *([value] * n_samples))


def _make_service(**kwargs) -> PinchTranslatorService:
    """Create a PinchTranslatorService with test defaults."""
    opts = kwargs.pop(
        "options",
        TranslatorOptions(source_language="en-US", target_language="es-ES"),
    )
    return PinchTranslatorService(options=opts, api_key=kwargs.pop("api_key", "test-key"), **kwargs)


# ===========================================================================
# 1.  TranslatorOptions validation
# ===========================================================================


class TestTranslatorOptions:
    def test_valid_clone(self):
        opts = TranslatorOptions("en-US", "es-ES", "clone")
        assert opts.voice_type == "clone"

    def test_valid_female(self):
        opts = TranslatorOptions("en-US", "es-ES", "female")
        assert opts.voice_type == "female"

    def test_valid_male(self):
        opts = TranslatorOptions("en-US", "es-ES", "male")
        assert opts.voice_type == "male"

    def test_default_voice_type_is_clone(self):
        opts = TranslatorOptions("en-US", "fr-FR")
        assert opts.voice_type == "clone"

    def test_invalid_voice_type_raises(self):
        with pytest.raises(ValueError, match="voice_type"):
            TranslatorOptions("en-US", "es-ES", "robot")

    def test_source_and_target_stored(self):
        opts = TranslatorOptions("de-DE", "ja-JP")
        assert opts.source_language == "de-DE"
        assert opts.target_language == "ja-JP"

    def test_invalid_voice_type_message_contains_allowed_set(self):
        with pytest.raises(ValueError, match="clone"):
            TranslatorOptions("en-US", "es-ES", "alien")


# 2.  TranscriptEvent properties


class TestTranscriptEvent:
    def _make(self, type_="translated_transcript", is_final=True):
        return TranscriptEvent(
            type=type_,
            text="Hola",
            is_final=is_final,
            language_detected="es-ES",
            timestamp=1234.56,
        )

    def test_is_translated_true_for_translated_type(self):
        assert self._make("translated_transcript").is_translated is True

    def test_is_translated_false_for_original_type(self):
        assert self._make("original_transcript").is_translated is False

    def test_is_original_true_for_original_type(self):
        assert self._make("original_transcript").is_original is True

    def test_is_original_false_for_translated_type(self):
        assert self._make("translated_transcript").is_original is False

    def test_default_confidence_is_zero(self):
        evt = TranscriptEvent(
            type="translated_transcript",
            text="Hi",
            is_final=True,
            language_detected="en-US",
            timestamp=0.0,
        )
        assert evt.confidence == 0.0

    def test_is_final_stored(self):
        assert self._make(is_final=False).is_final is False

    def test_language_detected_stored(self):
        evt = self._make()
        assert evt.language_detected == "es-ES"


# 3.  PinchTranslatorService.__init__  (API-key handling)


class TestServiceInit:
    def test_raises_when_no_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PINCH_API_KEY", None)
            with pytest.raises(ValueError, match="PINCH_API_KEY"):
                PinchTranslatorService(
                    options=TranslatorOptions("en-US", "es-ES"),
                )

    def test_reads_api_key_from_env(self):
        with patch.dict(os.environ, {"PINCH_API_KEY": "env-secret"}):
            svc = PinchTranslatorService(
                options=TranslatorOptions("en-US", "es-ES"),
            )
            assert svc._api_key == "env-secret"

    def test_explicit_api_key_takes_precedence_over_env(self):
        with patch.dict(os.environ, {"PINCH_API_KEY": "env-key"}):
            svc = _make_service(api_key="explicit-key")
            assert svc._api_key == "explicit-key"

    def test_options_stored(self):
        opts = TranslatorOptions("en-US", "es-ES", "female")
        svc = _make_service(options=opts)
        assert svc._options is opts

    def test_default_sample_rates(self):
        svc = _make_service()
        assert svc._in_sample_rate == 48_000
        assert svc._out_sample_rate == 48_000


# 4.  _create_session  (mocked aiohttp)


def _mock_http_response(status: int, json_data: dict | None = None, text: str = "") -> MagicMock:
    """Return a mock that looks like an aiohttp.ClientResponse."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.headers = {}
    return resp


class _MockContextManager:
    """Async context manager wrapping a fixed response."""

    def __init__(self, response):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_):
        pass


class TestCreateSession:
    def _make_post_mock(self, probe_resp, real_resp):
        """Return a MagicMock for aiohttp.ClientSession.post that returns probe on first call
        (allow_redirects=False) and real_resp on the second call."""
        call_count = 0

        def _post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _MockContextManager(probe_resp)
            return _MockContextManager(real_resp)

        mock = MagicMock(side_effect=_post)
        return mock

    def _session_ctx(self, post_mock):
        session = MagicMock()
        session.post = post_mock
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    @pytest.mark.asyncio
    async def test_raises_pinch_auth_error_on_401(self):
        probe = _mock_http_response(200)  # no redirect
        real = _mock_http_response(401, text="Unauthorized")
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            with pytest.raises(PinchAuthError):
                await svc._create_session()

    @pytest.mark.asyncio
    async def test_raises_pinch_rate_limit_error_on_429(self):
        probe = _mock_http_response(200)
        real = _mock_http_response(429, text="Rate limited")
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            with pytest.raises(PinchRateLimitError):
                await svc._create_session()

    @pytest.mark.asyncio
    async def test_raises_pinch_session_error_on_500(self):
        probe = _mock_http_response(200)
        real = _mock_http_response(500, text="Server error")
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            with pytest.raises(PinchSessionError):
                await svc._create_session()

    @pytest.mark.asyncio
    async def test_raises_session_error_on_missing_fields(self):
        probe = _mock_http_response(200)
        real = _mock_http_response(200, json_data={"url": "wss://x", "token": "t"})
        # Missing "room_name"
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            with pytest.raises(PinchSessionError, match="room_name"):
                await svc._create_session()

    @pytest.mark.asyncio
    async def test_returns_correct_dict_on_success(self):
        payload = {"url": "wss://pinch.livekit.cloud", "token": "tok123", "room_name": "api-abc"}
        probe = _mock_http_response(200)
        real = _mock_http_response(200, json_data=payload)
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            result = await svc._create_session()
        assert result == payload

    @pytest.mark.asyncio
    async def test_follows_redirect_and_retries(self):
        """When the first probe returns a 302, the second call should target the Location URL."""
        probe = _mock_http_response(302)
        probe.headers = {"Location": "https://www.startpinch.com/api/beta1/session"}

        payload = {"url": "wss://x", "token": "t", "room_name": "r"}
        real = _mock_http_response(200, json_data=payload)

        captured_urls: list[str] = []
        call_count = 0

        def _post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_urls.append(url)
            if call_count == 1:
                return _MockContextManager(probe)
            return _MockContextManager(real)

        session = MagicMock()
        session.post = MagicMock(side_effect=_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=ctx):
            result = await svc._create_session()

        assert captured_urls[1] == "https://www.startpinch.com/api/beta1/session"
        assert result == payload

    @pytest.mark.asyncio
    async def test_raises_session_error_on_all_missing_fields(self):
        probe = _mock_http_response(200)
        real = _mock_http_response(200, json_data={})
        post_mock = self._make_post_mock(probe, real)
        svc = _make_service()
        with patch("aiohttp.ClientSession", return_value=self._session_ctx(post_mock)):
            with pytest.raises(PinchSessionError):
                await svc._create_session()


# 5.  process_frame — frame routing


class TestProcessFrame:
    @pytest.mark.asyncio
    async def test_non_audio_frames_passed_through_unchanged(self):
        svc = _make_service()
        frame = EndFrame()
        await svc.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert frame in svc._pushed_frames

    @pytest.mark.asyncio
    async def test_input_audio_frame_forwarded_downstream(self):
        svc = _make_service()
        frame = InputAudioRawFrame(audio=_make_pcm(), sample_rate=16000, num_channels=1)
        # _send_audio_to_pinch returns early when _audio_source is None
        await svc.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert frame in svc._pushed_frames

    @pytest.mark.asyncio
    async def test_output_audio_frame_forwarded_downstream(self):
        svc = _make_service()
        frame = OutputAudioRawFrame(audio=_make_pcm(), sample_rate=16000, num_channels=1)
        await svc.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert frame in svc._pushed_frames

    @pytest.mark.asyncio
    async def test_send_audio_to_pinch_called_for_input_audio(self):
        svc = _make_service()
        svc._send_audio_to_pinch = AsyncMock()
        frame = InputAudioRawFrame(audio=_make_pcm(), sample_rate=16000, num_channels=1)
        await svc.process_frame(frame, FrameDirection.DOWNSTREAM)
        svc._send_audio_to_pinch.assert_awaited_once_with(frame)

    @pytest.mark.asyncio
    async def test_send_audio_not_called_for_non_audio_frame(self):
        svc = _make_service()
        svc._send_audio_to_pinch = AsyncMock()
        frame = EndFrame()
        await svc.process_frame(frame, FrameDirection.DOWNSTREAM)
        svc._send_audio_to_pinch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_frames_all_pushed(self):
        svc = _make_service()
        frames = [EndFrame(), CancelFrame(), EndFrame()]
        for f in frames:
            await svc.process_frame(f, FrameDirection.DOWNSTREAM)
        for f in frames:
            assert f in svc._pushed_frames


# 6.  _resample_pcm


class TestResamplePcm:
    def test_same_rate_returns_input_unchanged(self):
        data = _make_pcm(160)
        assert _resample_pcm(data, 48000, 48000) == data

    def test_upsample_increases_byte_length(self):
        data = _make_pcm(160)  # 160 samples at 16 kHz
        result = _resample_pcm(data, 16000, 48000)
        # Should have ~3× the number of samples
        assert len(result) > len(data)

    def test_downsample_decreases_byte_length(self):
        data = _make_pcm(480)  # 480 samples at 48 kHz
        result = _resample_pcm(data, 48000, 16000)
        assert len(result) < len(data)

    def test_output_is_bytes(self):
        data = _make_pcm(160)
        result = _resample_pcm(data, 16000, 48000)
        assert isinstance(result, bytes)

    def test_empty_input_returns_bytes(self):
        result = _resample_pcm(b"", 16000, 48000)
        assert isinstance(result, bytes)

    def test_resample_roundtrip_is_approximate(self):
        """Up- then down-sampling should return roughly the original number of samples."""
        n = 160
        data = _make_pcm(n)
        up = _resample_pcm(data, 16000, 48000)
        down = _resample_pcm(up, 48000, 16000)
        # Allow ±5 samples of rounding error
        assert abs(len(down) // 2 - n) <= 5

# 7.  Error hierarchy

class TestErrorHierarchy:
    def test_pinch_auth_error_is_pinch_error(self):
        assert issubclass(PinchAuthError, PinchError)

    def test_pinch_rate_limit_error_is_pinch_error(self):
        assert issubclass(PinchRateLimitError, PinchError)

    def test_pinch_session_error_is_pinch_error(self):
        assert issubclass(PinchSessionError, PinchError)

    def test_pinch_error_is_exception(self):
        assert issubclass(PinchError, Exception)

    def test_auth_error_can_be_raised_and_caught_as_pinch_error(self):
        with pytest.raises(PinchError):
            raise PinchAuthError("bad key")

    def test_rate_limit_error_can_be_raised(self):
        with pytest.raises(PinchRateLimitError):
            raise PinchRateLimitError("slow down")

    def test_session_error_can_be_raised(self):
        with pytest.raises(PinchSessionError):
            raise PinchSessionError("could not connect")


# 8.  _on_pinch_data_received — transcript parsing


class TestOnPinchDataReceived:
    def _data_packet(self, payload: dict, is_bytes: bool = True):
        pkt = MagicMock()
        raw = json.dumps(payload).encode()
        pkt.data = raw if is_bytes else raw.decode()
        return pkt

    @pytest.mark.asyncio
    async def test_final_translated_pushes_transcription_frame(self):
        svc = _make_service()
        pushed: list = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        svc.push_frame = _push

        pkt = self._data_packet(
            {
                "type": "translated_transcript",
                "text": "Hola",
                "is_final": True,
                "language_detected": "es-ES",
                "timestamp": 1.0,
            }
        )
        svc._on_pinch_data_received(pkt)
        await asyncio.sleep(0)  # let ensure_future run

        assert any(isinstance(f, TranscriptionFrame) for f in pushed)

    @pytest.mark.asyncio
    async def test_interim_translated_pushes_interim_frame(self):
        svc = _make_service()
        pushed: list = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        svc.push_frame = _push

        pkt = self._data_packet(
            {
                "type": "translated_transcript",
                "text": "Hol",
                "is_final": False,
                "language_detected": "es-ES",
                "timestamp": 1.0,
            }
        )
        svc._on_pinch_data_received(pkt)
        await asyncio.sleep(0)

        assert any(isinstance(f, InterimTranscriptionFrame) for f in pushed)

    @pytest.mark.asyncio
    async def test_original_final_transcript_pushed(self):
        svc = _make_service()
        pushed: list = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        svc.push_frame = _push

        pkt = self._data_packet(
            {
                "type": "original_transcript",
                "text": "Hello",
                "is_final": True,
                "language_detected": "en-US",
                "timestamp": 2.0,
            }
        )
        svc._on_pinch_data_received(pkt)
        await asyncio.sleep(0)

        transcripts = [f for f in pushed if isinstance(f, TranscriptionFrame)]
        assert any(getattr(f, "user_id", None) == "original" for f in transcripts)

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_raise(self):
        svc = _make_service()
        pkt = MagicMock()
        pkt.data = b"not valid json {"
        # Should not raise
        svc._on_pinch_data_received(pkt)

    @pytest.mark.asyncio
    async def test_transcription_frame_text_correct(self):
        svc = _make_service()
        pushed: list = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        svc.push_frame = _push

        pkt = self._data_packet(
            {
                "type": "translated_transcript",
                "text": "Bonjour",
                "is_final": True,
                "language_detected": "fr-FR",
                "timestamp": 9.9,
            }
        )
        svc._on_pinch_data_received(pkt)
        await asyncio.sleep(0)

        tf = next(f for f in pushed if isinstance(f, TranscriptionFrame))
        assert tf.text == "Bonjour"
