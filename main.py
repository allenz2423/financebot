import asyncio
import faulthandler
import signal
import os

# Debug helper: send SIGUSR1 to the bot process to dump all thread stacks
# to /app/stacks.txt.  Useful for diagnosing where on_ready / a watchdog
# is stuck without needing py-spy.
_STACKS_PATH = os.environ.get("STACKS_PATH", "/app/stacks.txt")


def _dump_stacks(signum, frame):
    try:
        with open(_STACKS_PATH, "w") as f:
            faulthandler.dump_traceback(file=f, all_threads=True)
    except Exception:
        pass


try:
    signal.signal(signal.SIGUSR1, _dump_stacks)
except Exception:
    pass

from src.bot.commands import main

if __name__ == "__main__":
    print(" Booting Delilah OS...")
    asyncio.run(main())