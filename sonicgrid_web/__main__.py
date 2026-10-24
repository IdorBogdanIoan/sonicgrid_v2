import sys

from .users import _cli

sys.exit(_cli(sys.argv[1:]))
