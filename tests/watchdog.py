"""Run one test suite under a deadline, so a hang fails instead of hanging.

A suite that stops forever is the least useful kind of red. It says nothing,
and the person watching eventually gives up and interrupts it, which prints a
traceback from wherever the interrupt happened to land rather than from
whatever was actually stuck. A report can then say no more than "the tests
hang on Windows", which is not enough to fix anything.

The suite runs in this process rather than a child, so its output, its exit
code and its tracebacks are its own and nothing here rewrites them. The only
addition is faulthandler's timer: when the deadline passes it dumps every
thread's stack -- the stuck one included -- and kills the process. That timer
is a thread rather than a signal, which matters because Windows has no
SIGALRM and the usual signal.alarm() guard does not exist there.

Usage:  python tests/watchdog.py SECONDS tests/test_thing.py [args...]

FEETBROWSER_TEST_TIMEOUT overrides SECONDS for a slow machine, and 0 disables
the deadline entirely for anyone stepping through a suite in a debugger.
"""
import faulthandler
import os
import runpy
import sys


def main(argv):
    if len(argv) < 3:
        sys.exit("usage: watchdog.py SECONDS SCRIPT [args...]")
    seconds = float(os.environ.get("FEETBROWSER_TEST_TIMEOUT") or argv[1])
    script = argv[2]

    # Hand the suite the argv it would have had if it were run directly.
    sys.argv = argv[2:]

    faulthandler.enable()
    if seconds > 0:
        faulthandler.dump_traceback_later(seconds, exit=True)
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main(sys.argv)
