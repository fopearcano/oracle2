' Double-click to start Oracle in the background, with no window, and open
' the dashboard. If Oracle is already running, it just opens the dashboard.
' `oracle.py autostart on` runs this file when you log in, with --no-browser.
'
' Keep this file in the oracle2 folder, next to oracle.py. To start Oracle from
' somewhere else (the desktop, the Startup folder), make a shortcut to it, not a copy.
'
' Arguments go to `oracle.py start`, for example --no-browser.
' Oracle's messages, and Python's own errors, go to logs\oracle.log.
Option Explicit

Const FIRST_OUTPUT = 60     ' seconds Python has to write its first line
Const DASHBOARD = 180       ' seconds the dashboard has to answer

Dim sh, fso, here, python, args, port, i, logPath, startSize, waited, news, failed

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

If Not fso.FileExists(here & "\oracle.py") Then
  MsgBox "This file has to stay in the oracle2 folder, next to oracle.py, but this copy is in:" & _
         vbCrLf & here & vbCrLf & vbCrLf & _
         "To start Oracle when you log in, delete this copy and run this once in the oracle2 folder:" & _
         vbCrLf & "    py oracle.py autostart on" & vbCrLf & vbCrLf & _
         "To start it from the desktop, use a shortcut to the file instead of a copy.", _
         vbExclamation, "Oracle"
  WScript.Quit 1
End If
sh.CurrentDirectory = here

' pyw and pythonw are the versions of Python that open no window.
If Found("pyw") Then
  python = "pyw -3"
ElseIf Found("pythonw") Then
  python = "pythonw"
Else
  MsgBox "Python 3 was not found. Install it from https://www.python.org/downloads/ " & _
         "and try again.", vbExclamation, "Oracle"
  WScript.Quit 1
End If

port = ConfiguredPort()
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & Quote(WScript.Arguments(i))
  If WScript.Arguments(i) = "--port" And i < WScript.Arguments.Count - 1 Then port = WScript.Arguments(i + 1)
Next

logPath = here & "\logs\oracle.log"
If Not fso.FolderExists(here & "\logs") Then fso.CreateFolder here & "\logs"
startSize = LogSize()

' Through cmd, so that Python's own errors (a crash before Oracle runs) reach the log too.
sh.Run "cmd /c """ & python & " " & Quote(here & "\oracle.py") & " start" & args & _
       " >> " & Quote(logPath) & " 2>&1""", 0, False

' Wait for the dashboard to answer, so that a failure doesn't go unnoticed.
failed = False
For waited = 1 To DASHBOARD * 2
  If Answers(port) Then WScript.Quit 0
  news = NewLog()
  If InStr(news, "Traceback (most recent call last)") > 0 Or InStr(news, "can't open file") > 0 Or _
     InStr(news, "Cannot open the dashboard") > 0 Or InStr(news, "usage: oracle.py") > 0 Then failed = True
  ' Oracle's own lines start with [oracle]; anything else after a minute is an error from Python.
  If waited >= FIRST_OUTPUT * 2 Then
    If LogSize() <= startSize Or (news <> "" And InStr(news, "[oracle]") = 0) Then failed = True
  End If
  If failed Then Exit For
  WScript.Sleep 500
Next

news = NewLog()
If LogSize() <= startSize Then
  news = "Python wrote nothing to logs\oracle.log, so it didn't start."
ElseIf news = "" Then
  news = "See logs\oracle.log in " & here
Else
  news = "logs\oracle.log says:" & vbCrLf & vbCrLf & LastLines(news, 14)
End If
MsgBox "Oracle didn't start. " & news, vbExclamation, "Oracle"
WScript.Quit 1


Function Found(program)
  Found = (sh.Run("cmd /c where /q " & program, 0, True) = 0)
End Function

Function Quote(text)
  Quote = """" & text & """"
End Function

' The dashboard port saved with `oracle.py config --port`, or 8800.
Function ConfiguredPort()
  Dim path, f, text, re, matches
  ConfiguredPort = "8800"
  path = here & "\oracle.config.json"
  If Not fso.FileExists(path) Then Exit Function
  If fso.GetFile(path).Size = 0 Then Exit Function
  Set f = fso.OpenTextFile(path, 1)
  text = f.ReadAll
  f.Close
  Set re = New RegExp
  re.Pattern = """port""\s*:\s*(\d+)"
  Set matches = re.Execute(text)
  If matches.Count > 0 Then ConfiguredPort = matches(0).SubMatches(0)
End Function

' True when an Oracle dashboard answers on this port.
Function Answers(p)
  Dim http
  Answers = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.setTimeouts 1000, 1000, 1000, 1000
  http.Open "GET", "http://127.0.0.1:" & p & "/api/status", False
  http.Send
  If Err.Number = 0 Then
    If http.Status = 200 And InStr(http.responseText, """app"": ""oracle""") > 0 Then Answers = True
  End If
  On Error GoTo 0
End Function

Function LogSize()
  LogSize = 0
  If fso.FileExists(logPath) Then LogSize = fso.GetFile(logPath).Size
End Function

' What the log gained since this script started Python ("" if it can't be read).
Function NewLog()
  Dim f
  NewLog = ""
  If LogSize() <= startSize Then Exit Function
  On Error Resume Next
  Set f = fso.OpenTextFile(logPath, 1)
  If Err.Number = 0 Then
    If startSize > 0 Then f.Skip startSize
    NewLog = f.ReadAll
    f.Close
  End If
  On Error GoTo 0
End Function

Function LastLines(text, count)
  Dim lines, first, j, out
  lines = Split(Replace(text, vbCr, ""), vbLf)
  first = UBound(lines) - count
  If first < 0 Then first = 0
  out = ""
  For j = first To UBound(lines)
    If Len(Trim(lines(j))) > 0 Then out = out & lines(j) & vbCrLf
  Next
  LastLines = out
End Function
