"""BuildCache implementation package.

Importing it makes sure the third-party requirements (``requirements.txt``) are
present, so every entry script can rely on them being importable.
"""

from . import deps

deps.ensure()
