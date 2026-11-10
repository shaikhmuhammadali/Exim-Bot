' EXIM Insight & Q&A Bot  --  silent always-on launcher (H.G. Hawa & Co.)
' Starts the bot in the background with NO console window, on a fixed port,
' so http://127.0.0.1:5057 is always available on this PC.
' Runs automatically at Windows login (a shortcut to this file lives in the
' Startup folder) and can also be double-clicked any time.

Option Explicit
Dim sh, botDir, pyw, port
port = "5057"
botDir = "C:\Users\Admin\OneDrive\Documents\New Projects of H.G Hawa & Co\EXIM_Insight_Bot"
pyw = "C:\Users\Admin\AppData\Local\Programs\Python\Python313\pythonw.exe"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = botDir
sh.Environment("PROCESS")("PORT") = port
' 0 = hidden window, False = do not wait (fire and forget)
sh.Run """" & pyw & """ app.py", 0, False
