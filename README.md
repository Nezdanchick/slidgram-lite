# slidgram

A
[feature-rich](https://slidge.im/docs/slidgram/main/features.html)
[Telegram](https://telegram.org) to
[XMPP](https://xmpp.org/) puppeteering
[gateway](https://xmpp.org/extensions/xep-0100.html), based on
[slidge](https://slidge.im) and
[Pyrofork](https://pyrofork.mayuri.my.id/main/).

[![PyPI package version](https://badge.fury.io/py/slidgram.svg)](https://pypi.org/project/slidgram/)
[![CI pipeline status](https://ci.codeberg.org/api/badges/14064/status.svg)](https://ci.codeberg.org/repos/14064)
[![Chat](https://conference.nicoco.fr:5281/muc_badge/slidge@conference.nicoco.fr)](https://slidge.im/xmpp-web/#/guest?join=slidge@conference.nicoco.fr)


## Installation

Refer to the [slidge admin documentation](https://slidge.im/docs/slidge/main/admin/)
for general info on how to set up an XMPP server component.

### Containers

From [the codeberg package registry](https://codeberg.org/slidge/-/packages/container/slidgram/latest)

```sh
docker run codeberg.org/slidge/slidgram  # works with podman too
```

Use the `:latest` tag for the latest release, `:vX.X.X` for release X.X.X, and `:main`
for the bleeding edge.

### Python package

With [pipx](https://pypa.github.io/pipx/):

```sh

# for the latest stable release (if any)
pipx install slidgram

# for the bleeding edge
pipx install slidgram \
    --pip-args='--extra-index-url https://codeberg.org/api/packages/slidge/pypi/simple/'

slidgram --help
```

## Documentation

Hosted on [codeberg pages](https://slidge.im/docs/slidgram/main/).

## Dev

```sh
git clone https://codeberg.org/slidge/slidgram
cd slidgram
docker-compose up  # works with podman-compose too
```

## Similar project

[Telegabber](https://dev.narayana.im/narayana/telegabber/), similar project written in go.
