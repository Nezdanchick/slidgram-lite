# slidgram

A
[feature-rich](https://slidge.im/docs/slidgram/main/user/features.html)
[Telegram](https://telegram.org) to
[XMPP](https://xmpp.org/) puppeteering
[gateway](https://xmpp.org/extensions/xep-0100.html), based on
[slidge](https://slidge.im) and
[Pyrofork](https://pyrofork.mayuri.my.id/main/).

[![CI pipeline status](https://ci.codeberg.org/api/badges/14064/status.svg)](https://ci.codeberg.org/repos/14064)
[![Chat](https://rooms.slidge.im:5281/muc_badge/support@rooms.slidge.im)](https://slidge.im/xmpp-web/#/guest?join=support@rooms.slidge.im)
[![PyPI package version](https://badge.fury.io/py/slidgram.svg)](https://pypi.org/project/slidgram/)




slidgram lets you chat with users of Telegram without leaving your favorite XMPP client.

## Quickstart

```sh
docker run -v /somewhere/writeable:/var/lib/slidge \  # for data persistence
    --network=host \  # easiest setup to reach your XMPP server
    codeberg.org/slidge/slidgram:latest \
    --jid telegram.example.org \  # can be whatever you want it to be
    --secret some-secret  # must match your XMPP server config
```

Use the `:latest` tag for the latest release, `:vX.X.X` for release X.X.X, and `:main`
for the bleeding edge.

If you do not like containers, other installation methods are detailed
[in the docs](https://slidge.im/docs/slidgram/main/admin/install.html).

## Documentation

Hosted on [codeberg pages](https://slidge.im/docs/slidgram/main/).

## Contributing

Contributions are **very** welcome, and we tried our best to make it easy
to start hacking on slidgram. See [CONTRIBUTING.md](./CONTRIBUTING.md).


## Similar project

[Telegabber](https://dev.narayana.im/narayana/telegabber/), similar project written in go.
