#!/usr/bin/env bash
# Convert a FRIDA plan.json to G-code and validate it against the virtual printer's
# real kinematics using Klipper's batch mode.
#
# Batch mode runs the genuine move planner, limit checks and kinematics, but with no
# real-time MCU deadline -- so it reports move-out-of-range and a print-time estimate
# without the "Timer too close" shutdowns that live simulavr suffers on this host.
#
# Usage: validate_plan.sh <plan.json> [extra frida_plan_to_gcode.py args...]
#   e.g. validate_plan.sh myplan.json --fit
#        validate_plan.sh myplan.json --bed-x 220 --bed-y 220 --draw-feed 1200
set -u

PROJECT="${PROJECT:-$HOME/virtual-klipper-printer}"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN="${1:-}"
[ -n "$PLAN" ] || { echo "usage: $0 <plan.json> [converter args...]" >&2; exit 2; }
shift

GDIR="$PROJECT/printer_data/gcodes"
NAME="$(basename "${PLAN%.*}")"
OUT="$GDIR/${NAME}.gcode"
mkdir -p "$GDIR"

echo "=============== 1. convert ==============="
python3 "$TOOLS/frida_plan_to_gcode.py" "$PLAN" -o "$OUT" "$@"
CONV=$?

echo
echo "=============== 2. validate on Klipper (batch mode) ==============="
docker exec printer bash -lc "
cd /home/printer/klipper && \
/home/printer/python-env/bin/python ./klippy/klippy.py \
  /home/printer/printer_data/config/printer.cfg \
  -i /home/printer/printer_data/gcodes/${NAME}.gcode \
  -o /home/printer/printer_data/gcodes/${NAME}.out \
  -d /home/printer/klipper/out/klipper.dict 2>&1" > /tmp/batch_$$.log 2>&1

echo "--- problems reported by Klipper ---"
if grep -iE "out of range|must home|Error|Traceback|shutdown" /tmp/batch_$$.log; then
  RESULT="REJECTED"
else
  echo "(none)"
  RESULT="ACCEPTED"
fi

echo
echo "--- Klipper's own estimate ---"
grep -E "Exiting \(print time" /tmp/batch_$$.log || echo "(no print time - run did not complete)"

echo
echo "=============== verdict ==============="
echo "bounds check : $([ $CONV -eq 0 ] && echo PASS || echo FAIL)"
echo "kinematics   : $RESULT"
echo "gcode        : $OUT"
echo "full log     : /tmp/batch_$$.log"
[ "$RESULT" = "ACCEPTED" ] && [ $CONV -eq 0 ]
