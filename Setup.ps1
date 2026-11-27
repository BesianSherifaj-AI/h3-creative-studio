[CmdletBinding()]
param([switch]$NoShortcut)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
foreach ($tool in @('ffmpeg','ffprobe')) {
    if (!(Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Install a full FFmpeg build and put $tool on PATH."
    }
}
if (!(Test-Path -LiteralPath $python)) {
    $bootstrap = Get-Command py -ErrorAction SilentlyContinue
    if ($bootstrap) {
        & $bootstrap.Source -3.11 -m venv (Join-Path $root '.venv')
    } else {
        & python -m venv (Join-Path $root '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Install the 64-bit Python 3.11 distribution and run Setup again.' }
}
& $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -c 'import av, cv2, httpx, numpy, PIL, psutil, webview'
if ($LASTEXITCODE -ne 0) { throw 'The installed desktop environment failed its import check.' }
Set-Content -LiteralPath (Join-Path $root '.venv\.h3-ready') -Value 'ready'
if (!$NoShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'H3 Creative Studio.lnk'))
    $link.TargetPath = (Join-Path $root '.venv\Scripts\pythonw.exe')
    $link.Arguments = '"'+(Join-Path $root 'studio.py')+'"'
    $link.WorkingDirectory = $root
    $link.Description = 'Open H3 Creative Studio'
    $link.Save()
    Write-Host "Desktop shortcut installed in $desktop"
}
Write-Host 'H3 Creative Studio is ready. Run Launch.cmd to open it.'
