"""Lets you run the command-line tool with `python -m podcast_studio` (same as `python -m podcast_studio.cli`)."""

import sys

from .cli import main

sys.exit(main())
