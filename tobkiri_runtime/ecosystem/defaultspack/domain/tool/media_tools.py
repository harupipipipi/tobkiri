from __future__ import annotations

import wave
from typing import Any

from ._agent_os_common import err, missing_dependency, ok, workspace
from .preview_tools import image_render


def pdf_extract(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    if not path:
        return err("'path' is required", "INVALID_INPUT")
    try:
        ws = workspace(context)
        target = ws.resolve(path, must_exist=True)
        raw = target.read_bytes()
        text = raw.decode("latin-1", errors="ignore")
        return ok({"path": ws.relative(target), "text": text[:100_000], "fallback": "latin1_scan"})
    except Exception as exc:
        return err(str(exc), "PDF_EXTRACT_FAILED")


def pdf_extract_tables(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    extracted = pdf_extract(arguments, context)
    data = extracted.get("widget", {}).get("data", {})
    if extracted.get("is_error"):
        return extracted
    return ok({"path": data.get("path"), "tables": [], "missing_dependency": "camelot/tabula/pymupdf optional for table extraction"})


def ocr_extract(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        import pytesseract  # type: ignore
        from PIL import Image
    except Exception:
        return missing_dependency("pytesseract", "OCR text extraction", "pip install pytesseract")
    path = str(arguments.get("path") or "")
    if not path:
        return err("'path' is required", "INVALID_INPUT")
    try:
        ws = workspace(context)
        text = pytesseract.image_to_string(Image.open(ws.resolve(path, must_exist=True)))
        return ok({"path": path, "text": text})
    except Exception as exc:
        return err(str(exc), "OCR_FAILED")


def image_convert(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    output_path = str(arguments.get("output_path") or "")
    if not path or not output_path:
        return err("'path' and 'output_path' are required", "INVALID_INPUT")
    try:
        from PIL import Image

        ws = workspace(context)
        source = ws.resolve(path, must_exist=True)
        output = ws.resolve(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.open(source).save(output)
        return ok({"path": ws.relative(output), "size": output.stat().st_size})
    except Exception as exc:
        return err(str(exc), "IMAGE_CONVERT_FAILED")


def image_resize(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    output_path = str(arguments.get("output_path") or "")
    width = int(arguments.get("width") or 512)
    height = int(arguments.get("height") or 512)
    if not path or not output_path:
        return err("'path' and 'output_path' are required", "INVALID_INPUT")
    try:
        from PIL import Image

        ws = workspace(context)
        source = ws.resolve(path, must_exist=True)
        output = ws.resolve(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.open(source).resize((width, height)).save(output)
        return ok({"path": ws.relative(output), "width": width, "height": height})
    except Exception as exc:
        return err(str(exc), "IMAGE_RESIZE_FAILED")


def image_generate_local_or_provider(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return image_render(
        {
            "text": str(arguments.get("prompt") or "Generated image"),
            "output_path": arguments.get("output_path") or "images/generated.png",
            "width": arguments.get("width") or 1024,
            "height": arguments.get("height") or 1024,
        },
        context,
    )


def audio_transcribe(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return missing_dependency("whisper.cpp or faster-whisper", "local audio transcription")


def audio_transcribe_local(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return audio_transcribe(arguments, context)


def tts_generate(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    output_path = str(arguments.get("output_path") or "audio/tts.wav")
    try:
        ws = workspace(context)
        output = ws.resolve(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 1600)
        return ok({"path": ws.relative(output), "fallback": "silent_wav", "size": output.stat().st_size})
    except Exception as exc:
        return err(str(exc), "TTS_FAILED")


def tts_generate_local(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return tts_generate(arguments, context)
