from pyrogram.types import MessageEntity
from pyrogram.enums import MessageEntityType

from slidgram.text_entities import merge_consecutive_entities, entities_to_xep_0393


def test_parse_entities():
    assert (
        entities_to_xep_0393(
            "Bon erfd dsa sdf",
            [
                MessageEntity(offset=4, length=4, type=MessageEntityType.ITALIC),
                MessageEntity(offset=9, length=3, type=MessageEntityType.BOLD),
            ],
        )
        == "Bon _erfd_ *dsa* sdf"
    )


def test_parse_nested_entities():
    assert (
        entities_to_xep_0393(
            "Bon erfd dsa sdf",
            [
                MessageEntity(offset=3, length=8, type=MessageEntityType.BOLD),
                MessageEntity(offset=4, length=4, type=MessageEntityType.ITALIC),
            ],
        )
        == "Bon* _erfd_ ds*a sdf"
    )


def test_code_block():
    assert (
        entities_to_xep_0393(
            "Example:\ndef prout():\n    print('P*O*T')\nBABY!!",
            [
                MessageEntity(
                    offset=9, length=31, type=MessageEntityType.CODE, language="python"
                ),
                MessageEntity(
                    offset=41, length=4, type=MessageEntityType.STRIKETHROUGH
                ),
            ],
        )
        == "Example:\n\n```python\ndef prout():\n    print('P*O*T')\n```\n\n~BABY~!!"
    )


def test_link():
    assert (
        entities_to_xep_0393(
            "Click this link.",
            [
                MessageEntity(
                    offset=11,
                    length=4,
                    type=MessageEntityType.URL,
                    url="http",
                ),
            ],
        )
        == "Click this link<http>."
    )


def test_merge():
    assert merge_consecutive_entities(
        [
            MessageEntity(offset=2, length=4, type=MessageEntityType.BOLD),
            MessageEntity(offset=6, length=1, type=MessageEntityType.BOLD),
            MessageEntity(offset=7, length=1, type=MessageEntityType.BOLD),
            MessageEntity(offset=12, length=15, type=MessageEntityType.BOLD),
            MessageEntity(offset=27, length=20, type=MessageEntityType.ITALIC),
        ]
    ) == [
        MessageEntity(offset=2, length=6, type=MessageEntityType.BOLD),
        MessageEntity(offset=12, length=15, type=MessageEntityType.BOLD),
        MessageEntity(offset=27, length=20, type=MessageEntityType.ITALIC),
    ]
