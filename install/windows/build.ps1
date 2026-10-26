$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Project '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Create the project .venv and install requirements first.' }
& $Python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller is required. Install requirements-build.txt into .venv.' }
Push-Location $Project
try {
    $AppBuild = Join-Path $Project 'build\portable\AcrobatSemanticSearch'
    $env:ACROBAT_APP_BUILD_DIR = $AppBuild
    & $Python install\make_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Application icon generation failed.' }
    & $Python install\windows\build_runtime.py
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
    $Payload = Join-Path $Project 'build\payload.zip'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Payload) | Out-Null
    if (Test-Path $Payload) { Remove-Item -LiteralPath $Payload -Force }
    Compress-Archive -Path (Join-Path $AppBuild '*') -DestinationPath $Payload -CompressionLevel Optimal
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name AcrobatSemanticSearchSetup `
        --icon install\assets\acrobat-semantic-search.ico --add-data "$Payload;." `
        --add-data "install\assets\acrobat-semantic-search.ico;assets" --hidden-import win32com.client install\windows\installer.py
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
    Write-Host "Installer: $(Join-Path $Project 'dist\AcrobatSemanticSearchSetup.exe')"
} finally { Remove-Item Env:\ACROBAT_APP_BUILD_DIR -ErrorAction SilentlyContinue; Pop-Location }
