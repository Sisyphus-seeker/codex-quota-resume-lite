param([switch]$Purge)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$root = [IO.Directory]::GetParent([IO.Directory]::GetParent($PSScriptRoot).FullName).FullName
$claim = Get-Content -LiteralPath (Join-Path $root '.quota-resume-lite.json') -Raw | ConvertFrom-Json
if ($claim.product -ne 'quota-resume-lite' -or $claim.root -ne $root) { throw 'Unowned installation' }
# Refuse reparse points before copying or running an installed interpreter.
foreach ($candidate in @($root, (Join-Path $root 'app'), (Join-Path $root 'runtime'))) {
    $item = Get-Item -LiteralPath $candidate -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked installation refused' }
}
foreach ($tree in @((Join-Path $root 'app'), (Join-Path $root 'runtime'))) {
    $items = @(Get-ChildItem -LiteralPath $tree -Recurse -Force)
    if ($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Linked payload refused' }
}
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$temporary = Join-Path $tempBase ('QuotaResumeLite-Uninstall-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $root 'runtime') -Destination (Join-Path $temporary 'runtime') -Recurse
    Copy-Item -LiteralPath (Join-Path $root 'app') -Destination (Join-Path $temporary 'app') -Recurse
    $arguments = @((Join-Path $temporary 'app\scripts\manage_install.py'),'uninstall','--root',$root)
    if ($Purge) { $arguments += '--purge' }
    & (Join-Path $temporary 'runtime\python.exe') @arguments
    $code = $LASTEXITCODE
} finally {
    $resolved = [IO.Path]::GetFullPath($temporary)
    if ([IO.Path]::GetDirectoryName($resolved) -eq $tempBase -and [IO.Path]::GetFileName($resolved) -match '^QuotaResumeLite-Uninstall-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
exit $code
