"""Execute a trusted Python script inside a Windows kill-on-close Job Object.

This bootstrap runs under the *configured* Python interpreter, preserving its
virtualenv dependencies. It assigns its own process to a Job Object before
executing user code, avoiding the race where a short-lived parent exits before
the Flow runner can discover and terminate its descendants.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import runpy
import sys


def _create_kill_on_close_job() -> int:
    if sys.platform != "win32":
        raise RuntimeError("Windows process bootstrap can run only on Windows.")

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
            ("ReadTransferCountExtra", ctypes.c_uint64),
        ]

    # IO_COUNTERS has exactly six 64-bit counters. Avoid relying on
    # platform-dependent aliases in ctypes.wintypes.
    class ACTUAL_IO_COUNTERS(ctypes.Structure):
        _fields_ = [(field, ctypes.c_uint64) for field in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", ACTUAL_IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    )
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def main() -> None:
    if len(sys.argv) < 2:
        raise ValueError("Python script path argument is required.")
    # Keep the handle alive for the entire script. Closing it on process
    # exit terminates every remaining descendant belonging to the job.
    global _JOB_HANDLE
    _JOB_HANDLE = _create_kill_on_close_job()
    script = str(Path(sys.argv[1]).resolve(strict=True))
    sys.argv = [script, *sys.argv[2:]]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
