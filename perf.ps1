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
$distro = if ($env:GNOME_DISTRO) { $env:GNOME_DISTRO } else { "Ubuntu-24.04" }
$linux = wsl -d $distro --exec bash "$(wsl -d $distro --exec wslpath -a $PSScriptRoot)/perf.sh" $Seconds   # takes $Seconds
$after = WinTimes
foreach ($k in $after.Keys) { "{0,-28} {1,5:N0} %" -f $k, (($after[$k] - $before[$k]) * 100 / $Seconds) }
$linux
