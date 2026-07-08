$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = (Get-Command python -ErrorAction Stop).Source
$logDir = Join-Path $scriptDir 'output\task_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("court_calendar_task_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".log")

function Write-TaskLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format s), $Message
    $line | Tee-Object -FilePath $logPath -Append
}

function Invoke-LoggedCommand {
    param([string[]]$CommandParts)
    Write-TaskLog ("Running: " + ($CommandParts -join ' '))
    & $CommandParts[0] $CommandParts[1..($CommandParts.Length - 1)] 2>&1 |
        ForEach-Object { $_ | Tee-Object -FilePath $logPath -Append }
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $($CommandParts -join ' ')"
    }
}

$env:FTP_HOST = 's2.serv00.com'
$env:FTP_USER = 'f5398_ftp'
$env:FTP_PASS = 'Irgriffin2'

Write-TaskLog 'Starting Victorville court calendar daily task'
Invoke-LoggedCommand @($python, 'vv_court_criminal_calendar_watch.py', '--export-csv')
Invoke-LoggedCommand @($python, 'upload_court_calendar_db.py')
Write-TaskLog 'Victorville court calendar daily task completed successfully'
