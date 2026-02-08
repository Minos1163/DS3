$patterns = @('run_oos_tasks','run_high_priority_tasks','validate_single_best')
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -match 'run_oos_tasks' -or
        $_.CommandLine -match 'run_high_priority_tasks' -or
        $_.CommandLine -match 'validate_single_best'
    )
}
if ($procs) {
    foreach ($p in $procs) {
        Write-Host "Killing PID $($p.ProcessId) - $($p.CommandLine)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "No matching processes found."
}
