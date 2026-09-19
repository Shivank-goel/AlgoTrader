"""Application startup boundary: no production release path exists yet."""

from enum import Enum


class RunMode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


def paper_execution(mode: str) -> bool:
    """Delta's existing runtime supports paper only until release gates are wired.

    Broker endpoint selection (demo/production data) is independent of this mode.
    A typo, missing authorization or config value can never turn paper off.
    """
    if mode != RunMode.PAPER:
        raise ValueError("Only paper execution is released; live requires qualification and reviewed deployment integration")
    return True
