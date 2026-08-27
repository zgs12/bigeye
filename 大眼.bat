@echo off
set PYW=C:\Users\95460\AppData\Local\Programs\Python\Python310\python.exe
if exist "%PYW%" (start "" "%PYW%" "%~dp0desktop.py") else (start "" py "%~dp0desktop.py")
