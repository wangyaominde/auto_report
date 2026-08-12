# 启动 WorkLog GUI（无控制台窗口）
# 用法：powershell -ExecutionPolicy Bypass -File .\start-gui.ps1
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $pyw)) {
    $pyw = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}
Start-Process -FilePath $pyw -ArgumentList "`"$root\worklog_gui.py`"" -WorkingDirectory $root
