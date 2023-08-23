from aiotdlib import api

from slidgram.text_entities import merge_consecutive_entities, entities_to_xep_0393


def test_parse_entities():
    assert (
        entities_to_xep_0393(
            "Bon erfd dsa sdf",
            [
                api.TextEntity.construct(
                    offset=4, length=4, type_=api.TextEntityTypeItalic()
                ),
                api.TextEntity.construct(
                    offset=9, length=3, type_=api.TextEntityTypeBold()
                ),
            ],
        )
        == "Bon _erfd_ *dsa* sdf"
    )


def test_parse_nested_entities():
    assert (
        entities_to_xep_0393(
            "Bon erfd dsa sdf",
            [
                api.TextEntity.construct(
                    offset=3, length=8, type_=api.TextEntityTypeBold()
                ),
                api.TextEntity.construct(
                    offset=4, length=4, type_=api.TextEntityTypeItalic()
                ),
            ],
        )
        == "Bon* _erfd_ ds*a sdf"
    )


def test_code_block():
    assert (
        entities_to_xep_0393(
            "Example:\ndef prout():\n    print('P*O*T')\nBABY!!",
            [
                api.TextEntity.construct(
                    offset=9,
                    length=31,
                    type_=api.TextEntityTypePreCode(language="python"),
                ),
                api.TextEntity.construct(
                    offset=41, length=4, type_=api.TextEntityTypeStrikethrough()
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
                api.TextEntity.construct(
                    offset=11,
                    length=4,
                    type_=api.TextEntityTypeTextUrl(url="http"),
                ),
            ],
        )
        == "Click this link<http>."
    )


def test_merge():
    assert merge_consecutive_entities(
        [
            api.TextEntity.construct(
                offset=2,
                length=4,
                type_=api.TextEntityTypeBold(),
            ),
            api.TextEntity.construct(
                offset=6,
                length=1,
                type_=api.TextEntityTypeBold(),
            ),
            api.TextEntity.construct(
                offset=7,
                length=1,
                type_=api.TextEntityTypeBold(),
            ),
            api.TextEntity.construct(
                offset=12,
                length=15,
                type_=api.TextEntityTypeBold(),
            ),
            api.TextEntity.construct(
                offset=27,
                length=20,
                type_=api.TextEntityTypeItalic(),
            ),
        ]
    ) == [
        api.TextEntity.construct(
            offset=2,
            length=6,
            type_=api.TextEntityTypeBold(),
        ),
        api.TextEntity.construct(
            offset=12,
            length=15,
            type_=api.TextEntityTypeBold(),
        ),
        api.TextEntity.construct(
            offset=27,
            length=20,
            type_=api.TextEntityTypeItalic(),
        ),
    ]
