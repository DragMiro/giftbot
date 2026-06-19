import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from giftsender_core import (  # noqa: E402
    EntityData,
    GiftCatalog,
    GiftInfo,
    GiftSender,
    SendPlan,
    TextPart,
    count_custom_emoji,
    entity_from_dict,
    entity_to_dict,
    merge_message_parts,
    preview_line,
    split_text,
    split_text_parts,
)

__all__ = [
    "EntityData",
    "TextPart",
    "GiftCatalog",
    "GiftSender",
    "split_text",
    "split_text_parts",
    "GiftInfo",
    "SendPlan",
    "merge_message_parts",
    "entity_to_dict",
    "entity_from_dict",
    "count_custom_emoji",
    "preview_line",
]
