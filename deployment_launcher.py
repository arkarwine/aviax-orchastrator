#!/usr/bin/env python3
"""Launch a deployment outside the manager's process tree on POSIX systems."""

import argparse
import os
import sys
import traceback
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--module", default="anony")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name != "posix":
        raise SystemExit("The detached deployment launcher requires a POSIX system.")

    first_child = os.fork()
    if first_child:
        _, status = os.waitpid(first_child, 0)
        return os.waitstatus_to_exitcode(status)

    os.setsid()
    deployment_pid = os.fork()
    if deployment_pid:
        Path(args.pid_file).write_text(str(deployment_pid), encoding="ascii")
        os._exit(0)

    os.chdir(args.cwd)
    with open(os.devnull, "rb") as stdin, open(args.log, "a", encoding="utf-8") as output:
        os.dup2(stdin.fileno(), 0)
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", args.module],
                os.environ,
            )
        except BaseException:
            traceback.print_exc()
            os._exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
