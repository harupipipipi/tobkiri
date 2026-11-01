

from blocks._common import ok
from domain.share.store import ShareStore


def run(input_data, context=None):
    return ok({"shares": ShareStore().list()})
