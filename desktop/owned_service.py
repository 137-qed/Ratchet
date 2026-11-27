"""Exec an app-owned service that must exit with its desktop parent."""

import ctypes
import os
import signal
import sys


PR_SET_PDEATHSIG = 1


def main():
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent:
        return 1
    command = sys.argv[2:]
    os.execvp(command[0], command)


if __name__ == '__main__':
    sys.exit(main())
