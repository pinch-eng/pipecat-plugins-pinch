from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport

# Your plugin 
from pipecat.plugins.pinch import PinchTranslatorService, TranslatorOptions

load_dotenv()

#Pretty console logging 
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")


# A tiny FrameProcessor that prints transcripts as they arrive
class TranscriptPrinter(FrameProcessor):
    """Sits at the end of the pipeline and prints every transcript frame."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            tag = "TRANSLATED ✓" if frame.user_id == "" else "ORIGINAL   ✓"
            print(f"\n[{tag}] [{frame.language}] {frame.text}\n", flush=True)

        elif isinstance(frame, InterimTranscriptionFrame):
            tag = "TRANSLATED …" if frame.user_id == "" else "ORIGINAL   …"
            print(f"[{tag}] {frame.text}", end="\r", flush=True)

        await self.push_frame(frame, direction)


# Main 
async def main() -> None:
    livekit_url = os.environ.get("LIVEKIT_URL")
    livekit_token = os.environ.get("LIVEKIT_TOKEN")

    if not livekit_url or not livekit_token:
        print(
            "\n  Missing environment variables.\n"
            "    Make sure your .env has:\n"
            "      LIVEKIT_URL=wss://your-project.livekit.cloud\n"
            "      LIVEKIT_TOKEN=eyJ...\n"
            "      PINCH_API_KEY=pk_...\n"
        )
        sys.exit(1)

    #Transport: LiveKit
    transport = LiveKitTransport(
        url=livekit_url,
        token=livekit_token,
        room_name="pinch-test-room",
        params=LiveKitParams(
            audio_in_enabled=True,   # receive mic audio from the room
            audio_out_enabled=True,  # publish translated audio back to the room
        ),
    )

    #Pinch plugin
    pinch = PinchTranslatorService(
        options=TranslatorOptions(
            source_language="en-US",
            target_language="es-ES",
            voice_type="clone",
        )
        # PINCH_API_KEY is read from environment automatically
    )

    # Transcript logger
    printer = TranscriptPrinter()
    pipeline = Pipeline(
        [
            transport.input(),  # audio frames come in from the LiveKit room
            pinch,              # your plugin: translates audio, emits transcripts
            printer,            # prints TranscriptionFrames to console
            transport.output(), # translated audio goes back out to the LiveKit room
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    # Lifecycle events 
    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        logger.info("Participant joined the room — speak now!")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        logger.info("Participant left. Shutting down pipeline…")
        await task.cancel()

    #Run 
    print("\n" + "─" * 60)
    print("  Pipecat + Pinch — LiveKit E2E Test")
    print("─" * 60)
    print(f"  Room     : pinch-test-room")
    print(f"  Direction: en-US  →  es-ES")
    print(f"  Voice    : clone")
    print("─" * 60)
    print("  Join the room in a browser, speak English,")
    print("  and you should hear Spanish come back.")
    print("  Ctrl+C to stop.\n")

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())