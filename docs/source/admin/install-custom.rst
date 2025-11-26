NetBSD-specific instructions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

It is possible to install slidgram on bare metal or in a sandbox.

First, install the necessary build dependencies:

.. code:: sh

    pkgin install py313-pip py313-setuptools py313-setuptools-rust py313-wheel rust rust-bin gcc14

Then, optionally, install the dependencies available with pkgin:

.. code:: sh

    pkgin install py313-uvloop py313-aiohttp py313-alembic py313-Pillow py313-configargparse py313-defusedxml py313-qrcode py313-sqlalchemy py313-aiohappyeyeballs py313-aiosignal py313-attrs py313-frozenlist py313-multidict py313-propcache py313-yarl py313-aiodns py313-brotli py313-mako py313-greenlet py313-idna py313-cffi

After this, install with ``pip``:

.. code:: sh

    PATH="$PATH:/usr/pkg/gcc14/bin" pip3.13 install slidgram
