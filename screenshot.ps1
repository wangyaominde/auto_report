# WorkLog 截图触发（每 5 分钟由计划任务调用；仅 09:00–21:00 生效）
# 真正的截图在 Python 的 --screenshot 里完成（避免截图脚本被杀软误拦）。
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$now   = Get-Date
$open  = Get-Date -Hour 9  -Minute 0 -Second 0
$close = Get-Date -Hour 21 -Minute 0 -Second 0
if ($now -lt $open -or $now -ge $close) { exit 0 }

function Resolve-Python([string]$exe) {
  $base = Join-Path $env:LOCALAPPDATA 'Programs\Python'
  $hit = Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
         Sort-Object Name -Descending |
         ForEach-Object { Join-Path $_.FullName $exe } |
         Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($hit) { return $hit }
  $c = Get-Command $exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return $null
}
$py = Resolve-Python 'pythonw.exe'
if (-not $py) { $py = Resolve-Python 'python.exe' }
if (-not $py) { exit 1 }

$script = Join-Path $Root 'claude_auto_report_code_minimax.py'
$logDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Process -FilePath $py -ArgumentList @(('"{0}"' -f $script), '--screenshot') `
  -WorkingDirectory $Root -WindowStyle Hidden -Wait `
  -RedirectStandardOutput (Join-Path $logDir 'shot.out.log') `
  -RedirectStandardError (Join-Path $logDir 'shot.err.log') | Out-Null
