' Runs the given .bat with a hidden window (no console flash).
' Usage: wscript.exe run_hidden.vbs "C:\path\to\script.bat"
'         cscript.exe //nologo run_hidden.vbs "C:\path\to\script.bat"
' Waits for completion and passes the exit code through, so Task Scheduler's
' LastTaskResult still reflects the batch outcome.
'
' cmd /c call 필수: .bat 을 직접 Run 하면 일부 환경에서 ERRORLEVEL 이 0 으로
' 떨어지는 경우가 있다(실패해도 스케줄러가 성공으로 찍힘).
If WScript.Arguments.Count < 1 Then WScript.Quit 1
Dim sh, cmd, rc
Set sh = CreateObject("WScript.Shell")
cmd = "cmd.exe /c call """ & WScript.Arguments(0) & """"
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
