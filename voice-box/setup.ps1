<#
 Nous voice box setup (spec C5). Windows 11, PowerShell. Idempotent: safe to re-run.
 Run in a normal window first; the firewall step needs an Administrator window
 (the script says so and skips it otherwise). Not run by CI; Tim runs this on the GPU box.
 Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PS 5.1 progress bar slows big downloads
function Check($step) { if ($LASTEXITCODE -ne 0) { throw "step failed: $step (exit $LASTEXITCODE)" } }
$Src = "C:\nous-src"; $Root = "C:\voice"; $Venv = "$Root\.venv"
$Lib = "$Root\cudalibs"; $Models = "$Root\models"; $EnvFile = "$Root\.env"

Write-Host "== 0. NVIDIA driver"; nvidia-smi

Write-Host "== 1. Tools"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { winget install --id astral-sh.uv -e }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { winget install --id Git.Git -e }
if (-not (Get-Command uv -ErrorAction SilentlyContinue) -or -not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "Installed uv/git. Open a NEW PowerShell window and re-run this script."; exit 0
}

Write-Host "== 2. Code (use 'gh auth login' first if the repo is private)"
if (Test-Path "$Src\.git") { git -C $Src pull --ff-only; Check "git pull" } else { git clone --depth 1 https://github.com/tfatykhov/nous $Src; Check "git clone" }

Write-Host "== 3. Python env, pinned versions"
New-Item -ItemType Directory -Force $Root, $Models | Out-Null
if (-not (Test-Path "$Venv\Scripts\python.exe")) { uv venv --python 3.12 $Venv; Check "uv venv" }
$Py = "$Venv\Scripts\python.exe"
uv pip install --python $Py -r "$Src\voice-box\requirements.txt"; Check "pip install requirements"
uv pip uninstall --python $Py onnxruntime onnxruntime-gpu; Check "pip uninstall onnxruntime"
uv pip install --python $Py onnxruntime-gpu; Check "pip install onnxruntime-gpu"
& $Py -c "import onnxruntime as o; p=o.get_available_providers(); print(p); assert 'CUDAExecutionProvider' in p, 'CUDA provider missing'"; Check "CUDA provider"

Write-Host "== 4. CUDA 12 cuBLAS + cuDNN 9 libraries (manual download)"
if (-not (Test-Path $Lib) -or -not (Get-ChildItem $Lib -ErrorAction SilentlyContinue)) {
  New-Item -ItemType Directory -Force $Lib | Out-Null
  Write-Host "Download the CUDA 12 archive from https://github.com/Purfview/whisper-standalone-win/releases/tag/libs"
  Write-Host "and unzip it into $Lib, then re-run this script."
} else {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (($userPath -split ";") -notcontains $Lib) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$Lib", "User")
    Write-Host "Added $Lib to your user PATH. Open a NEW PowerShell window before running the server."
  }
}

Write-Host "== 4b. Kokoro model files (~330 MB, from the kokoro-onnx release; [assumption] file names/URL)"
$rel = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
foreach ($f in "kokoro-v1.0.onnx", "voices-v1.0.bin") {
  if (-not (Test-Path "$Models\$f")) { Invoke-WebRequest "$rel/$f" -OutFile "$Models\$f.part"; Move-Item "$Models\$f.part" "$Models\$f" }
}

Write-Host "== 5. Secrets (created once; never overwritten)"
if (-not (Test-Path $EnvFile)) {
  Copy-Item "$Src\voice-box\.env.example" $EnvFile
  $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  $tok = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
  (Get-Content $EnvFile) -replace '^VOICE_BOX_TOKEN=.*', "VOICE_BOX_TOKEN=$tok" | Set-Content $EnvFile
  Write-Host "Token written to $EnvFile. Give the same value to Nous as NOUS_VOICE_BOX_TOKEN:"
  Write-Host $tok
  Write-Host "Edit NOUS_VOICE_URL / NOUS_VOICE_TOKEN in $EnvFile later (PR 7)."
} else { Write-Host "$EnvFile exists; leaving it alone." }

Write-Host "== 6. Windows Firewall (LAN + Tailscale only)"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  Write-Host "Not Administrator: skipping firewall. Re-run from an Administrator PowerShell."
} else {
  $ranges = "192.168.1.0/24", "100.64.0.0/10"   # edit if your LAN differs
  if (-not (Get-NetFirewallRule -DisplayName "nous-voice tcp" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "nous-voice tcp" -Direction Inbound -Protocol TCP -LocalPort 7860 -RemoteAddress $ranges -Action Allow
  }
  # The venv python.exe is a launcher; the process that opens sockets is the base interpreter.
  $base = & $Py -c "import sys; print(sys._base_executable)"
  if (-not (Get-NetFirewallRule -DisplayName "nous-voice udp" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "nous-voice udp" -Direction Inbound -Protocol UDP -Program $base -RemoteAddress $ranges -Action Allow
  } else { Set-NetFirewallRule -DisplayName "nous-voice udp" -Program $base -RemoteAddress $ranges }
  Set-NetFirewallRule -DisplayName "nous-voice tcp" -RemoteAddress $ranges
  Write-Host "If Windows shows an 'allow access' popup for python.exe on first run, click Cancel (it would bypass the address limits)."
}

Write-Host "== 7. Run (first run downloads the Whisper model from Hugging Face, a few GB; no audio leaves the box)"
Write-Host "   cd $Src\voice-box; `$env:VOICE_BOX_ENV_FILE='$EnvFile'; & '$Py' server.py"
Write-Host "== 8. Verify: curl.exe http://<this-pc-ip>:7860/health"
