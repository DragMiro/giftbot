from dataclasses import dataclass, field

from .entities import TextPart


@dataclass(slots=True)
class GiftInfo:
    id: int
    stars: int
    title: str | None
    sold_out: bool
    emoji: str = "🎁"


@dataclass(slots=True)
class SendPlan:
    gift: GiftInfo
    recipient: str
    parts: list[TextPart]
    total_stars: int
    hide_name: bool = False
