# Build the Windows installer: two PyInstaller bundles, wrapped in a setup .exe.
#
#   .\packaging\build_exe.ps1              # build into installers\windows\<version>\
#   .\packaging\build_exe.ps1 -KeepVenv    # reuse the build venv (much faster)
#   .\packaging\build_exe.ps1 -NoInstaller # freeze only, skip Inno Setup
#
# Needs on the build machine: Python 3.10+ on PATH, and Inno Setup 6 (ISCC.exe)
# unless -NoInstaller. Nothing is needed on the machine that installs the result
# except Windows 10+ and Google Chrome.
#
# The deliberate mirror of packaging/build_deb.sh: same order, same version
# source, same frozen-core health check. Where the two differ it is because the
# platform differs, and each of those is commented.
[CmdletBinding()]
param(
    [switch]$KeepVenv,
    [switch]$NoInstaller
)

$ErrorActionPreference = "Stop"

$Root   = Split-Path -Parent $PSScriptRoot
$Build  = Join-Path $Root "build"
$Venv   = Join-Path $Build "venv-win"
$Icons  = Join-Path $Build "icons"
$Py     = Join-Path $Venv "Scripts\python.exe"
$Pip    = Join-Path $Venv "Scripts\pip.exe"
$Pyi    = Join-Path $Venv "Scripts\pyinstaller.exe"

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor White }
function Die  ($m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Die "python is not on PATH (install from python.org and tick 'Add to PATH')"
}

# -- version ------------------------------------------------------------------
# pyproject.toml is the only copy; the frozen session_launcher.version() reads it
# back out of the bundle's VERSION file, because importlib.metadata cannot answer
# inside a freeze.
$Version = (Select-String -Path (Join-Path $Root "pyproject.toml") `
                          -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $Version) { Die "no version in pyproject.toml" }
$Out = Join-Path $Root "installers\windows\$Version"

Say "QAVector $Version (windows x64)"

# -- 1. build environment -----------------------------------------------------
if ((-not $KeepVenv) -or (-not (Test-Path $Py))) {
    Say "Creating the build environment"
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    python -m venv $Venv
    # Playwright ships a Chromium downloader that runs on install. We never use a
    # downloaded browser - the adapter only ever connect_over_cdp's to the Chrome
    # already on the machine - so skip it and save ~400 MB.
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"
    # Through the interpreter, not pip.exe: on Windows pip cannot replace its own
    # running executable ("To modify pip, please run ...") and the upgrade fails.
    & $Py -m pip install -q --upgrade pip wheel
    & $Pip install -q -r (Join-Path $Root "requirements.txt") `
                      -r (Join-Path $Root "gui\requirements.txt") `
                      "pyinstaller>=6.6" pyinstaller-hooks-contrib
    if ($LASTEXITCODE -ne 0) { Die "dependency install failed" }
}

# -- 2. version stamp ---------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Build | Out-Null
Set-Content -Path (Join-Path $Build "VERSION") -Value $Version -Encoding ascii

# -- 3. icons -----------------------------------------------------------------
# Before the freeze, not after: the .exe embeds icon.ico (see the specs), so it
# has to exist by the time PyInstaller runs. Building it afterwards is how the
# executables ended up carrying PyInstaller's default Python icon.
Say "Rendering icons"
Remove-Item -Recurse -Force $Icons -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Icons | Out-Null
Push-Location (Join-Path $Root "gui")
& $Py -m cms_gui.icon $Icons | Out-Null
Pop-Location
if (-not (Test-Path (Join-Path $Icons "icon.ico"))) { Die "icon.ico was not rendered" }

# -- 4. freeze ----------------------------------------------------------------
Remove-Item -Recurse -Force (Join-Path $Build "dist"), (Join-Path $Build "pyi") `
            -ErrorAction SilentlyContinue
foreach ($spec in @("core", "gui")) {
    Say "Freezing $spec"
    Push-Location $Root
    & $Pyi --noconfirm --clean `
           --distpath (Join-Path $Build "dist") `
           --workpath (Join-Path $Build "pyi") `
           "packaging\pyinstaller\$spec.spec"
    Pop-Location
    if ($LASTEXITCODE -ne 0) { Die "freezing $spec failed" }
}

$CoreBin = Join-Path $Build "dist\core\qavector-core.exe"
$GuiBin  = Join-Path $Build "dist\gui\qavector-gui.exe"
if (-not (Test-Path $CoreBin)) { Die "the core bundle was not produced" }
if (-not (Test-Path $GuiBin))  { Die "the GUI bundle was not produced" }

# The .deb build chmod +x's playwright's node driver here. Windows has no
# execute bit, so the equivalent is only to check it arrived: without it,
# connect_over_cdp fails the first time a flow runs, long after the build.
$Node = Join-Path $Build "dist\core\_internal\playwright\driver\node.exe"
if (-not (Test-Path $Node)) { Die "playwright's node driver is missing from the core bundle" }

# -- 5. sanity-check the frozen core ------------------------------------------
# Cheap, and it catches the two failures that are otherwise only visible to the
# person who installs the package: a lazy import the spec did not list, and a
# resource that did not make it into the bundle.
Say "Checking the frozen core"
$env:CMS_HOME = Join-Path $Build "smoke"
& $CoreBin --version
# Not Set-Content -Encoding utf8: Windows PowerShell 5.1 spells that with a BOM,
# and json.load() rejects one. WriteAllText with a BOM-less encoder is the only
# spelling that works on both PowerShell editions.
$Describe = Join-Path $Build "describe.json"
[System.IO.File]::WriteAllText($Describe, (& $CoreBin --describe | Out-String),
                               (New-Object System.Text.UTF8Encoding($false)))
& $Py (Join-Path $PSScriptRoot "check_frozen.py") $Describe $Version
if ($LASTEXITCODE -ne 0) { Die "the frozen core is not healthy" }

# -- 6. the installer ---------------------------------------------------------
if ($NoInstaller) {
    Say "Done (freeze only)"
    Write-Host "  $Build\dist\core"
    Write-Host "  $Build\dist\gui"
    exit 0
}

$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $Iscc) {
    foreach ($guess in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                         "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $guess) { $Iscc = $guess; break }
    }
}
if (-not $Iscc) {
    Die "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php, or pass -NoInstaller"
}

Say "Building the installer"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
& $Iscc "/DAppVersion=$Version" "/DRepoRoot=$Root" "/O$Out" `
        (Join-Path $PSScriptRoot "windows\installer.iss")
if ($LASTEXITCODE -ne 0) { Die "Inno Setup failed" }

# -- 7. checksums -------------------------------------------------------------
# Same contract as the .deb build: the installer itself is git-ignored (~100 MB),
# the checksum file is committed, so history records what was released.
Say "Checksums"
Push-Location $Out
Get-ChildItem -Filter *.exe | ForEach-Object {
    "$((Get-FileHash $_.Name -Algorithm SHA256).Hash.ToLower())  ./$($_.Name)"
} | Set-Content -Path "SHA256SUMS" -Encoding ascii
Get-Content SHA256SUMS
Pop-Location

Say "Done"
Write-Host "  $Out\qavector-$Version-setup.exe"
Write-Host ""
Write-Host "Install by running that .exe. Chrome must already be on the machine."
