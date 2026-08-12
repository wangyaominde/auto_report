# WorkLog 采集启动脚本
# 触发：每天 09:00 + 用户登录；仅在 09:00（含）至 21:00（不含）时间窗内真正启动。
# 作用：后台无窗口启动「窗口活动采集」进程（pythonw --collect），并保证全局单实例。
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

# —— 时间窗口守卫：仅 09:00–21:00 之间允许启动采集 ——
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

$script  = Join-Path $Root 'claude_auto_report_code_minimax.py'
$pidFile = Join-Path $Root '.worklog-collector.pid'
$leaf    = 'claude_auto_report_code_minimax.py'

# —— 全局单实例守卫：扫描是否已有采集进程（pythonw 跑本脚本且带 --collect）——
# 不再只依赖 PID 文件，避免孤儿进程累积导致重复计时。
$existing = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$leaf*" -and $_.CommandLine -like '*--collect*' }
if ($existing) {
  Set-Content -LiteralPath $pidFile -Value (@($existing)[0].ProcessId) -Encoding Ascii
  exit 0
}

$logDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$out = Join-Path $logDir 'collector.out.log'
$err = Join-Path $logDir 'collector.err.log'

$proc = Start-Process -FilePath $py -ArgumentList @(('"{0}"' -f $script), '--collect') `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding Ascii
