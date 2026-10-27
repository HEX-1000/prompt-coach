# Sets up the prompt coach TUI on Windows:
#   1. a virtual environment in this folder (.venv) with textual installed,
#   2. a Windows Terminal profile "Prompt Coach" (JSON fragment, with its own color scheme),
#   3. a desktop shortcut that opens that profile.
# Run once, and again after moving the folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
$App = $PSScriptRoot

# Problems that do not stop the install (Ollama, models) are collected and printed at the end.
$Notes = New-Object System.Collections.Generic.List[string]

# Python: the newest 3.10 or newer from the Python install manager folder, then the py launcher,
# then python on PATH. The python.exe in WindowsApps only opens the Microsoft Store, so it is skipped.
$PyHelp = "Install Python 3.10 or newer from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this script again."
$Py = $null
$PyArgs = @()
$managed = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Python") -Directory -Filter "pythoncore-3*" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $folderVersion = $null
        try { $folderVersion = [version]($_.Name -replace '^pythoncore-(\d+\.\d+).*$', '$1') } catch { }
        if ($folderVersion -and $folderVersion -ge [version]"3.10") {
            [pscustomobject]@{ Version = $folderVersion; Exe = (Join-Path $_.FullName "python.exe") }
        }
    } |
    Where-Object { Test-Path $_.Exe } |
    Sort-Object Version -Descending |
    Select-Object -First 1
if ($managed) {
    $Py = $managed.Exe
} else {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $Py = $launcher.Source
        $PyArgs = @("-3")
    } else {
        $python = Get-Command python -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
            Select-Object -First 1
        if ($python) { $Py = $python.Source }
    }
}
if (-not $Py) { throw "Python was not found. $PyHelp" }
$PyVersion = & $Py @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or -not $PyVersion) { throw "Python could not be started ($Py). $PyHelp" }
$PyVersion = "$PyVersion".Trim()
$PyParts = $PyVersion.Split(".")
if ([int]$PyParts[0] -lt 3 -or ([int]$PyParts[0] -eq 3 -and [int]$PyParts[1] -lt 10)) {
    throw "Python $PyVersion is too old. $PyHelp"
}
Write-Host "Python $PyVersion found."

$VPy = Join-Path $App ".venv\Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    Write-Host "Creating virtual environment..."
    & $Py @PyArgs -m venv (Join-Path $App ".venv")
}
Write-Host "Installing textual..."
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $App "requirements.txt")
& $VPy -c "import textual; print('textual', textual.__version__)"

# Windows Terminal fragment. Terminal reads it on the next start.
$fragDir = Join-Path $env:LOCALAPPDATA "Microsoft\Windows Terminal\Fragments\PromptCoach"
New-Item -ItemType Directory -Force $fragDir | Out-Null
$fragment = [ordered]@{
    profiles = @([ordered]@{
        name              = "Prompt Coach"
        commandline       = "`"$VPy`" -B `"$App\coach_tui.py`""
        startingDirectory = $App
        colorScheme       = "Prompt Coach Violet"
        icon              = (Join-Path $App "assets\icon.ico")
        hidden            = $false
    })
    schemes = @([ordered]@{
        name = "Prompt Coach Violet"
        background = "#16131F"; foreground = "#E8E3F3"; cursorColor = "#C4B5FD"
        selectionBackground = "#3B3352"
        black = "#16131F"; red = "#F4A6C6"; green = "#A8E6CF"; yellow = "#F6C9A8"
        blue = "#A5C8FF"; purple = "#C4B5FD"; cyan = "#9EE0E8"; white = "#E8E3F3"
        brightBlack = "#8A83A0"; brightRed = "#F7BED5"; brightGreen = "#C2EFDD"; brightYellow = "#F9DAC2"
        brightBlue = "#C3DBFF"; brightPurple = "#DDD3FE"; brightCyan = "#BDEBF0"; brightWhite = "#FFFFFF"
    })
}
$json = $fragment | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText((Join-Path $fragDir "prompt-coach.json"), $json, (New-Object Text.UTF8Encoding $false))
Write-Host "Windows Terminal profile written: $fragDir\prompt-coach.json"

# Desktop shortcut: opens the Terminal profile if Windows Terminal exists, otherwise run_tui.cmd.
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "Prompt Coach.lnk"))
$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
if ($wt) {
    $lnk.TargetPath = $wt.Source
    $lnk.Arguments = '-p "Prompt Coach"'
} else {
    $lnk.TargetPath = Join-Path $App "run_tui.cmd"
    $lnk.Arguments = ""
}
$lnk.WorkingDirectory = $App
$icon = Join-Path $App "assets\icon.ico"
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Description = "Local prompt coach (Ollama)"
$lnk.Save()
Write-Host "Desktop shortcut created: $desktop\Prompt Coach.lnk"
Write-Host "Done. Start it from the shortcut, or open Windows Terminal and pick 'Prompt Coach'."

# Ollama and model checks. These only warn: the app itself is already installed.
$Ollama = Get-Command ollama -ErrorAction SilentlyContinue
$OllamaExe = $null
if ($Ollama) { $OllamaExe = $Ollama.Source }
else {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $guess) { $OllamaExe = $guess }
}

if (-not $OllamaExe) {
    $Notes.Add("Ollama is not installed. Get it from https://ollama.com/download . When it asks you to sign in, choose 'No thanks, I'll use Ollama locally'.")
} else {
    $wanted = @()
    try { $wanted = @((Get-Content (Join-Path $App "config.json") -Raw | ConvertFrom-Json).models) } catch { }
    if ($wanted.Count -eq 0) { $wanted = @("qwen3.5:9b", "qwen3.5:4b") }

    $have = @()
    $listed = $false
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $OllamaExe list 2>$null
        if ($LASTEXITCODE -eq 0) {
            $listed = $true
            $have = @($lines | Select-Object -Skip 1 | ForEach-Object { ($_ -split "\s+")[0] } | Where-Object { $_ })
        }
    } catch { }
    $ErrorActionPreference = $prev

    if (-not $listed) {
        $Notes.Add("Ollama is installed but not running. Start it from the Start menu (it then sits in the system tray), then check your models with: ollama list")
    } else {
        $found = @($wanted | Where-Object { $have -contains $_ })
        if ($found.Count -eq 0) {
            $Notes.Add("No model is downloaded yet. In PowerShell run: ollama pull $($wanted[0])")
            if ($wanted.Count -gt 1) {
                $Notes.Add("On a GPU with less than 8 GB, use the smaller one instead: ollama pull $($wanted[1])  (and put it first in the models list in config.json)")
            }
        } else {
            Write-Host "Ollama is running; model found: $($found -join ', ')."
        }
    }
}

Write-Host ""
if ($Notes.Count -eq 0) {
    Write-Host "All checks passed." -ForegroundColor Green
} else {
    Write-Host "Before you start:" -ForegroundColor Yellow
    foreach ($n in $Notes) { Write-Host "  - $n" }
}
