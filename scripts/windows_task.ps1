param([ValidateSet('install','start','remove')][string]$Action, [string]$InstallRoot)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$marker = Join-Path $root '.quota-resume-lite.json'
$claim = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
if ($claim.product -ne 'quota-resume-lite' -or $claim.root -ne $root) { throw 'Unowned installation' }
$sha = [Security.Cryptography.SHA256]::Create()
try { $digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant())) | ForEach-Object {$_.ToString('x2')}) } finally { $sha.Dispose() }
$taskName = 'CodexQuotaResumeLite-' + $digest.Substring(0,16)
$python = Join-Path $root 'runtime\pythonw.exe'
$entry = Join-Path $root 'app\scripts\quota_resume.py'
$homeFile = Join-Path $root 'config\codex-home.txt'
if (Test-Path -LiteralPath $homeFile) { $codexHome = (Get-Content -LiteralPath $homeFile -Raw).Trim() }
elseif ($env:CODEX_HOME) { $codexHome = $env:CODEX_HOME }
else { $codexHome = Join-Path $env:USERPROFILE '.codex' }
if ($root.Contains('"') -or $codexHome.Contains('"')) { throw 'Invalid path' }
$codexHome = [IO.Path]::GetFullPath($codexHome)
# Windows argv quoting doubles trailing backslashes before a closing quote.
$quotedCodexHome = '"' + ($codexHome -replace '(\\+)$', '$1$1') + '"'
$arguments = '"' + $entry + '" --state "' + (Join-Path $root 'config\state.sqlite') + '" --codex-home ' + $quotedCodexHome + ' run'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Execute -ne $python -or $existing.Actions.Arguments -ne $arguments)) { throw 'Task belongs to another installation' }
if ($Action -eq 'install') {
    $person = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $actionDef = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $person
    $principal = New-ScheduledTaskPrincipal -UserId $person -LogonType Interactive -RunLevel Limited
    $options = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $actionDef -Trigger $trigger -Principal $principal -Settings $options -Force | Out-Null
} elseif ($Action -eq 'start') {
    if (-not $existing) { throw 'Task not installed' }
    Start-ScheduledTask -TaskName $taskName
} elseif ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
