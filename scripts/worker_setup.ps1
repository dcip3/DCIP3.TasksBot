# Requires PowerShell 5.1 or later
<#
.SYNOPSIS
    Installs (or upgrades) Python on Windows Deadline workers and pulls in the packages needed
    for deadline_preview_worker.py (PyOpenColorIO, OpenEXR, NumPy, ffmpeg already expected).

.DESCRIPTION
    - Downloads and installs Python 3.11 from python.org when not already present (falls back to winget).
    - Upgrades pip and installs the required Python packages into EVERY Python 3 interpreter found on
      the machine (py launcher registrations, PATH pythons including Microsoft Store, common install dirs),
      so preview jobs keep working no matter which interpreter `py`/`python` resolves to.
    - Can be executed with administrative privileges to install for all users.
    - Safe to re-run; existing installations will be reused.

.EXAMPLE
    # Run from an elevated PowerShell prompt on the worker:
    .\worker_setup.ps1 -PythonId "Python.Python.3.11" -Packages @("PyOpenColorIO","OpenEXR","numpy")

.NOTES
    - Ensure winget is available (Windows 10 2004+ or 11).
    - ffmpeg must already be reachable via PATH or configured in .env (FFMPEG_PATH).
#>

param (
    [string]$PythonVersion = "3.11.9",
    [string]$PythonArchitecture = "amd64",
    [string]$PythonDownloadUri = "",
    [switch]$InstallPythonForAllUsers,
    [string]$PythonId = "Python.Python.3.11",
    [string]$PythonExecutable = $(Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"),
    [string[]]$Packages = @("OpenColorIO", "OpenEXR", "numpy", "Pillow")
)

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Get-MajorMinor {
    param([string]$VersionString)
    if ([string]::IsNullOrWhiteSpace($VersionString)) {
        return $null
    }
    $parts = $VersionString.Split(".")
    if ($parts.Length -lt 2) {
        return $VersionString
    }
    return "$($parts[0]).$($parts[1])"
}

function Test-IsAdministrator {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-IsAppExecutionAlias {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    return $Path -like "*\Microsoft\WindowsApps\python*.exe"
}

function Get-CommandPathIfPhysical {
    param([System.Management.Automation.CommandInfo]$CommandInfo)
    if (-not $CommandInfo) {
        return $null
    }

    $candidatePath = $CommandInfo.Source
    if ([string]::IsNullOrWhiteSpace($candidatePath)) {
        $candidatePath = $CommandInfo.Definition
    }

    if ([string]::IsNullOrWhiteSpace($candidatePath)) {
        return $null
    }

    if (Test-IsAppExecutionAlias -Path $candidatePath) {
        return $null
    }

    if (-not (Test-Path $candidatePath)) {
        return $null
    }

    return $candidatePath
}

function Test-PythonMatchesVersion {
    param(
        [string]$PythonPath,
        [string]$MajorMinor
    )

    if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path $PythonPath)) {
        return $false
    }

    try {
        $versionOutput = & $PythonPath "-c" "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOutput)) {
            return $false
        }
        return $versionOutput.Trim() -eq $MajorMinor
    } catch {
        return $false
    }
}

function Get-PyLauncherPythonPath {
    param([string]$MajorMinor)

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        return $null
    }

    $pyLauncherPath = Get-CommandPathIfPhysical -CommandInfo $pyLauncher
    if (-not $pyLauncherPath) {
        return $null
    }

    try {
        $pyOutput = & $pyLauncherPath "-$MajorMinor" "-c" "import sys; print(sys.executable)"
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pyOutput)) {
            return $null
        }
        $candidatePath = $pyOutput.Trim()
        if (Test-PythonMatchesVersion -PythonPath $candidatePath -MajorMinor $MajorMinor) {
            Write-Host "Python launcher detected for version $MajorMinor at $candidatePath"
            return $candidatePath
        }
    } catch {
        return $null
    }

    return $null
}

function Get-PythonCandidatePaths {
    param(
        [string]$MajorMinor,
        [string]$PrimaryExecutable
    )

    $pythonShortVersion = $MajorMinor.Replace(".", "")
    $candidates = @(
        $PrimaryExecutable,
        (Join-Path $env:LocalAppData "Programs\Python\Python$pythonShortVersion\python.exe"),
        "C:\Program Files\Python$pythonShortVersion\python.exe",
        "C:\Python$pythonShortVersion\python.exe",
        "C:\Program Files\Python$MajorMinor\python.exe",
        "C:\Python$MajorMinor\python.exe"
    )

    return $candidates |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
}

function Install-PythonFromWeb {
    param(
        [string]$Version,
        [string]$Architecture,
        [string]$DownloadUri,
        [switch]$InstallForAllUsers
    )

    $resolvedUri = $DownloadUri
    if ([string]::IsNullOrWhiteSpace($resolvedUri)) {
        $resolvedUri = "https://www.python.org/ftp/python/$Version/python-$Version-$Architecture.exe"
    }

    Write-Host "Downloading Python $Version ($Architecture) from $resolvedUri..."

    $tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "worker_setup_python"
    if (-not (Test-Path $tempDirectory)) {
        New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
    }

    $installerPath = Join-Path $tempDirectory "python-$Version-$Architecture.exe"

    try {
        Invoke-WebRequest -Uri $resolvedUri -OutFile $installerPath -UseBasicParsing
        if (-not (Test-Path $installerPath)) {
            throw "Failed to download Python installer."
        }

        $installAllUsersValue = "0"
        if ($PSBoundParameters.ContainsKey("InstallForAllUsers")) {
            $installAllUsersValue = if ($InstallForAllUsers.IsPresent) { "1" } else { "0" }
        } else {
            $installAllUsersValue = if (Test-IsAdministrator) { "1" } else { "0" }
        }

        $arguments = @(
            "/quiet",
            "InstallAllUsers=$installAllUsersValue",
            "PrependPath=1",
            "Include_launcher=1",
            "Include_pip=1",
            "Include_test=0",
            "Include_doc=0",
            "Include_symbols=0",
            "Include_debug=0"
        )

        $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
        if ($null -eq $process -or $process.ExitCode -ne 0) {
            $exit = if ($null -ne $process) { $process.ExitCode } else { "unknown" }
            throw "Python installer exited with code $exit."
        }

        Write-Host "Python $Version installation completed."
    } finally {
        if (Test-Path $installerPath) {
            Remove-Item -Path $installerPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Install-PythonViaWinget {
    param([string]$PackageId)

    Ensure-Winget
    Write-Host "Attempting winget installation for package '$PackageId'..."
    $exitCode = winget install --id $PackageId --silent --accept-package-agreements --accept-source-agreements
    if ($exitCode -ne 0) {
        throw "winget failed to install package '$PackageId' (exit code $exitCode)."
    }
}

function Get-ExistingPythonInterpreter {
    param(
        [string]$MajorMinor,
        [string]$PrimaryExecutable
    )

    $pyLauncherPath = Get-PyLauncherPythonPath -MajorMinor $MajorMinor
    if ($pyLauncherPath) {
        return $pyLauncherPath
    }

    foreach ($commandName in @("python3", "python")) {
        $commandInfo = Get-Command $commandName -ErrorAction SilentlyContinue
        $commandPath = Get-CommandPathIfPhysical -CommandInfo $commandInfo
        if ($commandPath -and (Test-PythonMatchesVersion -PythonPath $commandPath -MajorMinor $MajorMinor)) {
            Write-Host "Python $MajorMinor detected at $commandPath"
            return $commandPath
        }
    }

    foreach ($candidatePath in Get-PythonCandidatePaths -MajorMinor $MajorMinor -PrimaryExecutable $PrimaryExecutable) {
        if (Test-PythonMatchesVersion -PythonPath $candidatePath -MajorMinor $MajorMinor) {
            Write-Host "Python $MajorMinor detected at $candidatePath"
            return $candidatePath
        }
    }

    return $null
}

function Resolve-PythonExecutable {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }
    try {
        $resolved = & $Path "-c" "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace("$resolved")) {
            $resolvedPath = "$resolved".Trim()
            if (Test-Path $resolvedPath) {
                return $resolvedPath
            }
        }
    } catch {}
    return $null
}

function Get-AllPythonInterpreters {
    $candidates = @()

    # Interpreters registered with the Python launcher (py -0p)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $listing = cmd /c "py -0p 2>&1"
        foreach ($line in @($listing)) {
            $text = "$line".Trim()
            if (-not $text.StartsWith("-")) { continue }
            $match = [regex]::Match($text, '^\S+\s+\*?\s*(.+)$')
            if ($match.Success) {
                $candidates += $match.Groups[1].Value.Trim()
            }
        }
    }

    # Every python/python3 reachable via PATH (includes Microsoft Store installs)
    foreach ($commandName in @("python", "python3")) {
        try {
            $commandInfos = Get-Command $commandName -All -ErrorAction SilentlyContinue
            foreach ($commandInfo in @($commandInfos)) {
                if ($commandInfo -and $commandInfo.Source) {
                    $candidates += $commandInfo.Source
                }
            }
        } catch {}
    }

    # Common installation directories
    $globRoots = @(
        (Join-Path $env:LocalAppData "Programs\Python"),
        "C:\Program Files",
        "C:\"
    )
    foreach ($root in $globRoots) {
        if (-not (Test-Path $root)) { continue }
        $dirs = Get-ChildItem -Path $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue
        foreach ($dir in @($dirs)) {
            $candidates += (Join-Path $dir.FullName "python.exe")
        }
    }

    # Resolve each candidate through the interpreter itself (handles Store aliases),
    # deduplicate, and keep only Python 3.
    $resolvedInterpreters = @()
    $seen = @{}
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate) -or -not (Test-Path $candidate)) { continue }
        $resolved = Resolve-PythonExecutable -Path $candidate
        if (-not $resolved) { continue }
        # Deduplicate by directory so python.exe / python3.exe from one install count once
        $key = (Split-Path $resolved -Parent).ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        try {
            $major = & $resolved "-c" "import sys; print(sys.version_info.major)"
            if ($LASTEXITCODE -ne 0 -or "$major".Trim() -ne "3") { continue }
        } catch { continue }
        $resolvedInterpreters += $resolved
    }

    return $resolvedInterpreters
}

function Ensure-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required but was not found. Install App Installer from Microsoft Store first."
    }
}

function Install-Python {
    Write-Section "Checking Python installation"

    $requiredMajorMinor = Get-MajorMinor -VersionString $PythonVersion
    if ([string]::IsNullOrWhiteSpace($requiredMajorMinor)) {
        $requiredMajorMinor = "3.11"
    }

    $pythonPath = Get-ExistingPythonInterpreter -MajorMinor $requiredMajorMinor -PrimaryExecutable $PythonExecutable
    if ($pythonPath) {
        return $pythonPath
    }

    Write-Host "Python $requiredMajorMinor not detected. Installing official Python $PythonVersion build..."

    $downloaded = $false
    try {
        if ($PSBoundParameters.ContainsKey("InstallPythonForAllUsers")) {
            Install-PythonFromWeb -Version $PythonVersion -Architecture $PythonArchitecture -DownloadUri $PythonDownloadUri -InstallForAllUsers:$InstallPythonForAllUsers
        } else {
            Install-PythonFromWeb -Version $PythonVersion -Architecture $PythonArchitecture -DownloadUri $PythonDownloadUri
        }
        $downloaded = $true
    } catch {
        Write-Warning "Failed to install Python via direct download: $($_.Exception.Message)"
    }

    if (-not $downloaded) {
        try {
            Write-Host "Falling back to winget package '$PythonId'..."
            Install-PythonViaWinget -PackageId $PythonId
        } catch {
            throw "Unable to install Python $PythonVersion. Last error: $($_.Exception.Message)"
        }
    }

    $pythonPath = Get-ExistingPythonInterpreter -MajorMinor $requiredMajorMinor -PrimaryExecutable $PythonExecutable
    if (-not $pythonPath -and (Test-Path $PythonExecutable)) {
        if (Test-PythonMatchesVersion -PythonPath $PythonExecutable -MajorMinor $requiredMajorMinor) {
            $pythonPath = $PythonExecutable
        }
    }

    if (-not $pythonPath) {
        throw "Python installation finished but no Python $requiredMajorMinor interpreter was found. Please set -PythonExecutable explicitly."
    }

    return $pythonPath
}

function Install-Packages {
    param(
        [string]$PythonPath,
        [string[]]$Pkgs
    )

    Write-Section "Installing Python packages"
    Write-Host "Using Python at: $PythonPath"

    if (-not (Test-Path $PythonPath)) {
        throw "Python executable '$PythonPath' was not found."
    }

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

    Write-Section "Detecting Python interpreters"
    $interpreters = @(Get-AllPythonInterpreters)
    $resolvedPrimary = Resolve-PythonExecutable -Path $pythonPath
    if ($resolvedPrimary -and -not ($interpreters | Where-Object { $_.ToLowerInvariant() -eq $resolvedPrimary.ToLowerInvariant() })) {
        $interpreters = @($resolvedPrimary) + $interpreters
    }
    if ($interpreters.Count -eq 0) {
        $interpreters = @($pythonPath)
    }
    Write-Host "Found $($interpreters.Count) Python interpreter(s):"
    foreach ($interpreter in $interpreters) {
        Write-Host "  $interpreter"
    }

    $failedInterpreters = @()
    foreach ($interpreter in $interpreters) {
        try {
            Install-Packages -PythonPath $interpreter -Pkgs $Packages
        } catch {
            $failedInterpreters += $interpreter
            Write-Warning "Package installation failed for '$interpreter': $($_.Exception.Message). Continuing with remaining interpreters."
        }
    }
    if ($failedInterpreters.Count -eq $interpreters.Count) {
        throw "Package installation failed for every detected Python interpreter."
    }

    Write-Section "Final steps"
    Write-Host "Ensure ffmpeg is installed and reachable on this worker (or set FFMPEG_PATH in .env)."
    Write-Host "Done." -ForegroundColor Green
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
