

from blocks._common import error, ok
from domain.artifact.store import ArtifactStore


def run(input_data, context=None):
    artifact_id = input_data.get("artifact_id")
    if not artifact_id:
        return error("'artifact_id' is required", code="INVALID_INPUT")
    item = ArtifactStore().get(artifact_id)
    if item is None:
        return error("Artifact not found", code="NOT_FOUND")
    return ok(item)
