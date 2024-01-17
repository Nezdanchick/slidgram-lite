"""
Converts telegram formatted text to XEP-0393 (message styling) strings.
"""
from dataclasses import dataclass
from typing import Optional

from aiotdlib import api as tgapi
from slidge_style_parser import format_for_telegram  # type:ignore

PARSER_TO_ENTITY = {
    "italics": tgapi.TextEntityTypeItalic,
    "bold": tgapi.TextEntityTypeBold,
    "strikethrough": tgapi.TextEntityTypeStrikethrough,
    "pre": tgapi.TextEntityTypeCode,
    "code": tgapi.TextEntityTypeCode,
    "spoiler": tgapi.TextEntityTypeSpoiler,
}


@dataclass
class Style:
    type_: str
    offset: int
    length: int
    lang: str

    def to_entity(self):
        if self.lang:
            type_ = tgapi.TextEntityTypePreCode(language=self.lang)
        else:
            type_ = PARSER_TO_ENTITY.get(self.type_, tgapi.TextEntityTypeCode)()
        return tgapi.TextEntity(type_=type_, offset=self.offset, length=self.length)


def formatted_text_to_xep_0393(
    t: tgapi.FormattedText,
    user_id: Optional[int] = None,
    user_nick: Optional[str] = None,
):
    return entities_to_xep_0393(t.text, t.entities, user_id, user_nick)


def to_formatted_text(t: str) -> tgapi.FormattedText:
    text, blocks = format_for_telegram(t)
    return tgapi.FormattedText(
        text=text,
        entities=[Style(*b).to_entity() for b in blocks],
    )


def to_xep_0393(
    t: bytes,
    entity: Optional[tgapi.TextEntity] = None,
    user_id: Optional[int] = None,
    user_nick: Optional[str] = None,
):
    if not entity:
        return t

    type_ = type(entity.type_)
    surround = _STYLING_SURROUNDS.get(type_)
    if surround:
        return surround + t + surround

    if type_ is tgapi.TextEntityTypePreCode:
        return (
            f"\n```{entity.type_.language}\n".encode("utf-16-le") + t + CODE_BLOCK_TERM
        )

    if type_ is tgapi.TextEntityTypeTextUrl:
        return t + f"<{entity.type_.url}>".encode("utf-16-le")

    if (
        type_ is tgapi.TextEntityTypeMentionName
        and entity.type_.user_id == user_id
        and user_nick
    ):
        return user_nick.encode("utf-16-le")

    return t


def merge_consecutive_entities(entities: list[tgapi.TextEntity]):
    result = []
    i = 0
    while i < len(entities):
        j = i
        add = 0
        while j < len(entities) - 1:
            e1 = entities[j]
            e2 = entities[j + 1]
            if e1.type_ == e2.type_ and e1.offset + e1.length == e2.offset:
                j += 1
                add += e2.length
            else:
                break
        entities[i].length += add
        result.append(entities[i])
        i = j + 1

    return result


def entities_to_xep_0393(
    text: str,
    entities: list[tgapi.TextEntity],
    user_id: Optional[int] = None,
    user_nick: Optional[str] = None,
):
    if not entities:
        return text

    # when there is nesting, telegram split entities, but we want to
    # avoid "_this__*is bold*__nested in italic_"

    # the split similar entities are not guaranteed to be consecutive,
    # so we first regroup by ID

    entities = sorted(entities, key=lambda x: x.type_.ID)

    # then we merge and sort by offset because our converter requires that
    entities = sorted(merge_consecutive_entities(entities), key=lambda x: x.offset)

    text_utf16 = text.encode("utf-16-le")
    for e in entities:
        e.offset *= 2
        e.length *= 2
    res_utf16 = entities_to_xep_0393_utf_16(text_utf16, entities, user_id, user_nick)

    return res_utf16.decode("utf-16-le")


def entities_to_xep_0393_utf_16(
    text: bytes,
    entities: list[tgapi.TextEntity],
    user_id: Optional[int] = None,
    user_nick: Optional[str] = None,
):
    result = b""
    index = 0
    while entities:
        entity = entities.pop(0)

        if (
            isinstance(entity.type_, tgapi.TextEntityTypePre)
            and NEW_LINE_UTF_16 in text
        ):
            # telegram allows new lines in preformatted blocks, but
            # XEP-0393 requires ``` instead of ` for that
            entity.type_ = tgapi.TextEntityTypePreCode(language="")

        offset = entity.offset
        length = entity.length
        end = offset + length

        before = text[index:offset]
        result += to_xep_0393(before)

        inside_entities = []
        while entities and entities[0].offset < end:
            inside_entities.append(entities.pop(0))

        for inside_entity in inside_entities:
            inside_entity.offset -= offset

        match = text[offset:end]
        match_md = entities_to_xep_0393_utf_16(match, inside_entities)
        result += to_xep_0393(match_md, entity, user_id, user_nick)
        index = end

    after = text[index:]
    result += to_xep_0393(after)

    return result


_STYLING_SURROUNDS = {
    tgapi.TextEntityTypeItalic: "_".encode("utf-16-le"),
    tgapi.TextEntityTypeBold: "*".encode("utf-16-le"),
    tgapi.TextEntityTypeStrikethrough: "~".encode("utf-16-le"),
    tgapi.TextEntityTypeCode: "`".encode("utf-16-le"),
}
CODE_BLOCK_TERM = "\n```\n".encode("utf-16-le")
NEW_LINE_UTF_16 = "\n".encode("utf-16-le")
