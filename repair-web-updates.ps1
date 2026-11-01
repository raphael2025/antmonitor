# Repair an existing AntMonitor Windows installation without replacing its source/data.
# Keep ASCII for Windows PowerShell 5.1; U() decodes the Chinese messages.
# Run after closing the original AntMonitor console. No administrator rights needed.
# The helpers stay OUTSIDE the worktree so older ZIPs can match their published commit.
param(
    [ValidateSet('github', 'gitee')]
    [string]$Source = 'github',
    [string]$Directory = '',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function U([string]$s) { [regex]::Unescape($s) }
function Say([string]$s, [string]$color = 'Cyan') { Write-Host $s -ForegroundColor $color }

function Test-MonitorFolder([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    return $Path -and (Test-Path -LiteralPath (Join-Path $Path 'server.py') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path 'run.bat') -PathType Leaf)
}

function Test-RepairPython([string]$Executable, $Prefix = @()) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    if ($Executable -like '*\WindowsApps\*') { return $false }
    try {
        & $Executable @Prefix -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-RepairPython([string]$Base) {
    $candidates = @((Join-Path $Base '.venv\Scripts\python.exe'))
    if ($env:MINER_PYTHON_DIR) { $candidates += Join-Path $env:MINER_PYTHON_DIR 'python.exe' }
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'AntMonitor\Python313\python.exe' }
    foreach ($candidate in $candidates) {
        if (Test-RepairPython $candidate) { return @{ Executable = $candidate; Prefix = @() } }
    }
    foreach ($name in @('py.exe', 'python.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $prefix = if ($name -eq 'py.exe') { @('-3') } else { @() }
        if (Test-RepairPython $command.Source $prefix) {
            return @{ Executable = $command.Source; Prefix = $prefix }
        }
    }
    return $null
}

$temporary = $null
$oldPath = $env:PATH
$oldUtf8 = $env:PYTHONUTF8
$oldDownloadSource = $env:MINER_DOWNLOAD_SOURCE
# Windows PowerShell 5.1 can start its provider in PSHOME when the native
# working directory contains '['. Capture the real process directory before
# running Python: a native invocation can synchronize it to the provider path.
$nativeStartDirectory = [Environment]::CurrentDirectory
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    if (-not $Directory) {
        $providerDirectory = (Get-Location).Path
        if ($env:ANTMONITOR_HOME) {
            $Directory = $env:ANTMONITOR_HOME
        } elseif (Test-MonitorFolder $providerDirectory) {
            $Directory = $providerDirectory
        } elseif (Test-MonitorFolder $nativeStartDirectory) {
            $Directory = $nativeStartDirectory
        } elseif ($env:LOCALAPPDATA -and (Test-MonitorFolder (Join-Path $env:LOCALAPPDATA 'AntMonitor\antmonitor'))) {
            $Directory = Join-Path $env:LOCALAPPDATA 'AntMonitor\antmonitor'
        } else {
            $Directory = Read-Host (U '\u8bf7\u7c98\u8d34\u73b0\u6709 AntMonitor \u6587\u4ef6\u5939\u8def\u5f84\uff08\u91cc\u9762\u5e94\u6709 run.bat\uff09')
            if ([string]::IsNullOrWhiteSpace($Directory)) {
                throw (U '\u672a\u8f93\u5165\u539f\u5b89\u88c5\u76ee\u5f55\uff0c\u4fee\u590d\u5df2\u505c\u6b62\u3002\u8bf7\u7528 -Directory ''D:\\AntMonitor'' \u6307\u5b9a\u539f\u6587\u4ef6\u5939\u540e\u91cd\u8bd5\u3002')
            }
            $Directory = $Directory.Trim().Trim('"')
        }
    }
    if (-not (Test-MonitorFolder $Directory)) {
        throw (U '\u627e\u4e0d\u5230\u8be5\u76ee\u5f55\u7684 server.py \u548c run.bat\u3002\u8bf7\u7528 -Directory ''D:\\AntMonitor'' \u6307\u5b9a\u539f\u5b89\u88c5\u6587\u4ef6\u5939\u3002')
    }
    $dest = (Resolve-Path -LiteralPath $Directory).ProviderPath
    Say ((U '\u4fee\u590d\u73b0\u6709\u5b89\u88c5\uff1a') + $dest)
    Say (U '\u8bf7\u5148\u5173\u95ed\u539f AntMonitor \u63a7\u5236\u53f0\u7a97\u53e3\u3002\u4fee\u590d\u4e0d\u4f1a\u91cd\u88c5\u7a0b\u5e8f\u3001\u8986\u76d6\u914d\u7f6e\u6216\u6570\u636e\u5e93\u3002') 'Yellow'
    $python = Find-RepairPython $dest
    if (-not $python) {
        throw (U '\u672a\u627e\u5230\u53ef\u7528\u7684 Python 3.8+\u3002\u8bf7\u786e\u8ba4\u6307\u5b9a\u7684\u662f\u80fd\u6b63\u5e38\u542f\u52a8\u7684\u539f\u5b89\u88c5\u76ee\u5f55\u3002')
    }

    $branch = if ($env:ANTMONITOR_BRANCH) { $env:ANTMONITOR_BRANCH } else { 'main' }
    if ($Source -eq 'gitee') {
        $repo = if ($env:ANTMONITOR_GITEE_REPO) { $env:ANTMONITOR_GITEE_REPO } else { 'raphael01/antmonitor' }
        $raw = "https://gitee.com/$repo/raw/$branch"
        $repoUrl = "https://gitee.com/$repo.git"
    } else {
        $repo = if ($env:ANTMONITOR_GH_REPO) { $env:ANTMONITOR_GH_REPO } else { 'raphael2025/antmonitor' }
        $proxy = if ($env:GH_PROXY) { $env:GH_PROXY } else { '' }
        $raw = "${proxy}https://raw.githubusercontent.com/$repo/$branch"
        $repoUrl = "https://github.com/$repo.git"
    }
    if ($env:MINER_REPO_URL) { $repoUrl = $env:MINER_REPO_URL }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('antmonitor-web-repair-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    foreach ($name in @('git_bootstrap.py', 'repair_web_updates.py')) {
        Invoke-WebRequest -UseBasicParsing -Uri "$raw/$name" -OutFile (Join-Path $temporary $name) -TimeoutSec 120
    }
    $resultFile = Join-Path $temporary 'result.json'
    $prefix = $python.Prefix
    $env:PYTHONUTF8 = '1'
    $env:MINER_DOWNLOAD_SOURCE = $Source
    # PowerShell 5.1 can turn native stderr into terminating errors under Stop.
    # Let Python print its precise error, then check its actual exit status.
    $ErrorActionPreference = 'Continue'
    & $python.Executable @prefix -I -X utf8 (Join-Path $temporary 'repair_web_updates.py') --directory $dest --repo-url $repoUrl --branch $branch --result $resultFile
    $repairExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($repairExit -ne 0 -or -not (Test-Path -LiteralPath $resultFile -PathType Leaf)) {
        throw (U '\u6062\u590d\u672a\u5b8c\u6210\uff0c\u8bf7\u67e5\u770b\u4e0a\u65b9\u5177\u4f53\u539f\u56e0\u3002\u539f\u7a0b\u5e8f\u6587\u4ef6\u548c\u6570\u636e\u4fdd\u7559\uff0c\u6ca1\u6709\u542f\u52a8\u7b2c\u4e8c\u4e2a\u76d1\u63a7\u8fdb\u7a0b\u3002')
    }
    $result = Get-Content -LiteralPath $resultFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $result.git -or -not (Test-Path -LiteralPath $result.git -PathType Leaf)) {
        throw 'Repair returned an unavailable Git executable.'
    }
    $env:PATH = (Split-Path -Parent $result.git) + [IO.Path]::PathSeparator + $oldPath
    Say ((U '\u5df2\u6062\u590d Git \u548c\u4ed3\u5e93\u4fe1\u606f\uff1a') + $result.branch + ' @ ' + $result.head.Substring(0, 10)) 'Green'
    if (-not $NoStart) {
        # Start-Process in Windows PowerShell 5.1 can expand brackets in paths.
        # ShellExecute takes these paths literally, opens the usual new console,
        # and inherits the process PATH that we temporarily prepared above.
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = Join-Path $dest 'run.bat'
        $startInfo.WorkingDirectory = $dest
        $startInfo.UseShellExecute = $true
        $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Normal
        [void][Diagnostics.Process]::Start($startInfo)
        Say (U '\u5df2\u7528\u4fee\u590d\u540e\u7684 Git \u542f\u52a8\u65b0\u7a97\u53e3\u3002\u767b\u5f55\u7f51\u9875\u540e\u70b9\u53f3\u4e0a\u89d2\u300c\u66f4\u65b0\u300d\uff0c\u5b89\u88c5\u6700\u65b0\u7248\u672c\u3002') 'Green'
    } else {
        Say (U '\u5df2\u6309 -NoStart \u8df3\u8fc7\u542f\u52a8\u3002\u65e7\u7248\u542f\u52a8\u5668\u53ef\u80fd\u8fd8\u4e0d\u8ba4\u8bc6\u65b0\u7f13\u5b58\uff0c\u8bf7\u91cd\u65b0\u8fd0\u884c\u672c\u4fee\u590d\u547d\u4ee4\uff08\u4e0d\u52a0 -NoStart\uff09\u542f\u52a8\u540e\u5b8c\u6210\u7f51\u9875\u66f4\u65b0\u3002') 'Yellow'
    }
} catch {
    Say ((U '\u7f51\u9875\u66f4\u65b0\u6062\u590d\u5931\u8d25\uff1a') + $_.Exception.Message) 'Red'
    throw
} finally {
    $env:PATH = $oldPath
    $env:PYTHONUTF8 = $oldUtf8
    $env:MINER_DOWNLOAD_SOURCE = $oldDownloadSource
    if ($temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
}
