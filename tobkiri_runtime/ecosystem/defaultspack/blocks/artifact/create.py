

from blocks._common import error, ok
from domain.artifact.store import ArtifactStore


def run(input_data, context=None):
    content = input_data.get("content")
    title = input_data.get("title")
    artifact_type = input_data.get("type", "markdown")
    if content is None or not title:
        return error("'title' and 'content' are required", code="INVALID_INPUT")
    item = ArtifactStore().create(
        artifact_type=artifact_type,
        title=title,
        content=str(content),
        path=input_data.get("path"),
        source_task=input_data.get("source_task", ""),
    )
    return ok(item)
