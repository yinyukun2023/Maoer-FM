# Tolk x86 bridge

The supplied Tolk package contains only 32-bit DLLs. The application is built
with 64-bit Python, so `TolkBridge.exe` is an out-of-process 32-bit helper.
It loads the DLLs from this directory and accepts base64-encoded UTF-8 lines
on stdin. It writes `READY` to stderr only when Tolk detects a screen reader,
then `OK` or `FAIL` for each announcement. Vendor DLL stdout diagnostics are
discarded. SAPI is not enabled: no system voice is used
in place of a screen reader. The application retains its UIA live-region path
when Tolk is unavailable.

To rebuild the helper on Windows with .NET Framework 4:

```
C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe /nologo /platform:x86 /target:exe /out:TolkBridge.exe TolkBridge.cs
```

The DLLs in this directory came from the user-provided `tolk(1).zip` on
2026-09-24. The archive did not include license files. Confirm distribution
rights for these binaries before including them in a public PR or release.
Tolk's upstream API and license are documented at
https://github.com/dkager/tolk .
