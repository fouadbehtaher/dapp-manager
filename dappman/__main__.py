from .cli import main
import sys
try:
    sys.exit(main())
except ValueError as e:  # bad user input (address/hash): clean message, no traceback
    print('error:', e, file=sys.stderr)
    sys.exit(2)
