from ..base_provider import BaseProvider


class StubProvider(BaseProvider):
    """スタブプロバイダー。実際のAPIは呼ばず固定レスポンスを返す。"""

    def complete(self, model, messages, tools, params):
        return {
            "content": [{"type": "text", "text": "This is a stub response from the defaults AI client."}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {}
        }

    def stream(self, model, messages, tools, params):
        return [
            {"type": "content_delta", "delta": {"type": "text", "text": "This is "}},
            {"type": "content_delta", "delta": {"type": "text", "text": "a stub "}},
            {"type": "content_delta", "delta": {"type": "text", "text": "stream response."}},
            {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
        ]

    def embed(self, model, input_text):
        if isinstance(input_text, str):
            input_text = [input_text]
        embeddings = [[0.0] * 128 for _ in input_text]
        return {
            "embeddings": embeddings,
            "usage": {"input_tokens": 0, "total_tokens": 0}
        }

    def image_gen(self, model, prompt, params):
        return {
            "images": ["data:image/png;base64,STUB_IMAGE_DATA"]
        }

    def image_analyze(self, model, image, prompt):
        return {
            "text": "This is a stub image analysis response."
        }

    def transcribe(self, model, audio, params):
        return {
            "text": "This is a stub transcription response."
        }

    def tts(self, model, text, voice):
        return {
            "audio": "data:audio/mp3;base64,STUB_AUDIO_DATA"
        }
