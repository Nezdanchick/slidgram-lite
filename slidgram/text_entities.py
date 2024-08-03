"""
Converts telegram formatted text to XEP-0393 (message styling) strings.
"""

from pyrogram.enums import MessageEntityType
from pyrogram.types import MessageEntity
from slidge.util.types import Mention
from slidge_style_parser import format_for_telegram  # type:ignore

_STYLING_SURROUNDS = {
    MessageEntityType.ITALIC: "_".encode("utf-16-le"),
    MessageEntityType.BOLD: "*".encode("utf-16-le"),
    MessageEntityType.STRIKETHROUGH: "~".encode("utf-16-le"),
    MessageEntityType.CODE: "`".encode("utf-16-le"),
}
CODE_BLOCK_TERM = "\n```\n".encode("utf-16-le")
NEW_LINE_UTF_16 = "\n".encode("utf-16-le")


def entities_to_xep_0393(
    text: str,
    entities: list[MessageEntity],
    user_id: int | None = None,
    user_nick: str | None = None,
):
    if not entities:
        return text

    # when there is nesting, telegram split entities, but we want to
    # avoid "_this__*is bold*__nested in italic_"
    #
    # # the split similar entities are not guaranteed to be consecutive,
    # # so we first regroup by ID
    #
    # entities = sorted(entities, key=lambda x: x.of.ID)

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
    entities: list[MessageEntity],
    user_id: int | None = None,
    user_nick: str | None = None,
):
    result = b""
    index = 0
    while entities:
        entity = entities.pop(0)

        offset = entity.offset
        length = entity.length
        end = offset + length

        if (
            entity.type == MessageEntityType.CODE
            and NEW_LINE_UTF_16 in text[offset:end]
        ):
            # telegram allows new lines in preformatted blocks, but
            # XEP-0393 requires ``` instead of ` for that
            entity.type = MessageEntityType.PRE

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


def to_xep_0393(
    t: bytes,
    entity: MessageEntity | None = None,
    user_id: int | None = None,
    user_nick: str | None = None,
):
    if not entity:
        return t

    type_ = entity.type
    surround = _STYLING_SURROUNDS.get(type_)
    if surround:
        return surround + t + surround

    if type_ == MessageEntityType.PRE:
        return f"\n```{entity.language}\n".encode("utf-16-le") + t + CODE_BLOCK_TERM

    if type_ in (MessageEntityType.TEXT_LINK, MessageEntityType.URL):
        if entity.url:
            return t + f"<{entity.url}>".encode("utf-16-le")
        return t

    if (
        type_ in (MessageEntityType.TEXT_MENTION, MessageEntityType.MENTION)
        and entity.user is not None
        and entity.user.id == user_id
        and user_nick
    ):
        return user_nick.encode("utf-16-le")

    return t


def merge_consecutive_entities(entities: list[MessageEntity]):
    result = []
    i = 0
    while i < len(entities):
        j = i
        add = 0
        while j < len(entities) - 1:
            e1 = entities[j]
            e2 = entities[j + 1]
            if e1.type == e2.type and e1.offset + e1.length == e2.offset:
                j += 1
                add += e2.length
            else:
                break
        entities[i].length += add
        result.append(entities[i])
        i = j + 1

    return result


async def styling_to_entities(
    text: str, mentions: list[Mention] | None = None
) -> tuple[str, list[MessageEntity]]:
    if mentions is None:
        mentions = []
    text, blocks = format_for_telegram(
        text, [(m.contact.name, m.start, m.end) for m in mentions]
    )
    entities = []
    for formatting, offset, length, lang in blocks:
        if formatting == "mention":
            entities.append(
                MessageEntity(
                    type=MessageEntityType.TEXT_MENTION,
                    offset=offset,
                    length=length,
                    user=await mentions.pop(0).contact.get_tg_user(),  # type:ignore
                )
            )
        elif formatting in ("code", "pre"):
            entities.append(
                MessageEntity(
                    type=MessageEntityType.PRE if lang else MessageEntityType.CODE,
                    offset=offset,
                    length=length,
                    language=lang or None,  # type:ignore
                )
            )
        else:
            entities.append(
                MessageEntity(
                    type=PARSER_TO_ENTITY[formatting],
                    offset=offset,
                    length=length,
                )
            )
    return text, entities


PARSER_TO_ENTITY = {
    "italics": MessageEntityType.ITALIC,
    "bold": MessageEntityType.BOLD,
    "strikethrough": MessageEntityType.STRIKETHROUGH,
    "spoiler": MessageEntityType.SPOILER,
}
