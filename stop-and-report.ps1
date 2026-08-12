# WorkLog 出报脚本
# 触发：每天 21:00。作用：停止采集 → 分析末段截图 → 生成当天日报 → 弹窗提醒。
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

$script  = Join-Path $Root 'claude_auto_report_code_minimax.py'
$pidFile = Join-Path $Root '.worklog-collector.pid'
$logDir  = Join-Path $Root 'logs'
$leaf    = 'claude_auto_report_code_minimax.py'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# —— 1) 停止所有采集进程（pythonw 跑本脚本且带 --collect），避免孤儿残留 ——
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and $_.CommandLine -like "*$leaf*" -and $_.CommandLine -like '*--collect*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

# —— 目标日期：凌晨（9 点前）补跑视为给「前一天」出报，避免 21:00 关机后日期错位 ——
$nowDt = Get-Date
if ($nowDt.Hour -lt 9) { $targetDt = $nowDt.AddDays(-1) } else { $targetDt = $nowDt }
$today = $targetDt.ToString('yyyy-MM-dd')

# —— 2) 分析目标日最后一段尚未处理的截图（如 19:00–21:00） ——
$aout = Join-Path $logDir 'analyze.out.log'
$aerr = Join-Path $logDir 'analyze.err.log'
Start-Process -FilePath $py -ArgumentList @(('"{0}"' -f $script), '--analyze-phase', $today) `
  -WorkingDirectory $Root -WindowStyle Hidden -Wait `
  -RedirectStandardOutput $aout -RedirectStandardError $aerr | Out-Null

# —— 3) 生成目标日日报（综合阶段小结 + 应用时长） ——
$out = Join-Path $logDir 'report.out.log'
$err = Join-Path $logDir 'report.err.log'
Start-Process -FilePath $py -ArgumentList @(('"{0}"' -f $script), '--report', $today) `
  -WorkingDirectory $Root -WindowStyle Hidden -Wait `
  -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null

# —— 4) 弹窗提醒 ——
$reportPath = Join-Path $Root ('reports\daily-report-{0}.md' -f $today)
$shell = New-Object -ComObject WScript.Shell
if (Test-Path $reportPath) {
  $ret = $shell.Popup(("今日工作日报已生成：`n{0}`n`n是否现在打开查看？" -f $reportPath), 30, 'WorkLog 日报已生成', 4 + 64)
  if ($ret -eq 6) { Invoke-Item -LiteralPath $reportPath }
} else {
  $tail = (Get-Content -LiteralPath $out, $err -ErrorAction SilentlyContinue | Select-Object -Last 4) -join "`n"
  $shell.Popup(("今日未生成日报（可能今天没有采集到有效活动记录）。`n`n{0}" -f $tail), 20, 'WorkLog 日报未生成', 48) | Out-Null
}
