# Starts GNOME Shell (WSL, fullscreen) and the bridge that shows Windows apps inside it.
#   start-gnome.ps1          start (restarts if already running)
#   start-gnome.ps1 stop     stop everything
#   $env:GNOME_DISTRO        WSL distro with the GNOME to run (default Ubuntu-24.04, GNOME 46)
param([string]$Action = "start")
$distro = if ($env:GNOME_DISTRO) { $env:GNOME_DISTRO } else { "Ubuntu-24.04" }
$env:GNOME_DISTRO = $distro   # winbridge.py finds GNOME's window by it
$dir = wsl -d $distro --exec wslpath -a $PSScriptRoot   # this folder and Windows' TEMP (logs), as WSL paths
$tmp = wsl -d $distro --exec wslpath -a $env:TEMP
$bridge = Join-Path $PSScriptRoot "winbridge.py"

# --exec: no extra Linux shell in between, so $(...) is expanded by our bash -c, not earlier
function Linux([string]$script) { wsl -d $distro --exec bash -c $script }

# Long-running Linux process inside a hidden wsl.exe. WSL shuts the whole VM (and WSLg with it)
# down once no wsl.exe is attached, so each of these keeps GNOME alive while it runs.
function LinuxHidden([string]$script) {
    Start-Process wsl -ArgumentList "-d $distro --exec bash -c `"$script`"" -WindowStyle Hidden
}

# stop: GNOME (the viewer dies with GNOME's Wayland socket), then the Windows bridge. GNOME first, so
# the bridge sees it go and puts the window it shows natively back under it, unclipped.
# no inner quotes: PowerShell 5.1 mangles them. Wait until the old GNOME is really gone, or the new
# one can't take the Wayland socket lock and dies. A crashed one leaves its socket behind, which would
# pass the wait for the new one below at once: remove it.
Linux 'pkill -x gnome-shell; pkill -f ^python3.-u.*gnomebridge; pkill -f ^python3.-u.*gnomeradios; for i in $(seq 40); do pgrep -x gnome-shell >/dev/null || break; sleep 0.25; done; rm -f /run/user/$(id -u)/gnome-nested /run/user/$(id -u)/gnome-nested.lock; true'
Get-CimInstance Win32_Process -Filter "Name like 'py%'" | Where-Object { $_.CommandLine -match 'win(bridge|radios)\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
if ($Action -eq "stop") { return }

LinuxHidden "bash $dir/gnome-nested.sh >$tmp/gnome-nested.log 2>&1"
# wait for GNOME's Wayland socket before starting the viewer inside it
Linux 'for i in $(seq 180); do [ -S /run/user/$(id -u)/gnome-nested ] && exit 0; sleep 0.5; done; exit 1'
if ($LASTEXITCODE -ne 0) { Write-Error "GNOME did not start, see $env:TEMP\gnome-nested.log"; return }
LinuxHidden "XDG_RUNTIME_DIR=/run/user/1000 GDK_BACKEND=wayland WAYLAND_DISPLAY=gnome-nested python3 -u $dir/gnomebridge.py >$tmp/gnomebridge.log 2>&1"
Start-Sleep 2
$log = Join-Path $env:TEMP "winbridge.log"
Start-Process py -ArgumentList "-3", "-u", "`"$bridge`"" -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err"
# Wi-Fi and Bluetooth for GNOME's settings (gnomeradios.py, started with GNOME, waits for it)
$log = Join-Path $env:TEMP "winradios.log"
Start-Process py -ArgumentList "-3", "-u", "`"$(Join-Path $PSScriptRoot "winradios.py")`"" -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err"
