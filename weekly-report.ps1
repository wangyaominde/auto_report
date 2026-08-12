# WorkLog 周报（周五 21:05 由计划任务触发）
# 汇总本周一~周五日报 → 生成周报 → 弹窗提醒。
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

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
$py = Resolve-Python 'python.exe'
if (-not $py) { $py = Resolve-Python 'pythonw.exe' }

$script = Join-Path $Root 'claude_auto_report_code_minimax.py'
$logDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$out = Join-Path $logDir 'weekly.out.log'
$err = Join-Path $logDir 'weekly.err.log'
Start-Process -FilePath $py -ArgumentList @(('"{0}"' -f $script), '--weekly-report') `
  -WorkingDirectory $Root -WindowStyle Hidden -Wait `
  -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null

# 计算本周一~周五，定位周报文件
$today = Get-Date
$monday = $today.Date.AddDays( - (([int]$today.DayOfWeek + 6) % 7) )
$friday = $monday.AddDays(4)
$wname = "weekly-report-{0}_to_{1}.md" -f $monday.ToString('yyyy-MM-dd'), $friday.ToString('yyyy-MM-dd')
$wpath = Join-Path $Root ("reports\{0}" -f $wname)

$shell = New-Object -ComObject WScript.Shell
if (Test-Path $wpath) {
  $ret = $shell.Popup(("本周周报已生成：`n{0}`n`n是否现在打开查看？" -f $wpath), 30, 'WorkLog 周报已生成', 4 + 64)
  if ($ret -eq 6) { Invoke-Item -LiteralPath $wpath }
} else {
  $tail = (Get-Content -LiteralPath $out, $err -ErrorAction SilentlyContinue | Select-Object -Last 4) -join "`n"
  $shell.Popup(("本周周报未生成（可能本周尚无日报）。`n`n{0}" -f $tail), 20, 'WorkLog 周报未生成', 48) | Out-Null
}
