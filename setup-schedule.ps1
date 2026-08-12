# 注册 WorkLog 计划任务（无需管理员；以当前用户「交互」身份运行，才能采集前台窗口与截图）
# 用法：powershell -ExecutionPolicy Bypass -File setup-schedule.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$user = "$env:USERDOMAIN\$env:USERNAME"

function New-PsArgs([string]$file) {
  return ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f (Join-Path $Root $file))
}

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
             -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# 1) 采集（窗口标题 / 时长）：每天 09:00 + 登录时
$actCollect = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'start-collector.ps1') -WorkingDirectory $Root
$trgC1 = New-ScheduledTaskTrigger -Daily -At 9:00am
$trgC2 = New-ScheduledTaskTrigger -AtLogOn -User $user
$trgC2.Delay = 'PT30S'
Register-ScheduledTask -TaskName 'WorkLog 采集 (09点启动)' -Action $actCollect -Trigger @($trgC1, $trgC2) -Principal $principal -Settings $settings -Force | Out-Null

# 2) 截图：每天 09:00 起每 5 分钟一次，持续 12 小时（到 21:00）
$actShot = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'screenshot.ps1') -WorkingDirectory $Root
$trgShot = New-ScheduledTaskTrigger -Daily -At 9:00am
$repShot = New-ScheduledTaskTrigger -Once -At 9:00am -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 12)
$trgShot.Repetition = $repShot.Repetition
Register-ScheduledTask -TaskName 'WorkLog 截图 (5分钟)' -Action $actShot -Trigger $trgShot -Principal $principal -Settings $settings -Force | Out-Null

# 3) 阶段分析：每天 11:00 起每 2 小时一次，持续 8 小时（11/13/15/17/19 点）
$actAnalyze = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'analyze-phase.ps1') -WorkingDirectory $Root
$trgAna = New-ScheduledTaskTrigger -Daily -At 11:00am
$repAna = New-ScheduledTaskTrigger -Once -At 11:00am -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Hours 8)
$trgAna.Repetition = $repAna.Repetition
Register-ScheduledTask -TaskName 'WorkLog 阶段分析 (2小时)' -Action $actAnalyze -Trigger $trgAna -Principal $principal -Settings $settings -Force | Out-Null

# 4) 出报：每天 21:00（停止采集 → 末段截图分析 → 生成日报 → 弹窗）
$actReport = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'stop-and-report.ps1') -WorkingDirectory $Root
$trgReport = New-ScheduledTaskTrigger -Daily -At 9:00pm
Register-ScheduledTask -TaskName 'WorkLog 出报 (21点生成)' -Action $actReport -Trigger $trgReport -Principal $principal -Settings $settings -Force | Out-Null

# 5) 周报：每周五 21:05（汇总本周一~周五日报 → 弹窗）
$actWeekly = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'weekly-report.ps1') -WorkingDirectory $Root
$trgWeekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 9:05pm
Register-ScheduledTask -TaskName 'WorkLog 周报 (周五21点)' -Action $actWeekly -Trigger $trgWeekly -Principal $principal -Settings $settings -Force | Out-Null

# 6) 补报：登录后 2 分钟，自动补生成最近 7 天缺失的日报与上周缺失的周报
#   （解决 21:00 关机导致出报被跳过/打断的问题）
$actCatchup = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (New-PsArgs 'catchup-reports.ps1') -WorkingDirectory $Root
$trgCatchup = New-ScheduledTaskTrigger -AtLogOn -User $user
$trgCatchup.Delay = 'PT2M'
Register-ScheduledTask -TaskName 'WorkLog 补报 (登录补漏)' -Action $actCatchup -Trigger $trgCatchup -Principal $principal -Settings $settings -Force | Out-Null

Get-ScheduledTask -TaskName 'WorkLog*' | Select-Object TaskName, State | Format-Table -AutoSize
Write-Output 'WorkLog 计划任务注册完成（共 6 个）。'
