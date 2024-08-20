# slidgram

[Home](https://sr.ht/~nicoco/slidge) |
[Docs](https://slidge.im/slidgram) |
[Issues](https://todo.sr.ht/~nicoco/slidgram) |
[Patches](https://lists.sr.ht/~nicoco/public-inbox) |
[Chat](xmpp:slidge@conference.nicoco.fr?join)

A
[feature-rich](https://slidge.im/slidgram/features.html)
[Telegram](https://telegram.org) to
[XMPP](https://xmpp.org/) puppeteering
[gateway](https://xmpp.org/extensions/xep-0100.html), based on
[slidge](https://slidge.im) and
[Pyrofork](https://pyrofork.mayuri.my.id/main/).

[![builds.sr.ht status](https://builds.sr.ht/~nicoco/slidgram/commits/master/ci.yml.svg)](https://builds.sr.ht/~nicoco/slidgram/commits/master/ci.yml)
[![containers status](https://builds.sr.ht/~nicoco/slidgram/commits/master/container.yml.svg)](https://builds.sr.ht/~nicoco/slidgram/commits/master/container.yml)
[![pypi status](https://badge.fury.io/py/slidgram.svg)](https://pypi.org/project/slidgram/)

## Installation

Refer to the [slidge admin documentation](https://slidge.im/core/admin/)
for general info on how to set up an XMPP server component.

### Containers

From [dockerhub](https://hub.docker.com/r/nicocool84/slidgram)

```sh
docker run docker.io/nicocool84/slidgram
```

### Python package

With [pipx](https://pypa.github.io/pipx/):

```sh

# for the latest stable release (if any)
pipx install slidgram

# for the bleeding edge
pipx install slidgram==0.0.0.dev0 \
    --pip-args='--extra-index-url https://slidge.im/repo'

# to update bleeding edge installs
pipx install slidgram==0.0.0.dev0 \
    --pip-args='--extra-index-url https://slidge.im/repo' --force

slidgram --help
```

## Dev

```sh
git clone https://git.sr.ht/~nicoco/slidgram
cd slidgram
docker-compose up
```

## Similar project

https://dev.narayana.im/narayana/telegabber/
