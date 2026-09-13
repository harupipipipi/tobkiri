from typing import IO

class YAMLError(Exception): ...

def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> object: ...

def safe_dump(
    data: object,
    *,
    allow_unicode: bool = ...,
    sort_keys: bool = ...,
) -> str: ...
