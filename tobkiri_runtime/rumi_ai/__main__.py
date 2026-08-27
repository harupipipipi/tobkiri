"""Legacy ``python -m rumi_ai`` compatibility entrypoint."""

from tobkiri.runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
