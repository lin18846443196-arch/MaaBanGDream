param([Parameter(Mandatory = $true)][string]$SourceRoot)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$patch = Join-Path $projectRoot 'patches\yesbangdream-mfa-branding.patch'
$baseline = 'a39dcd87ba2e5098ee23072e9a015c5c36f8c8d1'
$head = (& git -C $SourceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'MFA source is not a Git checkout.' }
if ($head -ne $baseline) {
    throw "Use an isolated MFA checkout at upstream commit $baseline before applying the branding patch."
}
$changes = @(& git -C $SourceRoot status --porcelain)
if ($changes.Count -gt 0) { throw 'MFA source has existing changes; preserve them before preparing a release.' }
& git -C $SourceRoot apply --check --unidiff-zero $patch
if ($LASTEXITCODE -ne 0) { throw 'YesBanGDream MFA patch does not apply.' }
& git -C $SourceRoot apply --unidiff-zero $patch
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply YesBanGDream MFA patch.' }
Write-Host 'YesBanGDream MFA changes are ready for review. Commit them in the separate MFA repository before a release build.'
