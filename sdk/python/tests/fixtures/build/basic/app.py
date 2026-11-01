"""Minimal Python Pack used by the official-wheel producer fixture."""

import wit_world


class WitWorld(wit_world.WitWorld):
    def ping(self) -> str:
        return "pong"
