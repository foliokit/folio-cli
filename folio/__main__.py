"""`python -m folio`, or `python path/to/folio` straight from a checkout."""
import os
import sys

if not __package__:
    # Run as a directory, Python put the package folder itself on sys.path.
    # Swap in its parent so `folio` imports as a package.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from folio.cli import main  # noqa: E402

raise SystemExit(main())
