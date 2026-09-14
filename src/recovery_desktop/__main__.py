import sys
sys.dont_write_bytecode = True
from .app import main
raise SystemExit(main())
