# WorkLog 补报脚本（登录后自动补漏）
# 检查最近 7 天：哪天的日报缺失就先分析该天剩余截图、再补生成日报；
# 上周五的周报若缺失也一并补。机器在 21:00 关机导致漏报时，下次开机自动痊愈。
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
if (-not $py) { exit 1 }

$script = Join-Path $Root 'claude_auto_report_code_minimax.py'
$logDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$out = Join-Path $logDir 'catchup.out.log'
"===== 补报检查 {0} =====" -f (Get-Date) | Set-Content -LiteralPath $out -Encoding UTF8

function Invoke-Py([string[]]$argList) {
  $pinfo = Start-Process -FilePath $py -ArgumentList (@(('"{0}"' -f $script)) + $argList) `
    -WorkingDirectory $Root -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput "$logDir\catchup.step.log" -RedirectStandardError "$logDir\catchup.step.err.log"
  Get-Content "$logDir\catchup.step.log" -EA SilentlyContinue | Add-Content -LiteralPath $out -Encoding UTF8
}

$fixed = @()

# —— 1) 最近 7 天的日报补漏（不含今天；空数据日 Python 会自行跳过不生成）——
for ($i = 7; $i -ge 1; $i--) {
  $d = (Get-Date).AddDays(-$i).ToString('yyyy-MM-dd')
  $rp = Join-Path $Root ("reports\daily-report-{0}.md" -f $d)
  if (-not (Test-Path $rp)) {
    "补 {0} ..." -f $d | Add-Content -LiteralPath $out -Encoding UTF8
    Invoke-Py @('--analyze-phase', $d)
    Invoke-Py @('--report', $d)
    if (Test-Path $rp) { $fixed += "日报 $d" }
  }
}

# —— 2) 上一个已结束的周五周报补漏（本周五 21:30 前不算缺）——
$now = Get-Date
$fri = $now.Date
while ($fri.DayOfWeek -ne 'Friday') { $fri = $fri.AddDays(-1) }
if ($fri -eq $now.Date -and $now -lt (Get-Date -Hour 21 -Minute 30 -Second 0)) { $fri = $fri.AddDays(-7) }
$mon = $fri.AddDays(-4)
$wp = Join-Path $Root ("reports\weekly-report-{0}_to_{1}.md" -f $mon.ToString('yyyy-MM-dd'), $fri.ToString('yyyy-MM-dd'))
if (-not (Test-Path $wp)) {
  $weekDailies = 0
  for ($d = $mon; $d -le $fri; $d = $d.AddDays(1)) {
    if (Test-Path (Join-Path $Root ("reports\daily-report-{0}.md" -f $d.ToString('yyyy-MM-dd')))) { $weekDailies++ }
  }
  if ($weekDailies -gt 0) {
    "补周报 {0}~{1} ..." -f $mon.ToString('MM-dd'), $fri.ToString('MM-dd') | Add-Content -LiteralPath $out -Encoding UTF8
    Invoke-Py @('--weekly-report', $fri.ToString('yyyy-MM-dd'))
    if (Test-Path $wp) { $fixed += ("周报 {0}~{1}" -f $mon.ToString('MM-dd'), $fri.ToString('MM-dd')) }
  }
}

# —— 3) 有补报才弹窗告知 ——
if ($fixed.Count -gt 0) {
  $shell = New-Object -ComObject WScript.Shell
  $shell.Popup(("已自动补生成：`n{0}`n`n文件在 reports 文件夹。" -f ($fixed -join "`n")), 30, 'WorkLog 补报完成', 64) | Out-Null
}
"完成，补了 {0} 份" -f $fixed.Count | Add-Content -LiteralPath $out -Encoding UTF8
