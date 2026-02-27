"""
End-to-end test: PinchTranslatorService with Daily transport.

How to run:
    python examples/test_daily_e2e.py

How to hear the translation:
    Open your DAILY_ROOM_URL in a browser, allow mic access, speak English.
    You should hear Spanish come back.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.plugins.pinch import PinchTranslatorService, TranslatorOptions

load_dotenv()

logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")


class TranscriptPrinter(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.user_id == "":
            print(f"\n[TRANSLATED ✓] {frame.text}\n", flush=True)
        elif isinstance(frame, TranscriptionFrame) and frame.user_id == "original":
            print(f"\n[ORIGINAL   ✓] {frame.text}\n", flush=True)
        await self.push_frame(frame, direction)


async def main() -> None:
    room_url = os.environ.get("DAILY_ROOM_URL")
    daily_api_key = os.environ.get("DAILY_API_KEY")

    if not room_url or not daily_api_key:
        print("❌  Missing DAILY_ROOM_URL or DAILY_API_KEY in .env")
        sys.exit(1)

    transport = DailyTransport(
        room_url,
        None,
        "Pinch Bot",
        DailyParams(
            api_key=daily_api_key,
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    pinch = PinchTranslatorService(
        options=TranslatorOptions(
            source_language="en-US",
            target_language="es-ES",
            voice_type="clone",
        )
    )

    printer = TranscriptPrinter()

    pipeline = Pipeline([
        transport.input(),
        pinch,
        printer,
        transport.output(),
    ])

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_participant_joined")
    async def on_joined(transport, participant):
        logger.info("✅  Participant joined — speak English!")

    @transport.event_handler("on_participant_left")
    async def on_left(transport, participant, reason):
        logger.info("Participant left. Shutting down...")
        await task.cancel()

    print("\n" + "─" * 60)
    print("  Pipecat + Pinch — Daily E2E Test")
    print("─" * 60)
    print(f"  Room     : {room_url}")
    print(f"  Direction: en-US  →  es-ES")
    print("─" * 60)
    print(f"  Open the room URL in your browser, speak English,")
    print(f"  and you should hear Spanish come back.")
    print("  Ctrl+C to stop.\n")

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
