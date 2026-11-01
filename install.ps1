# AntMonitor - Windows one-line installer. Keep this file ASCII: Windows PowerShell 5.1
# misreads UTF-8 without BOM, so Chinese messages below are \u-escaped and decoded by U().
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
#   & ([scriptblock]::Create((irm https://gitee.com/raphael01/antmonitor/raw/main/install.ps1))) -Source gitee
#
# GitHub: downloads the source ZIP (no git needed); run.bat adopts it for web updates.
# Gitee: requires Git for Windows on PATH and clones the selected repository, keeping its origin.
# Both install to %LOCALAPPDATA%\AntMonitor\antmonitor, add a desktop shortcut and start run.bat
# (which installs Python/venv/deps on first run).
# Env: ANTMONITOR_HOME = install folder, ANTMONITOR_BRANCH = branch (main),
#      ANTMONITOR_GH_REPO / ANTMONITOR_GITEE_REPO = owner/repository for the selected source,
#      MINER_REPO_URL = explicit Git remote override for the Gitee clone,
#      GH_PROXY = mirror prefix for GitHub ZIP downloads (trusted mirrors only).
param(
    [ValidateSet('github', 'gitee')]
    [string]$Source = 'github'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function U([string]$s) { [regex]::Unescape($s) }
function Say([string]$s, [string]$color = 'Cyan') { Write-Host $s -ForegroundColor $color }
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$repo = if ($Source -eq 'gitee') {
    if ($env:ANTMONITOR_GITEE_REPO) { $env:ANTMONITOR_GITEE_REPO } else { 'raphael01/antmonitor' }
} else {
    if ($env:ANTMONITOR_GH_REPO) { $env:ANTMONITOR_GH_REPO } else { 'raphael2025/antmonitor' }
}
$branch = if ($env:ANTMONITOR_BRANCH) { $env:ANTMONITOR_BRANCH } else { 'main' }
$proxy = if ($env:GH_PROXY) { $env:GH_PROXY } else { '' }
$dest = if ($env:ANTMONITOR_HOME) { $env:ANTMONITOR_HOME } else { Join-Path $env:LOCALAPPDATA 'AntMonitor\antmonitor' }

try {
    if (Test-Path -LiteralPath (Join-Path $dest 'server.py')) {
        Say (((U '\u5df2\u5b89\u88c5\u5728')) + " $dest" + ((U '\uff0c\u76f4\u63a5\u542f\u52a8\u3002\u66f4\u65b0\u8bf7\u5728\u7f51\u9875\u53f3\u4e0a\u89d2\u70b9\u300c\u66f4\u65b0\u300d\u3002')))
    } else {
        if (Test-Path -LiteralPath (Join-Path $dest '.git')) {
            throw (U '\u73b0\u6709 Git \u76ee\u5f55\u7f3a\u5c11 server.py\uff0c\u8bf7\u5148\u7528 Git \u4fee\u590d\uff1b\u5b89\u88c5\u811a\u672c\u4e0d\u4f1a\u8986\u76d6\u5176\u6765\u6e90\u6216\u6587\u4ef6\u3002')
        }
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('antmonitor-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        if ($Source -eq 'gitee') {
            $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $git -or $git.Source -like '*\WindowsApps\*') {
                throw (U 'Gitee \u5b89\u88c5\u9700\u8981\u5df2\u5b89\u88c5 Git for Windows \u5e76\u52a0\u5165 PATH\u3002\u8bf7\u5b89\u88c5 Git\u3001\u91cd\u65b0\u6253\u5f00 PowerShell \u540e\u91cd\u8bd5\uff1bGitee \u6a21\u5f0f\u4e0d\u4f1a\u81ea\u52a8\u4e0b\u8f7d Git\u3002')
            }
            $gitPath = $git.Source
            $repoUrl = if ($env:MINER_REPO_URL) { $env:MINER_REPO_URL } else { "https://gitee.com/$repo.git" }
            $checkout = Join-Path $tmp 'source'
            Say ("Git clone: $repoUrl (branch: $branch)")
            $oldPrompt = $env:GIT_TERMINAL_PROMPT
            try {
                $env:GIT_TERMINAL_PROMPT = '0'
                & $gitPath -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 clone --single-branch --branch $branch -- $repoUrl $checkout
                if ($LASTEXITCODE -ne 0) { throw (U 'Git \u514b\u9686\u5931\u8d25\uff1a\u8bf7\u68c0\u67e5\u4ed3\u5e93\u5730\u5740\u3001\u5206\u652f\u548c\u7f51\u7edc\u8fde\u63a5\u3002') }
            } finally {
                $env:GIT_TERMINAL_PROMPT = $oldPrompt
            }
            $inner = Get-Item -LiteralPath $checkout
        } else {
            $zipUrl = "${proxy}https://github.com/$repo/archive/refs/heads/$branch.zip"
            $zip = Join-Path $tmp 'src.zip'
            Say (((U '\u4e0b\u8f7d\u6e90\u7801')) + " $zipUrl")
            Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zip
            Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
            $inner = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
        }
        if (-not $inner -or -not (Test-Path -LiteralPath (Join-Path $inner.FullName 'run.bat'))) {
            throw ((U '\u4e0b\u8f7d\u7684\u6e90\u7801\u4e0d\u5b8c\u6574'))
        }
        $parent = Split-Path -Parent $dest
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        if (Test-Path -LiteralPath $dest) {
            # An empty or half-made folder: keep any local files, copy the source over it.
            Get-ChildItem -LiteralPath $inner.FullName -Force | Copy-Item -Destination $dest -Recurse -Force
        } else {
            Move-Item -LiteralPath $inner.FullName -Destination $dest
        }
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Say (((U '\u5df2\u5b89\u88c5\u5230')) + " $dest") 'Green'
    }

    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $desktop 'AntMonitor.lnk'))
        $link.TargetPath = Join-Path $dest 'run.bat'
        $link.WorkingDirectory = $dest
        $link.Description = 'AntMonitor'
        $link.Save()
        Say ((U '\u5df2\u5728\u684c\u9762\u521b\u5efa\u5feb\u6377\u65b9\u5f0f AntMonitor\uff0c\u4ee5\u540e\u53cc\u51fb\u5b83\u542f\u52a8\u3002')) 'Green'
    } catch {
        Say (((U '\u684c\u9762\u5feb\u6377\u65b9\u5f0f\u521b\u5efa\u5931\u8d25(\u4e0d\u5f71\u54cd\u4f7f\u7528)\uff1a')) + $_.Exception.Message) 'Yellow'
    }

    Say ((U '\u6b63\u5728\u542f\u52a8\uff1a\u9996\u6b21\u4f1a\u81ea\u52a8\u5b89\u88c5 Python \u548c\u4f9d\u8d56\uff0c\u8bf7\u5728\u65b0\u7a97\u53e3\u91cc\u6309\u63d0\u793a\u8bbe\u7f6e admin \u5bc6\u7801\u3002')) 'Green'
    Start-Process -FilePath (Join-Path $dest 'run.bat') -WorkingDirectory $dest
} catch {
    Say (((U '\u5b89\u88c5\u5931\u8d25\uff1a')) + $_.Exception.Message) 'Red'
    if ($Source -eq 'gitee') {
        Say (U '\u5148\u8fd0\u884c git --version\uff0c\u786e\u8ba4 Git \u53ef\u7528\u5e76\u80fd\u8bbf\u95ee\u6240\u9009\u4ed3\u5e93\uff1b\u9996\u6b21\u5b89\u88c5 Python \u548c pip \u4f9d\u8d56\u4ecd\u9700\u8054\u7f51\u3002') 'Yellow'
    } else {
        Say ((U '\u68c0\u67e5\u80fd\u5426\u8bbf\u95ee github.com\uff1b\u56fd\u5185\u7f51\u7edc\u53ef\u5148\u8bbe\u7f6e\u955c\u50cf\uff1a$env:GH_PROXY=''https://\u955c\u50cf\u524d\u7f00/'' \u518d\u8fd0\u884c\u3002')) 'Yellow'
    }
    exit 1
}
