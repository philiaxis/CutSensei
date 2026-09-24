"""Entry point used by the PyInstaller bundle."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from cutsensei.cli import main as cli_main

        sys.exit(cli_main(sys.argv[2:]))
    from cutsensei.app import main

    sys.exit(main())
