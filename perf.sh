#!/bin/bash
# CPU use of the Linux side over N seconds (default 10), in % of one core.
secs=${1:-10}
pids="$(pgrep -x gnome-shell) $(pgrep -x mutter-devkit) $(pgrep -f '^python3 -u .*gnomebridge')"
ticks() { for p in $pids; do echo "$p $(awk '{print $14 + $15}' /proc/$p/stat 2>/dev/null)"; done; }
before=$(ticks)
sleep "$secs"
after=$(ticks)
hz=$(getconf CLK_TCK)
join <(echo "$before" | sort) <(echo "$after" | sort) | while read -r pid t0 t1; do
    printf "%-28s %5.0f %%\n" "$(ps -o comm= -p "$pid")" "$(echo "($t1 - $t0) * 100 / $hz / $secs" | bc -l)"
done
