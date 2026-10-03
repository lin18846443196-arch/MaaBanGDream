param(
    [string]$Version,
    [string]$MfaSourceRoot,
    [string]$OutputDirectory,
    [string]$BuildPython,
    [string]$RuntimePythonRoot,
    [string]$RuntimeArchive,
    [string]$RuntimeArchiveSha256,
    [string]$NativeExtension,
    [string]$NativeExtensionSha256,
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $MfaSourceRoot) {
    $MfaSourceRoot = Join-Path $workspaceRoot 'MFAAvalonia'
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot '.local\release'
}
if (-not $BuildPython) {
    $BuildPython = Join-Path `
        $workspaceRoot `
        '.tools\Miniconda3\envs\maabangdream\python.exe'
}
if (-not $RuntimePythonRoot) {
    $RuntimePythonRoot = Join-Path `
        $env:SystemDrive `
        'MaaBanGDreamReleasePython'
}

$sourceInterface = Join-Path $projectRoot 'interface.json'
$interface = Get-Content `
    -LiteralPath $sourceInterface `
    -Raw `
    -Encoding utf8 | ConvertFrom-Json
if (-not $Version) {
    $Version = [string]$interface.version
}
if ($Version -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
    throw "Invalid release version: $Version"
}
$releaseNotesRelativePath = "docs\release-notes-v$Version.md"

$mfaProject = Join-Path `
    $MfaSourceRoot `
    'MFAAvalonia.Desktop\MFAAvalonia.Desktop.csproj'
$mfaUpdaterProject = Join-Path `
    $MfaSourceRoot `
    'MFAUpdater\MFAUpdater.csproj'
$zipBuilder = Join-Path $projectRoot 'scripts\create_release_zip.py'
$mfaLicense = Join-Path $MfaSourceRoot 'LICENSE'
$performanceSettings = Join-Path `
    $MfaSourceRoot `
    'MFAAvalonia\Views\UserControls\Settings\PerformanceProfileSettingsUserControl.axaml'
$versionChecker = Join-Path `
    $MfaSourceRoot `
    'MFAAvalonia\Helper\VersionChecker.cs'
foreach ($required in @(
    $mfaProject,
    $mfaUpdaterProject,
    $mfaLicense,
    $performanceSettings,
    $versionChecker,
    (Join-Path $projectRoot 'packaging\YesBanGDream.targets'),
    (Join-Path $projectRoot 'packaging\yesbangdream.ico'),
    (Join-Path $projectRoot 'packaging\start-maabangdream.cmd'),
    (Join-Path $projectRoot 'docs\release-package.md'),
    (Join-Path $projectRoot $releaseNotesRelativePath),
    (Join-Path $projectRoot 'LICENSING.md'),
    (Join-Path $projectRoot 'TRADEMARKS.md'),
    (Join-Path $projectRoot 'THIRD-PARTY-NOTICES.md'),
    (Join-Path $projectRoot 'licenses\LICENSE-MaaFramework-LGPL-3.0.md'),
    (Join-Path $projectRoot 'scripts\start-release.ps1'),
    (Join-Path $projectRoot 'scripts\normalize-release-directory.ps1'),
    (Join-Path $projectRoot 'scripts\restart-release.ps1'),
    (Join-Path $projectRoot 'scripts\create_mfa_source_archive.py'),
    $zipBuilder
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Release build input is missing: $required"
    }
}
if (-not (Select-String `
    -LiteralPath $versionChecker `
    -SimpleMatch 'SupportsSelectedResourceUpdateSource' `
    -Quiet)) {
    throw 'MFA source is missing the MaaBanGDream Mirror update guard.'
}
if (-not (Select-String -LiteralPath $versionChecker -SimpleMatch '"YesBanGDream"' -Quiet)) {
    throw 'Apply and commit the YesBanGDream MFA branding patch in the isolated MFA source checkout first.'
}

if (-not $AllowDirty) {
    foreach ($repository in @($projectRoot, $MfaSourceRoot)) {
        $changes = @(& git -C $repository status --porcelain)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect Git working tree: $repository"
        }
        if ($changes.Count -gt 0) {
            throw (
                "Release builds require a clean Git working tree: $repository. " +
                'Commit the release inputs, or use -AllowDirty only for a local probe.'
            )
        }
    }
}

$outputFull = [System.IO.Path]::GetFullPath($OutputDirectory)
$packageName = "YesBanGDream-v$Version-win-x64"
$packageRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $outputFull $packageName)
)
$outputPrefix = $outputFull.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $packageRoot.StartsWith(
    $outputPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Refusing to stage outside release output: $packageRoot"
}
New-Item -ItemType Directory -Force -Path $outputFull | Out-Null
if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

dotnet publish $mfaProject `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:MaaBanGDreamPackageBuild=true `
    "-p:YesBanGDreamBrandRoot=$(Join-Path $projectRoot 'packaging')" `
    "-p:CustomAfterMicrosoftCommonTargets=$(Join-Path $projectRoot 'packaging\YesBanGDream.targets')" `
    -o $packageRoot
if ($LASTEXITCODE -ne 0) {
    throw 'Customized MFAAvalonia publish failed.'
}
foreach ($hostFile in @('YesBanGDream.exe', 'YesBanGDream.dll', 'YesBanGDream.deps.json', 'YesBanGDream.runtimeconfig.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $hostFile) -PathType Leaf)) {
        throw "Branded desktop host is missing: $hostFile"
    }
}
if (Test-Path -LiteralPath (Join-Path $packageRoot 'MFAAvalonia.exe')) {
    throw 'Release publish unexpectedly retained the generic MFAAvalonia.exe host.'
}
Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Filter '*.pdb' |
    Remove-Item -Force

# 核心程序文件必须在 MFA 退出后覆盖。更新器独立发布为 self-contained 单文件，
# 避免目标电脑缺少对应 .NET 大版本时无法启动更新。
$updaterPublishDirectory = Join-Path $outputFull '.mfa-updater-publish'
if (Test-Path -LiteralPath $updaterPublishDirectory) {
    Remove-Item -LiteralPath $updaterPublishDirectory -Recurse -Force
}
dotnet publish $mfaUpdaterProject `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:PublishTrimmed=true `
    -p:TrimMode=link `
    "-p:CustomAfterMicrosoftCommonTargets=$(Join-Path $projectRoot 'packaging\YesBanGDream.targets')" `
    -o $updaterPublishDirectory
if ($LASTEXITCODE -ne 0) {
    throw 'MFAUpdater self-contained publish failed.'
}
$updaterExecutable = Join-Path $updaterPublishDirectory 'MFAUpdater.exe'
if (-not (Test-Path -LiteralPath $updaterExecutable -PathType Leaf)) {
    throw 'MFAUpdater publish did not produce MFAUpdater.exe.'
}
Copy-Item `
    -LiteralPath $updaterExecutable `
    -Destination (Join-Path $packageRoot 'MFAUpdater.exe') `
    -Force
Remove-Item -LiteralPath $updaterPublishDirectory -Recurse -Force

# Native 实时扩展被 .gitignore 忽略、不会进入 Git，但便携包必须内置；
# 否则打开 Native 的便携环境会报 “No module named 'maabangdream_realtime'”。
if ($NativeExtension) {
    $nativeChanges = @(& git -C $projectRoot diff v1.4.5 -- native/realtime)
    if ($LASTEXITCODE -ne 0 -or $nativeChanges.Count -gt 0) {
        throw 'Reusing the verified v1.4.5 native extension requires unchanged native sources.'
    }
    if (-not $NativeExtensionSha256 -or (Get-FileHash -LiteralPath $NativeExtension -Algorithm SHA256).Hash -ne $NativeExtensionSha256) {
        throw 'Verified native extension SHA256 mismatch.'
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'agent\realtime\native') | Out-Null
    Copy-Item -LiteralPath $NativeExtension -Destination (Join-Path $projectRoot 'agent\realtime\native\maabangdream_realtime.pyd') -Force
} else {
    & (Join-Path $projectRoot 'scripts\build_native_realtime.ps1') -Python $BuildPython
    if ($LASTEXITCODE -ne 0) {
        throw 'Native realtime extension build failed.'
    }
}

function Copy-ProjectFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath,
        [string]$DestinationRelativePath
    )
    if (-not $DestinationRelativePath) {
        $DestinationRelativePath = $RelativePath
    }
    $source = Join-Path $projectRoot $RelativePath
    $destination = Join-Path $packageRoot $DestinationRelativePath
    $destinationParent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

$trackedRuntimeFiles = & git -C $projectRoot ls-files -- agent resource
if ($LASTEXITCODE -ne 0 -or -not $trackedRuntimeFiles) {
    throw 'Unable to enumerate tracked Agent/resource files.'
}
foreach ($relativePath in $trackedRuntimeFiles) {
    Copy-ProjectFile -RelativePath $relativePath
}
Copy-ProjectFile -RelativePath 'agent\realtime\native\maabangdream_realtime.pyd'

foreach ($relativePath in @(
    'docs\about.md',
    'docs\contact.md',
    'docs\announcement.md',
    'docs\assets\yesbangdream-logo.png',
    'requirements.txt',
    'runtime-compatibility.json',
    'scripts\start-release.ps1',
    'scripts\normalize-release-directory.ps1',
    'scripts\restart-release.ps1',
    'scripts\cleanup_runtime_artifacts.py',
    'scripts\prepare_portable_runtime.py',
    'scripts\check_runtime.py',
    'scripts\sync_bestdori_catalog.py',
    'scripts\sync_bestdori_charts.py'
)) {
    Copy-ProjectFile -RelativePath $relativePath
}
Copy-ProjectFile `
    -RelativePath 'interface.json' `
    -DestinationRelativePath 'interface.template.json'
Copy-ProjectFile -RelativePath 'interface.json'
Copy-ProjectFile `
    -RelativePath 'packaging\start-maabangdream.cmd' `
    -DestinationRelativePath '启动 YesBanGDream.cmd'
Copy-ProjectFile `
    -RelativePath 'docs\release-package.md' `
    -DestinationRelativePath 'README.md'
Copy-ProjectFile `
    -RelativePath $releaseNotesRelativePath `
    -DestinationRelativePath 'resource\Release.md'
Copy-ProjectFile `
    -RelativePath 'LICENSE' `
    -DestinationRelativePath 'LICENSE-MaaBanGDream.txt'
Copy-ProjectFile `
    -RelativePath 'LICENSING.md' `
    -DestinationRelativePath 'LICENSING-MaaBanGDream.md'
Copy-ProjectFile `
    -RelativePath 'TRADEMARKS.md' `
    -DestinationRelativePath 'TRADEMARKS-MaaBanGDream.md'
Copy-ProjectFile -RelativePath 'THIRD-PARTY-NOTICES.md'
Copy-ProjectFile `
    -RelativePath 'licenses\LICENSE-MaaFramework-LGPL-3.0.md' `
    -DestinationRelativePath 'LICENSE-MaaFramework-LGPL-3.0.md'
Copy-Item `
    -LiteralPath $mfaLicense `
    -Destination (Join-Path $packageRoot 'LICENSE-MFAAvalonia.txt') `
    -Force

if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
    throw "Release build Python is missing: $BuildPython"
}
if (-not $RuntimeArchive) {
$buildPythonEnvironment = Split-Path -Parent $BuildPython
$condaPack = Join-Path $buildPythonEnvironment 'Scripts\conda-pack.exe'
if (-not (Test-Path -LiteralPath $condaPack -PathType Leaf)) {
    throw (
        'conda-pack build tool is missing. Install build requirements with: ' +
        'python -m pip install -r requirements-release.txt'
    )
}
$runtimePython = Join-Path $RuntimePythonRoot 'python.exe'
$conda = Join-Path $workspaceRoot '.tools\Miniconda3\Scripts\conda.exe'
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $conda -PathType Leaf)) {
        throw "Conda is required to create the release runtime: $conda"
    }
    & $conda create `
        --prefix $RuntimePythonRoot `
        --override-channels `
        --channel conda-forge `
        python=3.12 `
        pip `
        --yes
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to create the neutral release Python environment.'
    }
}
& $runtimePython -m pip install `
    -r (Join-Path $projectRoot 'requirements-runtime.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to install release Python dependencies.'
}
& $runtimePython -c 'import maa, cv2, onnxruntime, psutil'
if ($LASTEXITCODE -ne 0) {
    throw 'Release Python dependency validation failed.'
}
$runtimeDirectory = Join-Path $packageRoot 'runtime'
$pythonArchive = Join-Path $runtimeDirectory 'maabangdream-python.zip'
New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
& $condaPack `
    -p $RuntimePythonRoot `
    -o $pythonArchive `
    --force
if ($LASTEXITCODE -ne 0) {
    throw (
        'Unable to create portable Python runtime. Install build requirements ' +
        'with: python -m pip install -r requirements-release.txt'
    )
}

} else {
    if (-not $RuntimeArchiveSha256 -or (Get-FileHash -LiteralPath $RuntimeArchive -Algorithm SHA256).Hash -ne $RuntimeArchiveSha256) {
        throw 'Verified portable Python archive SHA256 mismatch.'
    }
    $runtimeDirectory = Join-Path $packageRoot 'runtime'
    $pythonArchive = Join-Path $runtimeDirectory 'maabangdream-python.zip'
    New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
    Copy-Item -LiteralPath $RuntimeArchive -Destination $pythonArchive -Force
}

$maaCommit = (& git -C $projectRoot rev-parse HEAD).Trim()
$mfaCommit = (& git -C $MfaSourceRoot rev-parse HEAD).Trim()
$mfaBranch = (& git -C $MfaSourceRoot rev-parse --abbrev-ref HEAD).Trim()
$buildInfo = [ordered]@{
    package = $packageName
    version = $Version
    platform = 'win-x64'
    maa_repository = 'https://github.com/woshiyigeanniu/YesBanGDream'
    upstream_repository = 'https://github.com/coatcn1/MaaBanGDream'
    upstream_baseline = 'v1.4.3'
    upstream_synced = 'v1.4.5'
    desktop_brand = 'YesBanGDream'
    maa_commit = $maaCommit
    mfa_repository = 'https://github.com/coatcn1/MFAAvalonia'
    mfa_branch = $mfaBranch
    mfa_commit = $mfaCommit
    mfa_source_asset = "YesBanGDream-v$Version-MFA-source.zip"
    mfaavalonia = '2.12.0-custom'
    maafw = '5.10.2'
    python = '3.12'
    python_runtime = 'conda-pack'
    dotnet_runtime = 'self-contained'
    branding_targets_sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'packaging\YesBanGDream.targets') -Algorithm SHA256).Hash.ToLowerInvariant()
    mfa_upstream_commit = 'a39dcd87ba2e5098ee23072e9a015c5c36f8c8d1'
    mfa_branding_patch_sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'patches\yesbangdream-mfa-branding.patch') -Algorithm SHA256).Hash.ToLowerInvariant()
    native_extension_source = $(if ($NativeExtension) { 'verified-upstream-v1.4.5-binary' } else { 'built-from-source' })
    native_extension_sha256 = (Get-FileHash -LiteralPath (Join-Path $packageRoot 'agent\realtime\native\maabangdream_realtime.pyd') -Algorithm SHA256).Hash.ToLowerInvariant()
    python_archive_source = $(if ($RuntimeArchive) { 'verified-upstream-v1.4.5-runtime' } else { 'neutral-conda-pack-build' })
    python_archive_sha256 = (Get-FileHash -LiteralPath $pythonArchive -Algorithm SHA256).Hash.ToLowerInvariant()
}
$buildInfo | ConvertTo-Json -Depth 5 |
    Set-Content `
        -LiteralPath (Join-Path $packageRoot 'BUILD-INFO.json') `
        -Encoding utf8

if (-not $AllowDirty) {
    & $BuildPython (Join-Path $PSScriptRoot 'create_mfa_source_archive.py') `
        --package-root $packageRoot --project-root $projectRoot --mfa-root $MfaSourceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Desktop corresponding source archive creation failed.' }
}

$forbiddenTopLevelNames = @(
    'config',
    'logs',
    'profiles',
    'debug',
    'screencap',
    '.maabangdream-backup',
    'profile-manager.json',
    'appsettings.json'
)
foreach ($name in $forbiddenTopLevelNames) {
    if (Test-Path -LiteralPath (Join-Path $packageRoot $name)) {
        throw "Private/runtime state leaked into release package: $name"
    }
}

# 内置维护者校准好的默认 Profile 与选择种子，便携包首次启动即可直接用。
$seedProfiles = Join-Path $projectRoot 'packaging\profiles'
if (Test-Path -LiteralPath $seedProfiles -PathType Container) {
    $packageProfiles = Join-Path $packageRoot 'profiles'
    New-Item -ItemType Directory -Force -Path $packageProfiles | Out-Null
    Copy-Item -Path (Join-Path $seedProfiles '*') -Destination $packageProfiles -Force
}

# 版本依据清单：version 是更新器判断本地版本、后续版本号的唯一来源；
# files（相对路径 -> SHA256）保留作包内容诊断，不再参与逐文件增量 diff。
$updateManifest = [ordered]@{ version = $Version; files = [ordered]@{} }
Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($packageRoot.Length + 1).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $updateManifest.files[$relative] = $hash
}
$updateManifest | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $packageRoot 'update-manifest.json') -Encoding utf8

$zipPath = Join-Path $outputFull "$packageName.zip"
$shaPath = "$zipPath.sha256"
foreach ($oldArtifact in @($zipPath, $shaPath)) {
    if (Test-Path -LiteralPath $oldArtifact) {
        Remove-Item -LiteralPath $oldArtifact -Force
    }
}
& $BuildPython $zipBuilder `
    --source $packageRoot `
    --output $zipPath
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to create Windows release ZIP.'
}
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
[System.IO.File]::WriteAllText(
    $shaPath,
    "$zipHash  $packageName.zip`r`n",
    [System.Text.UTF8Encoding]::new($false)
)

# 更新包：不含 Python 运行时归档。首次启动后该归档会被解压并删除，
# 更新时无需再下载约 350MB 运行库；本地谱面库（resource/charts）也有自己
# 的同步通道（演出设置 → 谱面辅助 → 同步），不进更新包。只有本机运行库
# 缺失时才回退下载完整包。
$updateZipPath = Join-Path $outputFull "$packageName-update.zip"
$updateShaPath = "$updateZipPath.sha256"
foreach ($oldArtifact in @($updateZipPath, $updateShaPath)) {
    if (Test-Path -LiteralPath $oldArtifact) {
        Remove-Item -LiteralPath $oldArtifact -Force
    }
}
$runtimeArchiveRelative = 'runtime/maabangdream-python.zip'
$chartsRelative = 'resource/charts'
& $BuildPython $zipBuilder `
    --source $packageRoot `
    --output $updateZipPath `
    --flat-root `
    --exclude $runtimeArchiveRelative `
    --exclude $chartsRelative
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to create Windows update ZIP.'
}
$updateZipHash = (Get-FileHash -LiteralPath $updateZipPath -Algorithm SHA256).Hash
[System.IO.File]::WriteAllText(
    $updateShaPath,
    "$updateZipHash  $packageName-update.zip`r`n",
    [System.Text.UTF8Encoding]::new($false)
)

[pscustomobject]@{
    PackageRoot = $packageRoot
    Zip = $zipPath
    Sha256 = $zipHash
    Bytes = (Get-Item -LiteralPath $zipPath).Length
    UpdateZip = $updateZipPath
    UpdateSha256 = $updateZipHash
    UpdateBytes = (Get-Item -LiteralPath $updateZipPath).Length
    MfaSourceZip = $(if (-not $AllowDirty) { Join-Path $outputFull "YesBanGDream-v$Version-MFA-source.zip" })
}
