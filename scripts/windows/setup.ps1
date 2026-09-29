<#
  QuickTranscriber - one-time setup for Windows.

  Installs a private copy of Python and the libraries QuickTranscriber needs
  INTO THIS FOLDER ONLY:
      runtime\uv                - the uv package manager (one small file)
      runtime\python-installs   - Python 3.12
      runtime\venv              - QuickTranscriber's libraries
      runtime\cache             - download cache (safe to delete)
  Nothing is installed anywhere else, nothing is added to the registry or PATH.
  To uninstall, delete the folder.

  Usage:  setup.ps1 [-NoLaunch] [-NoGpu]
#>
param(
    [switch]$NoLaunch,
    [switch]$NoGpu
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Runtime = Join-Path $Root 'runtime'
$UvVersion = '0.8.17'
$PythonVersion = '3.12'

$Host.UI.RawUI.WindowTitle = 'QuickTranscriber setup'
Write-Host ''
Write-Host '  QuickTranscriber' -ForegroundColor Magenta
Write-Host '  Private meeting transcription - everything stays on this computer.' -ForegroundColor Gray
Write-Host ''

function Step([string]$Message) { Write-Host "  > $Message" -ForegroundColor Cyan }
function Fail([string]$Message) {
    Write-Host ''
    Write-Host "  Setup failed: $Message" -ForegroundColor Red
    Write-Host '  Check your internet connection and run QuickTranscriber again.' -ForegroundColor Yellow
    Write-Host ''
    Read-Host '  Press Enter to close'
    exit 1
}

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

# 1. uv (downloads Python and the libraries)
$UvDir = Join-Path $Runtime 'uv'
$Uv = Join-Path $UvDir 'uv.exe'
if (-not (Test-Path $Uv)) {
    Step 'Downloading the installer (about 20 MB)...'
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
    $zip = Join-Path $Runtime 'uv.zip'
    try {
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$arch-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
        New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
        Expand-Archive -Path $zip -DestinationPath $UvDir -Force
        Remove-Item $zip -Force
    } catch { Fail "could not download uv ($($_.Exception.Message))" }
}

# Keep everything uv does inside this folder
$env:UV_CACHE_DIR = Join-Path $Runtime 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Runtime 'python-installs'
$env:UV_PYTHON_BIN_DIR = Join-Path $Runtime 'python-installs\bin'
$env:UV_TOOL_DIR = Join-Path $Runtime 'tools'
$env:UV_NO_CONFIG = '1'
$env:UV_LINK_MODE = 'copy'
$env:UV_PYTHON_PREFERENCE = 'only-managed'

# 2. Python + a private environment
$Venv = Join-Path $Runtime 'venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Step "Installing a private copy of Python $PythonVersion (about 30 MB)..."
    & $Uv python install $PythonVersion --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { Fail 'Python could not be downloaded' }
    if (Test-Path $Venv) { Remove-Item $Venv -Recurse -Force }
    & $Uv venv $Venv --python $PythonVersion --no-project
    if ($LASTEXITCODE -ne 0) { Fail 'could not create the Python environment' }
}

# 3. Libraries (re-run automatically when requirements.txt changes)
$Req = Join-Path $Root 'requirements.txt'
$Stamp = Join-Path $Venv '.requirements-installed'
$Hash = (Get-FileHash $Req -Algorithm SHA256).Hash
$Installed = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { '' }
if ($Installed -ne $Hash) {
    Step 'Installing speech recognition and AI libraries (about 400 MB, one time)...'
    & $Uv pip install --python $Py -r $Req
    if ($LASTEXITCODE -ne 0) { Fail 'the libraries could not be installed' }
    Set-Content -Path $Stamp -Value $Hash
}

# 4. NVIDIA GPU acceleration (optional)
$NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $NvidiaSmi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $NvidiaSmi = "$env:SystemRoot\System32\nvidia-smi.exe" }
$GpuStamp = Join-Path $Venv '.gpu-installed'
if ($NvidiaSmi -and -not $NoGpu -and -not $env:QT_NO_GPU -and -not (Test-Path $GpuStamp)) {
    Step 'NVIDIA graphics card found - adding GPU acceleration (about 1.3 GB, one time)...'
    & $Uv pip install --python $Py -r (Join-Path $Root 'requirements-gpu.txt')
    if ($LASTEXITCODE -eq 0) { Set-Content -Path $GpuStamp -Value 'ok' }
    else { Write-Host '  GPU acceleration could not be installed - the processor will be used instead.' -ForegroundColor Yellow }
}

Step 'Setup complete.'
if ($NoLaunch) { exit 0 }

Write-Host ''
$env:QT_ROOT = $Root
$env:PYTHONUTF8 = '1'
& $Py -m quicktranscriber
exit $LASTEXITCODE
