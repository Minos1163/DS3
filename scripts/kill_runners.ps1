# Kill runner processes by matching known script names in their command line
$patterns = @('run_oos_tasks','run_high_priority_tasks','validate_single_best')

# Combine patterns into a single regex
$regex = ($patterns -join '|')

$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and ($_.CommandLine -match $regex)
}

if ($procs) {
    foreach ($p in $procs) {
        Write-Host "Killing PID $($p.ProcessId) - $($p.CommandLine)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "No matching processes found."
}
