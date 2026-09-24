"""Entry point used by the PyInstaller bundle.

The bundle contains two executables built from this script: ``CutSensei``
(GUI, no console) and ``cutsensei-cli`` (console).  The CLI is also reachable
with ``CutSensei --cli ...``.
"""

import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    exe = os.path.basename(sys.executable).lower()
    if exe.startswith("cutsensei-cli") or (len(sys.argv) > 1 and sys.argv[1] == "--cli"):
        args = sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] == "--cli" else sys.argv[1:]
        from cutsensei.cli import main as cli_main

        sys.exit(cli_main(args))
    from cutsensei.app import main

    sys.exit(main())
