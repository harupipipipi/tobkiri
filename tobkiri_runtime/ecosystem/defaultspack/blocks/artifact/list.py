

from blocks._common import ok
from domain.artifact.store import ArtifactStore


def run(input_data, context=None):
    return ok({"artifacts": ArtifactStore().list()})
