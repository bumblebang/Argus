' Runs the given .bat with a hidden window (no console flash).
' Usage: wscript.exe run_hidden.vbs "C:\path\to\script.bat"
' Waits for completion and passes the exit code through, so Task Scheduler's
' LastTaskResult still reflects the batch outcome.
If WScript.Arguments.Count < 1 Then WScript.Quit 1
Dim rc
rc = CreateObject("WScript.Shell").Run("""" & WScript.Arguments(0) & """", 0, True)
WScript.Quit rc
