' Double-click to start Oracle in the background, with no window, and open
' the dashboard. If Oracle is already running, it just opens the dashboard.
'
' Arguments go to `oracle.py start`: a shortcut to this file with
' --no-browser starts Oracle without opening the browser.
' Oracle writes its messages to logs\oracle.log. Stop it with Quit Oracle on
' the dashboard, or with `py oracle.py stop`.
Option Explicit

Dim sh, fso, here, python, args, port, i, waited

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
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

sh.Run python & " " & Quote(here & "\oracle.py") & " start" & args, 0, False

' Wait for the dashboard to answer, so that a failure doesn't go unnoticed.
For waited = 1 To 60
  If Answers(port) Then WScript.Quit 0
  WScript.Sleep 500
Next
MsgBox "Oracle didn't start within 30 seconds. The end of logs\oracle.log says:" & _
       vbCrLf & vbCrLf & LogTail(here & "\logs\oracle.log", 12), vbExclamation, "Oracle"
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

' The last lines of a text file.
Function LogTail(path, count)
  Dim f, lines, first, i, text
  LogTail = "(there is no log yet)"
  If Not fso.FileExists(path) Then Exit Function
  If fso.GetFile(path).Size = 0 Then Exit Function
  Set f = fso.OpenTextFile(path, 1)
  lines = Split(Replace(f.ReadAll, vbCr, ""), vbLf)
  f.Close
  first = UBound(lines) - count
  If first < 0 Then first = 0
  text = ""
  For i = first To UBound(lines)
    text = text & lines(i) & vbCrLf
  Next
  LogTail = text
End Function
