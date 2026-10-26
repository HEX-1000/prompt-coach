# Sets up the prompt coach TUI on Windows:
#   1. a virtual environment in this folder (.venv) with textual installed,
#   2. a Windows Terminal profile "Prompt Coach" (JSON fragment, with its own color scheme),
#   3. a desktop shortcut that opens that profile.
# Run once, and again after moving the folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
$App = $PSScriptRoot

# Python 3.14 from the Python install manager; falls back to the py launcher.
$Py = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
$PyArgs = @()
if (-not (Test-Path $Py)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) { throw "Python not found at $Py and no 'py' launcher on PATH." }
    $Py = $launcher.Source
    $PyArgs = @("-3")
}

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
