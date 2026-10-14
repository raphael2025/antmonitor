# Standard Windows PowerShell bootstrap; Python is not needed to run this file.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PythonVersion = '3.13.16'
$RuntimeCheck = Join-Path $PSScriptRoot 'windows_launcher.py'

function Test-Python($Executable, $Prefix = @()) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        & $Executable @Prefix $RuntimeCheck --check-runtime 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-SystemPython {
    foreach ($name in @('py.exe', 'python.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if (-not $command -or $command.Source -like '*\WindowsApps\*') { continue }
        if ($name -eq 'py.exe') {
            # Try the configured default first, then supported minors if the
            # default is a new runtime (e.g. 3.15) without dependency wheels.
            foreach ($tag in @('-3', '-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3.9', '-3.8')) {
                if (Test-Python $command.Source @($tag)) {
                    return @{ Executable = $command.Source; Prefix = @($tag) }
                }
            }
        } elseif (Test-Python $command.Source) {
            return @{ Executable = $command.Source; Prefix = @() }
        }
    }
    # An installed Python may be absent from PATH, or hidden by a Store alias.
    foreach ($key in @('HKCU:\Software\Python\PythonCore',
                       'HKLM:\Software\Python\PythonCore',
                       'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem $key -ErrorAction SilentlyContinue)) {
            $install = Get-Item ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if (-not $install) { continue }
            $candidate = $install.GetValue('ExecutablePath')
            if (-not $candidate -and $install.GetValue('')) {
                $candidate = Join-Path $install.GetValue('') 'python.exe'
            }
            if ($candidate -and (Test-Python $candidate)) {
                return @{ Executable = $candidate; Prefix = @() }
            }
        }
    }
    return $null
}

function Install-ProjectPython($RuntimeDirectory) {
    $executable = Join-Path $RuntimeDirectory 'python.exe'
    if (Test-Python $executable) { return $executable }
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    switch ($architecture.ToUpperInvariant()) {
        'AMD64' { $suffix = '-amd64' }
        'ARM64' { $suffix = '-arm64' }
        'X86'   { $suffix = '' }
        default { throw "Unsupported Windows architecture: $architecture" }
    }
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion$suffix.exe"
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('antmonitor-python-' + [guid]::NewGuid())
    $parent = Split-Path -Parent $RuntimeDirectory
    [IO.Directory]::CreateDirectory($temporary) | Out-Null
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $installer = Join-Path $temporary 'python-installer.exe'
    $log = Join-Path $parent 'python-install.log'
    try {
        Write-Host "No compatible Python 3.8-3.14 found. Downloading Python $PythonVersion from python.org..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -UseBasicParsing -OutFile $installer -TimeoutSec 120
        # A parent PowerShell 7 can leave its module paths in the environment;
        # Windows PowerShell 5.1 must load its own signature-checking module.
        Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1')
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)(CN|O)=Python Software Foundation(,|$)') {
            throw 'The Python installer signature is invalid or has an unexpected publisher.'
        }
        Write-Host "Installing Python for the current user in $RuntimeDirectory..."
        $arguments = '/quiet InstallAllUsers=0 Include_pip=1 Include_launcher=0 ' +
                     'InstallLauncherAllUsers=0 PrependPath=0 Shortcuts=0 Include_test=0 ' +
                     'Include_doc=0 Include_tcltk=0 ' +
                     ('TargetDir="{0}" /log "{1}"' -f $RuntimeDirectory, $log)
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Python installation failed (code $($process.ExitCode)). Installer log: $log"
        }
        if (-not (Test-Python $executable)) {
            throw "Python could not run after installation. Installer log: $log"
        }
        return $executable
    } finally {
        Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    $python = $null
    # Optional explicit runtime directory also makes deployment without PATH or
    # registry discovery deterministic. Normal users do not need to set this.
    if ($env:MINER_PYTHON_DIR) {
        $runtime = [IO.Path]::GetFullPath($env:MINER_PYTHON_DIR)
    } else {
        $python = Find-SystemPython
        if (-not $python) {
            if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not defined for this Windows user.' }
            $runtime = Join-Path $env:LOCALAPPDATA 'AntMonitor\Python313'
        }
    }
    if (-not $python) {
        $python = @{ Executable = (Install-ProjectPython $runtime); Prefix = @() }
    }
    $prefix = $python.Prefix
    $ErrorActionPreference = 'Continue'
    & $python.Executable @prefix (Join-Path $PSScriptRoot 'windows_launcher.py')
    exit $LASTEXITCODE
} catch {
    Write-Host "AntMonitor could not start: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Check internet access and the errors above, then double-click run.bat again.'
    if (-not $env:MINER_NO_PAUSE -and -not [Console]::IsInputRedirected) {
        Read-Host 'Press Enter to close' | Out-Null
    }
    exit 1
}
