# Requires PowerShell 5.1 or later
<#
.SYNOPSIS
    Installs (or upgrades) Python on Windows Deadline workers and pulls in the packages needed
    for deadline_preview_worker.py (PyOpenColorIO, OpenEXR, NumPy, ffmpeg already expected).

.DESCRIPTION
    - Uses winget to install Python if it is not already present.
    - Upgrades pip and installs the required Python packages system-wide (or per-user).
    - Can be executed with administrative privileges to install for all users.
    - Safe to re-run; existing installations will be reused.

.EXAMPLE
    # Run from an elevated PowerShell prompt on the worker:
    .\setup_deadline_python.ps1 -PythonId "Python.Python.3.11" -Packages @("PyOpenColorIO","OpenEXR","numpy")

.NOTES
    - Ensure winget is available (Windows 10 2004+ or 11).
    - ffmpeg must already be reachable via PATH or configured in .env (FFMPEG_PATH).
#>

param (
    [string]$PythonId = "Python.Python.3.11",
    [string]$PythonExecutable = "C:\Python311\python.exe",
    [string[]]$Packages = @("OpenColorIO", "OpenEXR", "numpy", "Pillow")
)

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Ensure-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required but was not found. Install App Installer from Microsoft Store first."
    }
}

function Install-Python {
    Ensure-Winget
    Write-Section "Checking Python installation"

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        Write-Host "Python launcher detected at $($pyLauncher.Source)"
        return $pyLauncher.Name
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        Write-Host "Python already available at $($pythonCmd.Source)"
        return $pythonCmd.Source
    }

    Write-Host "Python not detected. Installing via winget package '$PythonId'..."
    $exitCode = winget install --id $PythonId --silent --accept-package-agreements --accept-source-agreements
    if ($exitCode -ne 0) {
        throw "winget failed to install Python (exit code $exitCode)."
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        Write-Host "Python launcher installed at $($pyLauncher.Source)"
        return $pyLauncher.Name
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return $pythonCmd.Source
    }

    if (Test-Path $PythonExecutable) {
        Write-Host "Using Python executable at $PythonExecutable"
        return $PythonExecutable
    }

    throw "Python installation finished but no interpreter was found. Please set -PythonExecutable explicitly."
}

function Install-Packages {
    param(
        [string]$PythonPath,
        [string[]]$Pkgs
    )

    Write-Section "Installing Python packages"
    Write-Host "Using Python at: $PythonPath"

    & $PythonPath -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }

    $packageFallbacks = @{
        "OpenColorIO" = @("OpenColorIO", "PyOpenColorIO")
        "PyOpenColorIO" = @("PyOpenColorIO", "OpenColorIO")
        "OpenEXR" = @("OpenEXR", "openexr-python")
        "numpy" = @("numpy")
        "Pillow" = @("Pillow")
    }

    foreach ($pkg in $Pkgs) {
        $candidates = if ($packageFallbacks.ContainsKey($pkg)) { $packageFallbacks[$pkg] } else { @($pkg) }
        $installed = $false
        foreach ($candidate in $candidates) {
            Write-Host "Installing $candidate..."
            & $PythonPath -m pip install --upgrade $candidate
            if ($LASTEXITCODE -eq 0) {
                $installed = $true
                break
            } else {
                Write-Warning "Failed to install '$candidate'. Trying next alternative (if any)."
            }
        }
        if (-not $installed) {
            throw "Failed to install package '$pkg'."
        }
    }

    Write-Host "Package installation complete." -ForegroundColor Green
}

try {
    $pythonPath = Install-Python
    if (-not (Test-Path $pythonPath) -and (Test-Path $PythonExecutable)) {
        $pythonPath = $PythonExecutable
    }
    Install-Packages -PythonPath $pythonPath -Pkgs $Packages

    Write-Section "Final steps"
    Write-Host "Ensure ffmpeg is installed and reachable on this worker (or set FFMPEG_PATH in .env)."
    Write-Host "Done." -ForegroundColor Green
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
