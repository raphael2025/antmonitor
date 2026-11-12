# AntMonitor Windows installer. ASCII for Windows PowerShell 5.1.
# GitHub/Gitee are download sources, not programs or account requirements.
# Run locally for downloaded source, or download this script from either source.
# Optional offline files: offline/MinGit-2.56.0-{arch}.zip, Python installer, wheels/.
param(
    [ValidateSet('auto', 'github', 'gitee')]
    [string]$Source = $(if ($env:ANTMONITOR_SOURCE) { $env:ANTMONITOR_SOURCE } else { 'auto' }),
    [string]$SourcePath = '',
    [switch]$NoStart
)

# Capture BEFORE native commands: PS 5.1 may start its provider in PSHOME for [] paths.
$nativeStartDirectory = [Environment]::CurrentDirectory
$providerDirectory = (Get-Location).Path
$entryDirectory = if ($providerDirectory -eq $PSHOME -and $nativeStartDirectory -ne $PSHOME) {
    $nativeStartDirectory
} else { $providerDirectory }
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$oldPath = $env:PATH
$oldDownloadSource = $env:MINER_DOWNLOAD_SOURCE
$temporary = $null
$mingitVersion = '2.56.0'
$mingitHashes = @{
    '64-bit' = '064b440ff870ed5198527e8f3a92cdf5bd2fd0fedf5e718af95e3fdaddeff718'
    '32-bit' = '9f8266486c8818b91cbb6b719e35972a406f7560e86b82fbda2f3dcb7c069f12'
    'arm64' = 'cb3b0f2d486ea52673227151a5baf5bc13861ff80e74e94e46d614d1bfcd5c06'
}
function U([string]$s) { [regex]::Unescape($s) }
function Say([string]$s, [string]$color = 'Cyan') { Write-Host $s -ForegroundColor $color }
function Full-InstallPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $entryDirectory $Path))
}
function Test-SourceFolder([string]$Path) {
    foreach ($name in @('server.py', 'run.bat', 'ensure_python.ps1', 'windows_launcher.py', 'requirements.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $name) -PathType Leaf)) { return $false }
    }
    return $true
}
function Invoke-InstallGit([string]$Git, [string[]]$GitArgs, [int]$Timeout = 120, [switch]$Probe) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Git
    $info.WorkingDirectory = $entryDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    # Windows command-line quoting, including paths with spaces, [] and trailing backslashes.
    $info.Arguments = (($GitArgs | ForEach-Object {
        '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
    $info.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $info.EnvironmentVariables['GCM_INTERACTIVE'] = 'never'
    $info.EnvironmentVariables['GIT_HTTP_LOW_SPEED_LIMIT'] = '1000'
    $info.EnvironmentVariables['GIT_HTTP_LOW_SPEED_TIME'] = '30'
    if ($Probe) {
        $info.EnvironmentVariables['PATH'] = ''
        $info.EnvironmentVariables['LC_ALL'] = 'C'
        $info.EnvironmentVariables.Remove('GIT_EXEC_PATH')
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout * 1000)) {
            $process.Kill()
            throw "Git timed out after $Timeout seconds."
        }
        return @{ Code = $process.ExitCode; Output = $output.Result; Error = $errors.Result }
    } finally { $process.Dispose() }
}
function Test-InstallGit([string]$Git, [switch]$Portable) {
    if (-not (Test-Path -LiteralPath $Git -PathType Leaf)) { return $false }
    # Antivirus can briefly hold freshly extracted executables. Keep retries bounded.
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        try {
            $version = Invoke-InstallGit $Git @('--version') 10
            if ($version.Code -ne 0 -or $version.Output -notmatch '^git version ') { return $false }
            if (-not $Portable) { return $true }
            $probe = Invoke-InstallGit $Git @('-c', 'help.autocorrect=0', '-c', 'protocol.allow=never', 'remote-https') 10 -Probe
            return $probe.Code -eq 1 -and $probe.Error.Contains('remote-curl: usage:')
        } catch {
            if ($attempt -lt 2) { Start-Sleep -Milliseconds (200 * ($attempt + 1)) }
        }
    }
    return $false
}
function Get-InstallGit {
    $command = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command -and $command.Source -notlike '*\WindowsApps\*' -and (Test-InstallGit $command.Source)) {
        return $command.Source
    }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $arch = switch ($architecture.ToUpperInvariant()) { 'AMD64' { '64-bit' }; 'X86' { '32-bit' }; 'ARM64' { 'arm64' } }
    if (-not $arch) { throw "Unsupported Windows architecture: $architecture" }
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required to install portable Git.' }
    $cache = Join-Path $env:LOCALAPPDATA 'AntMonitor'
    $legacy = Join-Path $cache "MinGit-$mingitVersion"
    $marker = "MinGit $mingitVersion $arch`nsha256 $($mingitHashes[$arch])`n"
    $oldGit = Join-Path $legacy 'cmd\git.exe'
    if (Test-InstallGit $oldGit -Portable) { return $oldGit }
    foreach ($candidate in @(Get-ChildItem -LiteralPath $cache -Directory -ErrorAction SilentlyContinue)) {
        if ($candidate.Name -notlike "MinGit-$mingitVersion-$arch-*") { continue }
        $complete = Join-Path $candidate.FullName '.antmonitor-complete'
        try {
            if (-not (Test-Path -LiteralPath $complete) -or [IO.File]::ReadAllText($complete) -cne $marker) { continue }
        } catch { continue } # an inaccessible old cache must not block a fresh install
        $cachedGit = Join-Path $candidate.FullName 'cmd\git.exe'
        if (Test-InstallGit $cachedGit -Portable) { return $cachedGit }
    }
    $archive = Join-Path $temporary 'mingit.zip'
    $localPackage = $env:MINER_MINGIT_ZIP
    if (-not $localPackage -and $SourcePath -and (Test-Path -LiteralPath $SourcePath -PathType Container)) {
        $bundled = Join-Path $SourcePath "offline\MinGit-$mingitVersion-$arch.zip"
        if (Test-Path -LiteralPath $bundled -PathType Leaf) { $localPackage = $bundled }
    }
    if ($localPackage) {
        Copy-Item -LiteralPath (Full-InstallPath $localPackage) -Destination $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $mingitHashes[$arch]) {
            throw 'Local MinGit ZIP failed SHA256 verification. No files were installed.'
        }
    } else {
        $official = "https://github.com/git-for-windows/git/releases/download/v$mingitVersion.windows.1/MinGit-$mingitVersion-$arch.zip"
        $mirror = "https://sourceforge.net/projects/git-for-windows.mirror/files/v$mingitVersion.windows.1/MinGit-$mingitVersion-$arch.zip/download"
        $urls = if ($Source -eq 'gitee') { @($mirror, $official) } else { @($official, $mirror) }
        $verified = $false
        foreach ($url in $urls) {
            try {
                Say ((U '\u81ea\u52a8\u51c6\u5907 Git\uff08\u65e0\u9700\u624b\u52a8\u5b89\u88c5\uff09\uff1a') + ([uri]$url).Host)
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 120
                if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $mingitHashes[$arch]) {
                    throw 'MinGit download failed SHA256 verification.'
                }
                $verified = $true
                break
            } catch { Say ('Git source unavailable or rejected: ' + $_.Exception.Message) 'Yellow' }
        }
        if (-not $verified) { throw 'Cannot download verified Git. Set MINER_MINGIT_ZIP to a local official MinGit ZIP, or use -SourcePath with downloaded source.' }
    }
    # Extract into a unique permanent directory. Never rename/replace an old cache (WinError 5).
    $target = Join-Path $cache ("MinGit-$mingitVersion-$arch-" + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($target) | Out-Null
    $done = $false
    try {
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            try {
                Expand-Archive -LiteralPath $archive -DestinationPath $target -Force
                break
            } catch {
                if ($attempt -eq 2) { throw }
                Start-Sleep -Milliseconds (200 * ($attempt + 1))
            }
        }
        $git = Join-Path $target 'cmd\git.exe'
        if (-not (Test-InstallGit $git -Portable)) { throw 'Portable Git could not run its Git/HTTPS runtime.' }
        [IO.File]::WriteAllText((Join-Path $target '.antmonitor-complete'), $marker, [Text.Encoding]::ASCII)
        $done = $true
        return $git
    } finally {
        if (-not $done) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

try {
    # A PowerShell 7 parent can leave its module paths in the environment.
    # Load THIS host's built-in hashing and ZIP modules by absolute path before
    # any download, rather than autoloading incompatible modules from that path.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Archive\Microsoft.PowerShell.Archive.psd1') -ErrorAction Stop
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $dest = Full-InstallPath $(if ($env:ANTMONITOR_HOME) { $env:ANTMONITOR_HOME } else { Join-Path $env:LOCALAPPDATA 'AntMonitor\antmonitor' })
    if (-not $SourcePath -and -not $PSBoundParameters.ContainsKey('Source') -and -not $env:ANTMONITOR_SOURCE -and $PSScriptRoot -and (Test-SourceFolder $PSScriptRoot)) {
        $SourcePath = $PSScriptRoot
    }
    if ($SourcePath) { $SourcePath = Full-InstallPath $SourcePath }
    if (Test-Path -LiteralPath (Join-Path $dest '.antmonitor-installing')) {
        throw 'A previous copy did not finish. Keep this folder for recovery; choose a new empty ANTMONITOR_HOME or restore the complete source before retrying.'
    }
    if (Test-SourceFolder $dest) {
        Say ((U '\u5df2\u5b89\u88c5\uff0c\u4fdd\u7559\u539f\u7a0b\u5e8f\u548c\u914d\u7f6e\uff1a') + $dest)
    } else {
        if ((Test-Path -LiteralPath $dest) -and @(Get-ChildItem -LiteralPath $dest -Force).Count -gt 0) {
            throw 'The destination is not empty and is not a complete installation. Existing files were preserved; set ANTMONITOR_HOME to a new empty folder.'
        }
        $temporary = Join-Path ([IO.Path]::GetTempPath()) ('antmonitor-install-' + [guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($temporary) | Out-Null
        $checkout = Join-Path $temporary 'source'
        $git = $null
        if ($SourcePath) {
            if (Test-Path -LiteralPath $SourcePath -PathType Container) {
                $inner = $SourcePath
            } elseif (Test-Path -LiteralPath $SourcePath -PathType Leaf) {
                Expand-Archive -LiteralPath $SourcePath -DestinationPath $checkout
                if (Test-SourceFolder $checkout) { $inner = $checkout } else {
                    $folders = @(Get-ChildItem -LiteralPath $checkout -Directory | Where-Object { Test-SourceFolder $_.FullName })
                    if ($folders.Count -ne 1) { throw 'The local ZIP does not contain one complete AntMonitor source folder.' }
                    $inner = $folders[0].FullName
                }
            } else { throw "Local source does not exist: $SourcePath" }
        } else {
            $branch = if ($env:ANTMONITOR_BRANCH) { $env:ANTMONITOR_BRANCH } else { 'main' }
            $githubRepo = if ($env:ANTMONITOR_GH_REPO) { $env:ANTMONITOR_GH_REPO } else { 'raphael2025/antmonitor' }
            $giteeRepo = if ($env:ANTMONITOR_GITEE_REPO) { $env:ANTMONITOR_GITEE_REPO } else { 'raphael01/antmonitor' }
            $proxy = if ($env:GH_PROXY) { $env:GH_PROXY } else { '' }
            $sources = if ($Source -eq 'auto') { @('github', 'gitee') } else { @($Source) }
            # A custom repository must never fall back to a different project's source.
            if ($env:MINER_REPO_URL -or $env:ANTMONITOR_GH_REPO -or $env:ANTMONITOR_GITEE_REPO) { $sources = @($sources[0]) }
            try { $git = Get-InstallGit } catch {
                if ($Source -eq 'gitee' -or $env:MINER_REPO_URL) { throw }
                Say ('Git unavailable; trying the GitHub source ZIP. ' + $_.Exception.Message) 'Yellow'
            }
            $cloned = $false
            if ($git) {
                $env:PATH = (Split-Path -Parent $git) + [IO.Path]::PathSeparator + $oldPath
                foreach ($selected in $sources) {
                    $repoUrl = if ($env:MINER_REPO_URL) { $env:MINER_REPO_URL } elseif ($selected -eq 'gitee') {
                        "https://gitee.com/$giteeRepo.git"
                    } else { "${proxy}https://github.com/$githubRepo.git" }
                    Say ("Git clone ($selected): $repoUrl")
                    try {
                        $result = Invoke-InstallGit $git @('-c', 'core.autocrlf=false', 'clone', '--single-branch', '--branch', $branch, '--', $repoUrl, $checkout) 180
                        if ($result.Code -ne 0) { throw $result.Error }
                        $cloned = $true
                        $env:MINER_DOWNLOAD_SOURCE = $selected
                        break
                    } catch {
                        Say ('Source unavailable: ' + $_.Exception.Message) 'Yellow'
                        Remove-Item -LiteralPath $checkout -Recurse -Force -ErrorAction SilentlyContinue
                    }
                }
            }
            if (-not $cloned) {
                if ($Source -eq 'gitee' -or $env:MINER_REPO_URL) { throw 'The selected Git repository is unavailable. Retry later or use -SourcePath with downloaded source.' }
                $zip = Join-Path $temporary 'source.zip'
                Invoke-WebRequest -UseBasicParsing -Uri "${proxy}https://github.com/$githubRepo/archive/refs/heads/$branch.zip" -OutFile $zip -TimeoutSec 120
                Expand-Archive -LiteralPath $zip -DestinationPath $checkout
                $folders = @(Get-ChildItem -LiteralPath $checkout -Directory | Where-Object { Test-SourceFolder $_.FullName })
                if ($folders.Count -ne 1) { throw 'Downloaded source is incomplete.' }
                $inner = $folders[0].FullName
            } else { $inner = $checkout }
        }
        if (-not (Test-SourceFolder $inner)) { throw 'Source is incomplete; server, launcher and requirements files are required.' }
        [IO.Directory]::CreateDirectory($dest) | Out-Null
        $installing = Join-Path $dest '.antmonitor-installing'
        [IO.File]::WriteAllText($installing, 'Copy in progress. Do not launch until complete.')
        Get-ChildItem -LiteralPath $inner -Force | Copy-Item -Destination $dest -Recurse -Force
        if (-not (Test-SourceFolder $dest)) { throw 'Source copy did not complete.' }
        if ($git -and (Test-Path -LiteralPath (Join-Path $dest '.git'))) {
            $status = Invoke-InstallGit $git @('-C', $dest, 'status', '--porcelain', '--untracked-files=no')
            if ($status.Code -ne 0 -or $status.Output.Trim()) { throw 'Copied Git checkout failed validation; installation has not been started.' }
        }
        Remove-Item -LiteralPath $installing -Force
        Say ((U '\u5df2\u5b89\u88c5\u5230\uff1a') + $dest) 'Green'
    }
    if (-not $NoStart) {
        try {
            $desktop = [Environment]::GetFolderPath('Desktop')
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut((Join-Path $desktop 'AntMonitor.lnk'))
            $link.TargetPath = Join-Path $dest 'run.bat'
            $link.WorkingDirectory = $dest
            $link.Description = 'AntMonitor'
            $link.Save()
        } catch { Say ('Desktop shortcut unavailable: ' + $_.Exception.Message) 'Yellow' }
        Say (U '\u6b63\u5728\u542f\u52a8\uff1a\u9996\u6b21\u4f1a\u81ea\u52a8\u51c6\u5907 Python \u548c\u4f9d\u8d56\uff0c\u8bf7\u5728\u65b0\u7a97\u53e3\u6309\u63d0\u793a\u8bbe\u7f6e admin \u5bc6\u7801\u3002') 'Green'
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = Join-Path $dest 'run.bat'
        $startInfo.WorkingDirectory = $dest
        $startInfo.UseShellExecute = $true
        $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Normal
        [void][Diagnostics.Process]::Start($startInfo)
    }
} catch {
    Say ((U '\u5b89\u88c5\u5931\u8d25\uff1a') + $_.Exception.Message) 'Red'
    Say (U '\u53ef\u6362\u7528 GitHub/Gitee \u53e6\u4e00\u6761\u5b89\u88c5\u547d\u4ee4\uff0c\u6216\u4f20\u5165 -SourcePath \u672c\u5730\u6e90\u7801\u6587\u4ef6\u5939/ZIP\u3002Python \u548c\u4f9d\u8d56\u4ecd\u9700\u53ef\u7528\u4e0b\u8f7d\u6e90\u6216\u672c\u5730\u5b89\u88c5\u5305\u3002') 'Yellow'
    throw
} finally {
    $env:PATH = $oldPath
    $env:MINER_DOWNLOAD_SOURCE = $oldDownloadSource
    if ($temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
}
