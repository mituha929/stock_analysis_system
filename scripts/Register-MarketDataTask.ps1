[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)][string]$ApprovedSources,
    [Parameter(Mandatory = $true)][string]$Coverage,
    [Parameter(Mandatory = $true)][string]$Calendar,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_]+$')][string]$Database,
    [ValidateRange(1, 300)][int]$Interval = 1,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe')).Path
$sourcesPath = (Resolve-Path -LiteralPath $ApprovedSources).Path
$coveragePath = (Resolve-Path -LiteralPath $Coverage).Path
$calendarPath = (Resolve-Path -LiteralPath $Calendar).Path
$taskName = 'Capital Market Data Reports'
$arguments = '-B -X utf8 -m scripts.run_market_data scheduled --run --live --apply-mysql --confirm-database {0} --approved-sources "{1}" --coverage "{2}" --calendar "{3}" --interval {4} --limit 50' -f @(
    $Database, $sourcesPath, $coveragePath, $calendarPath, $Interval
)
$times = foreach ($hour in @(9, 18, 20, 21, 22)) {
    foreach ($minute in @(0, 5, 10)) { '{0:D2}:{1:D2}' -f $hour, $minute }
}

if (-not $Install) {
    [pscustomobject]@{
        TaskName = $taskName
        Execute = $pythonPath
        Arguments = $arguments
        WorkingDirectory = $projectRoot
        TriggerTimesTaipei = $times
    }
    return
}

if ((Get-TimeZone).Id -ne 'Taipei Standard Time') {
    throw 'The Windows host must use Taipei Standard Time for these wall-clock triggers.'
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Scheduled task already exists: $taskName"
}
if ($PSCmdlet.ShouldProcess($taskName, 'Register scheduled task')) {
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectRoot
    $triggers = foreach ($time in $times) {
        New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
    }
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings
}
