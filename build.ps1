# Builds dist\CADCopilot.exe, a single file people can download and run.
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

& .venv\Scripts\python.exe -m unittest test_agent
if ($LASTEXITCODE -ne 0) { throw "Tests failed; not building." }

& .venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed `
    --name CADCopilot `
    --icon assets\icon.ico `
    --add-data "ui;ui" `
    --hidden-import keyring.backends.Windows `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Get-Item dist\CADCopilot.exe
"Built $($exe.FullName) ($([math]::Round($exe.Length / 1MB, 1)) MB)"
