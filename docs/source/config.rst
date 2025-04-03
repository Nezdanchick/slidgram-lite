Configuration
=============

Minimal setup
-------------

For a minimal ``prosody <https://prosody.im>`` based setup, add these lines at the bottom of
``/etc/prosody/prosody.cfg.lua``:

.. code-block:: lua

  Component "slidgram.example.org"
      component_secret = "secret"

And start slidgram with:

.. code-block:: bash

  slidgram \
    --jid slidgram.example.org \
    --secret secret \
    --home-dir /somewhere/writable

Advanced usage
--------------

Refer to the `slidge admin docs <https://slidge.im/docs/slidge/main/admin>`_ for more
advanced setups and examples of configuration for other XMPP servers.

You will probably want to add support for `attachments <https://slidge.im/docs/slidge/main/admin/attachments.html>`_
received from Telegram, and setup slidgram as a `privileged component <https://slidge.im/docs/slidge/main/admin/privilege.html>`_
for better UX.

slidgram-specific config
------------------------

All `generic slidge configuration options <https://slidge.im/docs/slidge/main/admin/config/#common-config>`_
apply.
slidgram provides these additional component-wide options:

.. config-obj:: slidgram.config
