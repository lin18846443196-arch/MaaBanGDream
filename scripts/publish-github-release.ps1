param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$GitHubCli = 'gh',
    [switch]$Publish,
    [string]$DeviceAcceptance
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$repository = 'woshiyigeanniu/YesBanGDream'
$interface = Get-Content (Join-Path $projectRoot 'interface.json') -Raw -Encoding utf8 | ConvertFrom-Json
$version = [string]$interface.version
$tag = "v$version"
$notes = Join-Path $projectRoot "docs\release-notes-v$version.md"
$buildInfo = Get-Content (Join-Path $PackageRoot 'BUILD-INFO.json') -Raw -Encoding utf8 | ConvertFrom-Json
$head = (& git -C $projectRoot rev-parse HEAD).Trim()
$branch = (& git -C $projectRoot branch --show-current).Trim()
$changes = @(& git -C $projectRoot status --porcelain)
if ($changes.Count -gt 0) { throw 'Release source must be clean.' }
if ($buildInfo.version -ne $version -or $buildInfo.maa_commit -ne $head) {
    throw 'Rebuild the package from the current clean release source before publishing.'
}
if ($Publish) {
    if ($branch -ne 'main') { throw 'A public stable release must be built from clean main.' }
    if (-not $DeviceAcceptance) { throw 'Stable release requires the completed device acceptance JSON.' }
    $acceptance = Get-Content $DeviceAcceptance -Raw -Encoding utf8 | ConvertFrom-Json
    if ($acceptance.version -ne $version -or $acceptance.commit -ne $head -or $acceptance.passed -ne $true) {
        throw 'Device acceptance must match this version and source commit.'
    }
}
& $Python (Join-Path $PSScriptRoot 'check_release_package.py') $PackageRoot
if ($LASTEXITCODE -ne 0) { throw 'Package validation failed.' }
$assets = @()
foreach ($suffix in @('.zip', '-update.zip')) {
    $archive = "$PackageRoot$suffix"
    $checksum = "$archive.sha256"
    $expected = ((Get-Content $checksum -Raw) -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        throw "Release archive SHA256 mismatch: $archive"
    }
    $assets += @($archive, $checksum)
}
& $GitHubCli auth status --hostname github.com
if ($LASTEXITCODE -ne 0) { throw 'GitHub login is required; no release has been created.' }
& $GitHubCli api "repos/$repository/commits/$head" --silent
if ($LASTEXITCODE -ne 0) { throw 'Push and review this commit in the target repository before creating the release.' }
$remoteTag = @(& git -C $projectRoot ls-remote origin "refs/tags/$tag")
if ($LASTEXITCODE -ne 0 -or $remoteTag.Count -eq 0) { throw "Push the reviewed $tag tag before creating the release." }
$releaseArguments = @('release', 'create', $tag, '--repo', $repository, '--verify-tag',
    '--title', "RhythmPilot $tag", '--notes-file', $notes)
if (-not $Publish) { $releaseArguments += @('--draft', '--prerelease') }
& $GitHubCli @releaseArguments @assets
if ($LASTEXITCODE -ne 0) { throw 'GitHub release creation failed; inspect the remote before retrying.' }
