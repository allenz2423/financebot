"""Speech-to-text for voice memos.

Two backends, selected by STT_BACKEND in .env:

  openai  — OpenAI Whisper API. Requires OPENAI_API_KEY. Best quality,
            no local model, pay-per-minute.
  local   — faster-whisper running on the host GPU. Requires a CUDA-capable
            machine and the model downloaded on first use. No API cost,
            fully offline after the model is cached.

The backend is chosen at import time and is fixed for the lifetime of the
process. Switching requires a container restart.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

STT_BACKEND = os.getenv("STT_BACKEND", "openai").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1").strip()
LOCAL_MODEL = os.getenv("STT_LOCAL_MODEL", "base").strip()
LOCAL_DEVICE = os.getenv("STT_LOCAL_DEVICE", "cuda").strip()
LOCAL_COMPUTE = os.getenv("STT_LOCAL_COMPUTE", "int8").strip()

_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB, Whisper's practical limit


class STTError(Exception):
    """Raised when transcription fails for any reason."""


def _validate_config() -> None:
    if STT_BACKEND not in {"openai", "local"}:
        raise STTError(
            f"Unknown STT_BACKEND '{STT_BACKEND}'. Use 'openai' or 'local'."
        )
    if STT_BACKEND == "openai" and not OPENAI_API_KEY:
        raise STTError(
            "STT_BACKEND=openai requires OPENAI_API_KEY in .env."
        )
    if STT_BACKEND == "local":
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise STTError(
                "STT_BACKEND=local requires faster-whisper. "
                "Install it or switch to the openai backend."
            ) from exc


def transcribe(audio_bytes: bytes, *, filename: str = "audio.ogg") -> str:
    """Transcribe audio bytes to text. Raises STTError on failure.

    Args:
        audio_bytes: Raw audio data from a Discord attachment.
        filename: Original filename, used to hint the format to the backend.

    Returns:
        Transcribed text, stripped of surrounding whitespace.

    Raises:
        STTBackendNotConfigured: if the selected backend is misconfigured.
        STTError: if the backend returns no usable text.
    """
    _validate_config()

    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise STTError(
            f"Audio is {len(audio_bytes)} bytes; maximum is {_MAX_AUDIO_BYTES}."
        )

    if STT_BACKEND == "openai":
        return _transcribe_openai(audio_bytes, filename)
    return _transcribe_local(audio_bytes, filename)


def _transcribe_openai(audio_bytes: bytes, filename: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise STTError("openai package is not installed.") from exc

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.audio.transcriptions.create(
            model=OPENAI_MODEL,
            file=(filename, io.BytesIO(audio_bytes)),
            response_format="text",
        )
    except Exception as exc:
        raise STTError(f"OpenAI Whisper request failed: {type(exc).__name__}: {exc}") from exc

    text = (response if isinstance(response, str) else getattr(response, "text", "") or "").strip()
    if not text:
        raise STTError("Whisper returned an empty transcription.")
    return text


def _transcribe_local(audio_bytes: bytes, filename: str) -> str:
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(LOCAL_MODEL, device=LOCAL_DEVICE, compute_type=LOCAL_COMPUTE)
    except Exception as exc:
        raise STTError(
            f"Failed to load faster-whisper model '{LOCAL_MODEL}' on "
            f"'{LOCAL_DEVICE}': {type(exc).__name__}: {exc}"
        ) from exc

    # faster-whisper wants a file path; write the bytes to a temp file.
    import tempfile

    suffix = os.path.splitext(filename)[1] or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        segments, _info = model.transcribe(tmp_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as exc:
        raise STTError(f"faster-whisper transcription failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not text:
        raise STTError("Local Whisper returned an empty transcription.")
    return text


__all__ = ["transcribe", "STT_BACKEND", "STTError"]