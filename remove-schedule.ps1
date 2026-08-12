# 卸载 WorkLog 计划任务并停止采集
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$pidFile = Join-Path $Root '.worklog-collector.pid'
if (Test-Path $pidFile) {
  $oldPid = Get-Content -LiteralPath $pidFile | Select-Object -First 1
  if ($oldPid) { Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue }
  Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Unregister-ScheduledTask -TaskName 'WorkLog 采集 (09点启动)' -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'WorkLog 出报 (21点生成)' -Confirm:$false -ErrorAction SilentlyContinue
Write-Output '已移除 WorkLog 计划任务，并停止采集进程。'
