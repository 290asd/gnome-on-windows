# Measures where CPU goes while GNOME + the bridge run, in % of one core over N seconds.
#   perf.ps1 [seconds]
param([int]$Seconds = 10)
function WinTimes {
    $t = @{}
    $bridge = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'winbridge' }
    foreach ($b in $bridge) { $t["winbridge (Windows capture)"] += (Get-Process -Id $b.ProcessId).TotalProcessorTime.TotalSeconds }
    foreach ($m in Get-Process msrdc -ErrorAction SilentlyContinue) { $t["msrdc (WSLg -> screen)"] += $m.TotalProcessorTime.TotalSeconds }
    $t
}
$before = WinTimes
$linux = wsl -d Ubuntu-24.04 --exec bash "$(wsl -d Ubuntu-24.04 --exec wslpath -a $PSScriptRoot)/perf.sh" $Seconds   # takes $Seconds
$after = WinTimes
foreach ($k in $after.Keys) { "{0,-28} {1,5:N0} %" -f $k, (($after[$k] - $before[$k]) * 100 / $Seconds) }
$linux
