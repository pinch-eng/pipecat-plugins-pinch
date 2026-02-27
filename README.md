# pipecat-plugins-pinch

Real-time **speech-to-speech translation** for [Pipecat](https://github.com/pipecat-ai/pipecat) powered by [Pinch](https://startpinch.com).

---

## Installation

```bash
pip install pipecat-plugins-pinch
```

Requires Python ≥ 3.10.

---

## Prerequisites

You need a **Pinch API key**. Get one at the [developers portal](https://portal.startpinch.com/dashboard/developers).

Set it in your environment:

```bash
export PINCH_API_KEY=pk_your_key_here
```

---

## How it fits into a Pipecat pipeline

`PinchTranslatorService` is a drop-in `FrameProcessor`. It sits between your transport's input and output — receiving `InputAudioRawFrame` from the user and emitting `OutputAudioRawFrame` (translated speech) and `TranscriptionFrame` (transcripts) downstream.

```
transport.input()
      │
      ▼
PinchTranslatorService        ← translate en-US → es-ES
      │
      ├─► OutputAudioRawFrame  → transport.output()  (translated audio)
      └─► TranscriptionFrame   → your handler        (transcripts)
```

---

## Usage

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.plugins.pinch import PinchTranslatorService, TranslatorOptions

async def main():
    # Replace with your preferred Pipecat transport (Daily, LiveKit, WebSocket, etc.)
    # transport = DailyTransport(...)

    translator = PinchTranslatorService(
        options=TranslatorOptions(
            source_language="en-US",
            target_language="es-ES",
            voice_type="clone",   # preserves the speaker's voice
        ),
        # api_key="pk_..."  ← or set PINCH_API_KEY env var
    )

    pipeline = Pipeline([
        transport.input(),
        translator,
        transport.output(),
    ])

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
    await PipelineRunner().run(task)
```

The plugin handles everything internally — calling the Pinch API, managing the translation session, resampling audio, and routing frames — so you don't need to configure anything beyond `TranslatorOptions`.

---

## Configuration

### `TranslatorOptions`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `source_language` | `str` | required | code for the speaker's language (e.g. `"en-US"`) |
| `target_language` | `str` | required | code for the output language (e.g. `"es-ES"`) |
| `voice_type` | `str` | `"clone"` | Voice used for translated output: `"clone"`, `"female"`, or `"male"` |

### `PinchTranslatorService`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `options` | `TranslatorOptions` | required | Language and voice configuration |
| `api_key` | `str \| None` | `None` | Pinch API key. Falls back to `PINCH_API_KEY` env var |

---

## Supported languages

Full list of language codes: [supported languages](https://www.startpinch.com/docs/supported-languages)

---

## Transcript frames

The plugin emits standard Pipecat transcript frames for both the original and translated speech. You can consume these in any downstream `FrameProcessor`.

| Frame | `user_id` | When |
|---|---|---|
| `TranscriptionFrame` | `""` | Final **translated** transcript |
| `InterimTranscriptionFrame` | `""` | Interim translated transcript |
| `TranscriptionFrame` | `"original"` | Final **original** transcript |
| `InterimTranscriptionFrame` | `"original"` | Interim original transcript |

```python
from pipecat.frames.frames import TranscriptionFrame, InterimTranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

class MyTranscriptHandler(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.user_id == "":
            print(f"Translated: {frame.text}")

        await self.push_frame(frame, direction)
```

---

## Links

- [Pinch](https://startpinch.com)
- [Pipecat documentation](https://docs.pipecat.ai)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)

---

## License

Apache 2.0 — see [LICENSE](LICENSE).