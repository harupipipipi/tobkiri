

from blocks._common import error, ok
from domain.recording.capture import RecordingCaptureService


def run(input_data, context=None):
    payload = input_data if isinstance(input_data, dict) else {}
    try:
        result = RecordingCaptureService().run(payload)
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), "RECORDING_CAPTURE_ERROR")
    if isinstance(result, dict) and result.get("is_error"):
        return error(str(result.get("message") or result.get("status") or "recording_capture failed"), str(result.get("status") or "RECORDING_CAPTURE_ERROR").upper())
    return ok(result)
