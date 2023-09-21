project = "slidgram"
copyright = "2023, nicoco"
author = "nicoco"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.extlinks",
    "sphinx.ext.viewcode",
    "sphinx.ext.autodoc.typehints",
    "sphinxarg.ext",
    "autoapi.extension",
    "slidge_dev_helpers.doap",
    "slidge_dev_helpers.sphinx_config_obj",
    "sphinx_mdinclude",
]

autodoc_typehints = "description"

# Include __init__ docstrings
autoclass_content = "both"
autoapi_python_class_content = "both"

autoapi_type = "python"
autoapi_dirs = ["../../slidgram"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "slixmpp": ("https://slixmpp.readthedocs.io/en/latest/", None),
    "slidge": ("https://slidge.im/core/", None),
}

extlinks = {"xep": ("https://xmpp.org/extensions/xep-%s.html", "XEP-%s")}

html_theme = "furo"
