<#
  Vex setup script for Windows 10/11.

  Run it from inside your vex\ folder:
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  Options:
      -WakeWord   also install the "Hey Vex" wake-word packages (Phase 5)
      -Dev        also install testing and packaging tools

  Safe to re-run: it skips anything already set up.
  Does NOT need admin rights.
#>
param([switch]$WakeWord, [switch]$Dev)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
$PyVersion = '3.12'   # 3.12 has the best wheel support for every package Vex uses

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [x]  $msg" -ForegroundColor Red; exit 1 }
function Write-NoBom($path, $text) { [IO.File]::WriteAllText((Join-Path $PSScriptRoot $path), $text) }

Write-Host "Vex setup" -ForegroundColor Magenta

# ---------------------------------------------------------------- winget
Step "Checking winget"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fail "winget not found. Install 'App Installer' from the Microsoft Store, then re-run."
}
Ok "winget found"

# ---------------------------------------------------------------- Python
Step "Checking Python $PyVersion"
function Test-Py {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $false }
    $out = cmd /c "py -$PyVersion -c `"print('ok')`" 2>nul"
    return ($out -eq 'ok')
}
if (-not (Test-Py)) {
    Warn "Python $PyVersion not found - installing it with winget (per-user, no admin)"
    winget install --id Python.Python.3.12 -e --scope user --silent `
        --accept-package-agreements --accept-source-agreements
    # Pick up the new PATH in this window
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (-not (Test-Py)) {
        Fail "Python was installed but this window can't see it yet. Close PowerShell, open a new one, and re-run setup.ps1."
    }
}
Ok (cmd /c "py -$PyVersion --version")

# ---------------------------------------------------------------- venv
Step "Creating virtual environment (.venv)"
$Py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    cmd /c "py -$PyVersion -m venv .venv"
    if (-not (Test-Path $Py)) { Fail "Could not create .venv" }
    Ok "created .venv"
} else {
    Ok ".venv already exists"
}

# ---------------------------------------------------------------- packages
Step "Upgrading pip"
& $Py -m pip install --upgrade pip wheel --quiet
if ($LASTEXITCODE) { Fail "pip upgrade failed" }
Ok "pip up to date"

Step "Installing Vex packages (this takes a few minutes the first time)"
if (-not (Test-Path requirements.txt)) { Fail "requirements.txt is missing from this folder" }
& $Py -m pip install -r requirements.txt
if ($LASTEXITCODE) { Fail "Package install failed - read the error above" }
Ok "core packages installed"

if ($WakeWord) {
    Step "Installing wake-word packages"
    & $Py -m pip install -r requirements-wakeword.txt
    if ($LASTEXITCODE) { Warn "Wake-word install failed. Vex still works with the hotkey." }
    else { Ok "wake-word packages installed" }
}

if ($Dev) {
    Step "Installing dev tools (pytest, pyinstaller)"
    & $Py -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE) { Warn "Dev tools install failed" } else { Ok "dev tools installed" }
}

# ---------------------------------------------------------------- folders + vault
Step "Creating folders and the memory vault"
foreach ($d in 'logs', 'ui', 'VexVault', 'VexVault\people', 'VexVault\daily') {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$notes = [ordered]@{
    'VexVault\profile.md'     = "# Profile`n`nFacts about me that Vex should know.`n"
    'VexVault\preferences.md' = "# Preferences`n`nHow I want Vex to behave.`n"
    'VexVault\tasks.md'       = "# Tasks`n`n"
}
foreach ($k in $notes.Keys) {
    if (-not (Test-Path $k)) { Write-NoBom $k $notes[$k]; Ok "created $k" }
}
Ok "vault ready (VexMind keeps its notes in VexVault\)"

if (-not (Test-Path 'ui\orb.html')) {
    Warn "ui\orb.html not found - copy orb.html into the ui\ folder"
} else {
    Ok "ui\orb.html found"
}

# ---------------------------------------------------------------- .env
Step "Setting up .env"
if (Test-Path .env) {
    Ok ".env already exists (left unchanged)"
} elseif (Test-Path .env.example) {
    Copy-Item .env.example .env
    Ok ".env created from .env.example"
} else {
    Warn ".env.example missing - create .env by hand"
}

# ---------------------------------------------------------------- .gitignore
if (-not (Test-Path .gitignore)) {
    Write-NoBom '.gitignore' ".env`n.venv/`nlogs/`n__pycache__/`n*.pyc`ndist/`nbuild/`ngoogle-tts-key.json`n"
    Ok ".gitignore created (keeps your keys out of git)"
}

# ---------------------------------------------------------------- verify
Step "Verifying every package imports"
$check = @'
import importlib
mods = [
    "google.genai", "groq", "dotenv", "PySide6.QtWebEngineWidgets",
    "google.cloud.texttospeech", "edge_tts", "sounddevice", "soundfile",
    "numpy", "webrtcvad", "yt_dlp", "send2trash", "psutil", "pycaw",
    "comtypes", "screen_brightness_control", "mss", "pyperclip", "ddgs",
    "apscheduler", "dateparser", "winotify", "keyboard", "rapidfuzz",
    "win32com.client", "tenacity",
]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"  {m}: {e}")
if bad:
    print("Some packages failed to import:")
    print("\n".join(bad))
    raise SystemExit(1)
print(f"All {len(mods)} packages import correctly")
'@
$tmp = Join-Path $env:TEMP 'vex_check_imports.py'
[IO.File]::WriteAllText($tmp, $check)
& $Py $tmp
$importsOk = ($LASTEXITCODE -eq 0)
Remove-Item $tmp -ErrorAction SilentlyContinue

Step "Checking winget can reach its sources (Vex uses it to install apps)"
$null = cmd /c "winget source list 2>nul"
if ($LASTEXITCODE) { Warn "winget sources look broken - try: winget source reset --force" }
else { Ok "winget sources OK" }

# ---------------------------------------------------------------- done
if ($importsOk) {
    Write-Host "`nSetup complete." -ForegroundColor Green
} else {
    Write-Host "`nSetup finished with import errors (see above)." -ForegroundColor Yellow
}
Write-Host @"

Next steps:
  1. Open .env and paste your keys:
       GEMINI_API_KEY   -> aistudio.google.com  (project WITHOUT billing)
       GROQ_API_KEY     -> console.groq.com
       Google Cloud TTS -> service-account JSON from your billing project
                           (needed from Phase 2; set a budget alert)
  2. Activate the environment in each new terminal:
       .\.venv\Scripts\Activate.ps1
  3. Open Claude Code in this folder and say:
       "Read CLAUDE.md and start Phase 0."
"@
