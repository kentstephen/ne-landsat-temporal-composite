#!/bin/zsh
# Watch the v4 rebuild chain and validate tiles off the store as they land.
# Stdout is the event stream (one line each): TILE FLAG, YEAR_DONE plus the
# check_rebuilt summary, FAIL, CHAIN, DEAD, HEARTBEAT, END. OK tiles go to
# data/logs/validate.nbr3.log only. Polls every 60 s. Read-only on the store.
cd "$(dirname "$0")/../.."
L8=data/logs/build.nbr3.l8era.log; L5=data/logs/build.nbr3.l5era.log
CH=data/logs/nbr3_chain.log; RF=data/logs/refresh.nbr3.log; VL=data/logs/validate.nbr3.log
typeset -A need done_seen tile_seen
for f in data/l7fill_jobs_l8era.txt data/l7fill_jobs_l5era.txt; do
  while read t y; do need[$y]=$(( ${need[$y]:-0} + 1 )); done < $f
done
seen_fail=$(cat $L8 $L5 2>/dev/null | grep -E -c "Traceback|Error|Killed|MemoryError")
seen_failcount=$(grep -h progress $L8 $L5 2>/dev/null | tail -1 | sed -E 's/.*\(([0-9]+) failed\).*/\1/'); seen_failcount=${seen_failcount:-0}
ch_n=$(wc -l < $CH 2>/dev/null || echo 0); rf_n=$(wc -l < $RF 2>/dev/null || echo 0)
last_hb=$(date +%s); dead_reported=0; nval=0; nflag=0
done_lines() { cat $L8 $L5 2>/dev/null | grep -E "^r[0-9][0-9]c[0-9][0-9] [0-9]{4}: done in" | awk '{print $1" "substr($2,1,4)}'; }
count_year() { done_lines | grep -c " $1$"; }
clean() { grep -v -E "WARN|at icechunk|^$"; }
# tiles and years already done at start are not re-validated (2013 was checked by hand)
done_lines | while read t y; do tile_seen["$t $y"]=1; done
for y in ${(k)need}; do [[ $(count_year $y) -ge ${need[$y]} ]] && done_seen[$y]=1; done
while true; do
  done_lines | while read t y; do
    if [[ -z ${tile_seen["$t $y"]:-} ]]; then
      tile_seen["$t $y"]=1
      line=$(uv run python src/landsat_mosaic/validate_tile.py $t $y 2>&1 | clean | tail -1)
      echo "[$(date '+%m-%d %H:%M')] $line" >> $VL; nval=$((nval+1))
      case $line in OK*) ;; *) nflag=$((nflag+1)); echo "TILE $line" | cut -c1-400 ;; esac
    fi
  done
  for y in ${(ko)need}; do
    if [[ -z ${done_seen[$y]:-} ]]; then
      c=$(count_year $y)
      if [[ $c -ge ${need[$y]} ]]; then
        done_seen[$y]=1; log=$L8; [[ $y -le 2012 ]] && log=$L5
        echo "YEAR_DONE $y ($c/${need[$y]} tiles) $(date '+%H:%M')"
        uv run python src/landsat_mosaic/check_rebuilt.py $y $log 2>&1 | clean | tee -a $VL | sed 's/^/  /'
      fi
    fi
  done
  f=$(cat $L8 $L5 2>/dev/null | grep -E -c "Traceback|Error|Killed|MemoryError")
  if [[ $f -gt $seen_fail ]]; then
    echo "FAIL new failure signature(s) in build logs:"; cat $L8 $L5 2>/dev/null | grep -E "Traceback|Error|Killed|MemoryError" | tail -n $(( f - seen_fail )) | cut -c1-200; seen_fail=$f
  fi
  # failed-job count from the latest progress line; announce only when it grows
  fc=$(grep -h progress $L8 $L5 2>/dev/null | tail -1 | sed -E 's/.*\(([0-9]+) failed\).*/\1/'); fc=${fc:-0}
  if [[ $fc -gt $seen_failcount ]]; then echo "FAIL job count now $fc: $(grep -h progress $L8 $L5 2>/dev/null | tail -1)"; seen_failcount=$fc; fi
  n=$(wc -l < $CH 2>/dev/null || echo 0); if [[ $n -gt $ch_n ]]; then sed -n "$((ch_n+1)),${n}p" $CH | sed 's/^/CHAIN /'; ch_n=$n; fi
  n=$(wc -l < $RF 2>/dev/null || echo 0); if [[ $n -gt $rf_n ]]; then sed -n "$((rf_n+1)),${n}p" $RF | sed 's/^/REFRESH /'; rf_n=$n; fi
  if ! pgrep -f "nbr3_chain.sh|batch.py|after_batch.sh|refresh_pyramid.sh" >/dev/null; then
    if [[ $dead_reported -eq 0 ]]; then echo "DEAD no chain/batch/refresh process running $(date '+%H:%M'); last progress: $(grep progress $L8 $L5 2>/dev/null | tail -1)"; dead_reported=1; fi
    if grep -q -E "refresh done|check failed|build failed|short, stopping" $CH $RF 2>/dev/null; then echo "END chain finished: $(grep -h -E 'refresh done|check failed|build failed|short, stopping' $CH $RF | tail -1)"; exit 0; fi
  else dead_reported=0; fi
  now=$(date +%s); if [[ $(( now - last_hb )) -ge 7200 ]]; then echo "HEARTBEAT validated $nval tiles, $nflag flagged | $(grep -h progress $L8 $L5 2>/dev/null | tail -1)"; last_hb=$now; fi
  sleep 60
done
