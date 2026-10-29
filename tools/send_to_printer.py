#!/usr/bin/env python3
"""Send plotter G-code to a real Klipper printer through Moonraker, and drive the job.

Uploads to the printer's virtual_sdcard and starts it there rather than streaming
commands. That matters for long plots: a streamed job dies with the client, while an
sdcard job keeps drawing if the laptop sleeps, the wifi drops, or the script is killed.

Stdlib only -- no pip install on the printer or the laptop.

  send_to_printer.py --host 192.168.1.50 plan.gcode            upload and start
  send_to_printer.py --host p.local plan.gcode --upload-only   stage it, start by hand
  send_to_printer.py --host p.local --watch                    follow progress
  send_to_printer.py --host p.local --pause | --resume | --cancel
  send_to_printer.py --host p.local --estop                    M112, then firmware restart
  send_to_printer.py --host p.local --preflight                checks, change nothing
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


def _url(host, path):
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/") + path


def api(host, path, method="GET", body=None, ctype=None, timeout=TIMEOUT, retries=3):
    """One Moonraker call.

    Transient network faults are retried with backoff: over wifi, across a multi-hour
    plot, a reset connection is routine and must not take the tool down. An HTTP error
    is the printer answering with a refusal, so that is never retried.
    """
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(_url(host, path), data=body, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw)["error"]["message"].splitlines()[0]
            except Exception:
                msg = raw[:200]
            raise SystemExit("error: HTTP %d from %s\n  %s" % (e.code, path, msg))
        except (urllib.error.URLError, OSError) as e:
            # URLError subclasses OSError; ConnectionResetError does not, so catch both.
            last = e
            reason = getattr(e, "reason", e)
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                print("  warning: %s unreachable (%s), retry %d/%d in %ds"
                      % (host, reason, attempt, retries - 1, wait), file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit(
                "error: cannot reach %s after %d tries (%s)\n"
                "  check the host, that Moonraker is on port 7125, and the network.\n"
                "  an sdcard print keeps running even when this tool cannot connect."
                % (_url(host, path), retries, reason))
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def gcode(host, script, timeout=TIMEOUT):
    return api(host, "/printer/gcode/script?script=" + urllib.parse.quote(script),
               method="POST", timeout=timeout)


def query(host, objects):
    q = "&".join(objects)
    return api(host, "/printer/objects/query?" + q)["result"]["status"]


def state(host):
    return api(host, "/server/info")["result"]["klippy_state"]


def upload(host, path, root="gcodes"):
    """Multipart upload to Moonraker's file manager."""
    name = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    boundary = "----plotter%d" % int(time.time() * 1000)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    parts = []
    for field, value in (("root", root), ("print", "false")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, field, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, name, ctype)).encode())
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    api(host, "/server/files/upload", method="POST", body=body,
        ctype="multipart/form-data; boundary=" + boundary, timeout=300)
    return name


PROFILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pen_profiles.json")


def read_offset(host):
    """Current live Z offset (gcode_move.homing_origin[2])."""
    s = query(host, ["gcode_move=homing_origin,gcode_position"])
    gm = s.get("gcode_move", {})
    return (gm.get("homing_origin") or [0, 0, 0, 0])[2]


def load_profiles():
    if not os.path.exists(PROFILES):
        return {}
    try:
        with open(PROFILES) as f:
            return json.load(f)
    except (ValueError, OSError):
        print("warning: %s is unreadable, ignoring it" % PROFILES)
        return {}


def save_profiles(d):
    with open(PROFILES, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")


def do_offsets(host, args):
    """Read / set / nudge / persist the pen Z offset. None of this touches the G-code."""
    if args.read_offset:
        print("live Z offset : %+.4f mm" % read_offset(host))
        prof = load_profiles()
        if prof:
            print("saved pen profiles (%s):" % PROFILES)
            for k in sorted(prof):
                print("  %-16s Z%+.4f   %s" % (k, prof[k].get("z", 0.0),
                                               prof[k].get("note", "")))
        return 0

    if args.set_z_offset is not None:
        before = read_offset(host)
        gcode(host, "SET_GCODE_OFFSET Z=%.4f" % args.set_z_offset)
        print("Z offset %+.4f -> %+.4f mm" % (before, read_offset(host)))
        print("  not persistent: lost on restart. --save-pen to keep it.")
        return 0

    if args.adjust_z is not None:
        before = read_offset(host)
        # Z_ADJUST is relative and safe to send mid-plot -- this is live babystepping.
        gcode(host, "SET_GCODE_OFFSET Z_ADJUST=%.4f" % args.adjust_z)
        print("Z offset %+.4f -> %+.4f mm  (delta %+.4f)"
              % (before, read_offset(host), args.adjust_z))
        return 0

    if args.save_pen:
        z = read_offset(host)
        prof = load_profiles()
        prof[args.save_pen] = {"z": z, "note": args.note or ""}
        save_profiles(prof)
        print("saved pen '%s' at Z%+.4f mm -> %s" % (args.save_pen, z, PROFILES))
        print("  apply later with: --use-pen %s" % args.save_pen)
        return 0

    if args.use_pen:
        prof = load_profiles()
        if args.use_pen not in prof:
            print("error: no pen profile '%s'. Known: %s"
                  % (args.use_pen, ", ".join(sorted(prof)) or "(none)"))
            return 1
        z = prof[args.use_pen]["z"]
        before = read_offset(host)
        gcode(host, "SET_GCODE_OFFSET Z=%.4f" % z)
        print("pen '%s': Z offset %+.4f -> %+.4f mm"
              % (args.use_pen, before, read_offset(host)))
        # Combined with a file, apply the pen then fall through and send it.
        return None if args.gcode else 0

    if args.list_pens:
        prof = load_profiles()
        if not prof:
            print("no pen profiles yet (%s)" % PROFILES)
            return 0
        for k in sorted(prof):
            print("  %-16s Z%+.4f   %s" % (k, prof[k].get("z", 0.0),
                                           prof[k].get("note", "")))
        return 0
    return None


def preflight(host, args):
    """Read-only checks worth doing before a multi-hour plot. Changes nothing."""
    print("=== preflight: %s ===" % host)
    ok = True

    st = state(host)
    print("  klippy_state        : %s" % st)
    if st != "ready":
        print("    -> not ready; FIRMWARE_RESTART or fix the config before plotting")
        ok = False

    try:
        s = query(host, ["toolhead=homed_axes,axis_maximum,position",
                         "gcode_move=homing_origin,gcode_position",
                         "print_stats=state,filename",
                         "idle_timeout=timeout,state"])
    except SystemExit:
        return False

    th = s.get("toolhead", {})
    print("  homed axes          : %-6s %s" % (th.get("homed_axes", "") or "none",
          "" if th.get("homed_axes") == "xyz" else "<- home before plotting"))
    am = th.get("axis_maximum")
    if am:
        print("  axis maximum        : X%.1f Y%.1f Z%.1f" % (am[0], am[1], am[2]))
        print("    -> pass these to frida_plan_to_gcode.py as --bed-x %.0f --bed-y %.0f"
              % (am[0], am[1]))

    off = s.get("gcode_move", {}).get("homing_origin")
    if off:
        print("  gcode offset Z      : %+.3f mm   (SET_GCODE_OFFSET; your pen offset lives here)"
              % off[2])

    ps = s.get("print_stats", {})
    print("  print_stats         : %s %s" % (ps.get("state"), ps.get("filename") or ""))
    if ps.get("state") in ("printing", "paused"):
        print("    -> a job is already active; cancel it first")
        ok = False

    it = s.get("idle_timeout", {})
    t = it.get("timeout")
    if t is not None:
        print("  idle_timeout        : %.0f s" % t)
        if t < args.min_idle_timeout:
            print("    -> WARNING: a pen-change pause longer than this disables the")
            print("       steppers and loses position. Raise it, or keep pauses short:")
            print("       SET_IDLE_TIMEOUT TIMEOUT=%d" % args.min_idle_timeout)

    # Heaters should be cold and stay cold for a pen plot.
    try:
        h = query(host, ["extruder=target,temperature", "heater_bed=target,temperature"])
        for k in ("extruder", "heater_bed"):
            if k in h:
                print("  %-19s : %.1fC (target %.1fC)%s"
                      % (k, h[k].get("temperature", 0), h[k].get("target", 0),
                         "   <- should be 0 for plotting" if h[k].get("target", 0) else ""))
    except SystemExit:
        pass

    print("  verdict             : %s" % ("looks ready" if ok else "NOT ready"))
    return ok


def watch(host, interval=5.0):
    print("watching (ctrl-c to stop; the printer keeps going either way)")
    last = None
    while True:
        try:
            s = query(host, ["print_stats=state,filename,print_duration,total_duration",
                             "display_status=progress,message",
                             "toolhead=position"])
        except SystemExit as e:
            print("  lost contact: %s" % e)
            time.sleep(interval)
            continue
        ps, ds = s.get("print_stats", {}), s.get("display_status", {})
        pos = s.get("toolhead", {}).get("position", [0, 0, 0, 0])
        line = ("  %-9s %5.1f%%  elapsed %6.0fs  X%7.2f Y%7.2f Z%6.2f  %s"
                % (ps.get("state", "?"), 100 * (ds.get("progress") or 0),
                   ps.get("print_duration") or 0, pos[0], pos[1], pos[2],
                   (ds.get("message") or "")[:30]))
        if line != last:
            print(line)
            last = line
        if ps.get("state") in ("complete", "cancelled", "error", "standby"):
            print("  finished: %s" % ps.get("state"))
            return 0 if ps.get("state") == "complete" else 1
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gcode", nargs="?", help="local .gcode to upload")
    ap.add_argument("--host", required=True, help="printer host/IP (Moonraker, port 7125)")
    ap.add_argument("--upload-only", action="store_true", dest="upload_only")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--cancel", action="store_true")
    ap.add_argument("--estop", action="store_true", help="M112 emergency stop")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--qgl", action="store_true",
                    help="run QUAD_GANTRY_LEVEL first (SV08: do this before plotting)")
    g = ap.add_argument_group("pen Z offset (never requires regenerating G-code)")
    g.add_argument("--read-offset", action="store_true", dest="read_offset",
                   help="print the live Z offset and any saved pen profiles")
    g.add_argument("--set-z-offset", type=float, default=None, dest="set_z_offset",
                   help="SET_GCODE_OFFSET Z=<v> absolute; lost on restart")
    g.add_argument("--adjust-z", type=float, default=None, dest="adjust_z",
                   help="SET_GCODE_OFFSET Z_ADJUST=<d> relative; safe MID-PLOT")
    g.add_argument("--save-pen", default=None, dest="save_pen",
                   help="store the live offset under this pen name")
    g.add_argument("--use-pen", default=None, dest="use_pen",
                   help="apply a stored pen's offset")
    g.add_argument("--list-pens", action="store_true", dest="list_pens")
    g.add_argument("--note", default=None, help="note to store with --save-pen")

    ap.add_argument("--min-idle-timeout", type=int, default=3600, dest="min_idle_timeout")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not args.host.replace(".", "").replace(":", "").replace("-", "").isalnum() \
            and "://" not in args.host:
        pass  # hostnames vary; let the request fail with a clear message instead

    rc = do_offsets(args.host, args)
    if rc is not None:
        return rc

    if args.estop:
        print("sending M112 emergency stop")
        try:
            api(args.host, "/printer/emergency_stop", method="POST")
        except SystemExit as e:
            print(e)
        print("  klippy_state: %s" % state(args.host))
        print("  recover with: curl -X POST http://%s:7125/printer/firmware_restart"
              % args.host.split("://")[-1].split(":")[0])
        return 0

    for flag, path in (("pause", "/printer/print/pause"),
                       ("resume", "/printer/print/resume"),
                       ("cancel", "/printer/print/cancel")):
        if getattr(args, flag):
            print("%s ..." % flag)
            api(args.host, path, method="POST")
            s = query(args.host, ["print_stats=state"])
            print("  print_stats.state = %s" % s.get("print_stats", {}).get("state"))
            return 0

    if args.status or args.preflight:
        ok = preflight(args.host, args)
        return 0 if ok else 1

    if args.watch and not args.gcode:
        return watch(args.host)

    if not args.gcode:
        ap.error("give a .gcode file, or one of --status/--watch/--pause/--resume/--cancel")

    if not os.path.isfile(args.gcode):
        raise SystemExit("error: no such file: %s" % args.gcode)

    if not preflight(args.host, args):
        if not args.yes:
            raise SystemExit("preflight failed; re-run with --yes to send anyway")
        print("preflight failed but --yes given, continuing")
    print()

    if args.qgl:
        print("running QUAD_GANTRY_LEVEL (this takes a minute)")
        gcode(args.host, "QUAD_GANTRY_LEVEL", timeout=600)
        print("  done")

    name = upload(args.host, args.gcode)
    size = os.path.getsize(args.gcode)
    moves = sum(1 for l in open(args.gcode) if l.startswith("G1"))
    print("uploaded %s (%.1f KB, %d G1 moves) to the printer's gcodes/" % (name, size / 1024.0, moves))

    if args.upload_only:
        print("--upload-only: start it from Mainsail/Fluidd, or:")
        print("  curl -X POST 'http://%s/printer/print/start?filename=%s'"
              % (args.host, urllib.parse.quote(name)))
        return 0

    if not args.yes:
        print()
        print("About to START PLOTTING. Check before answering:")
        print("  - pen mounted, and its Z offset set (SET_GCODE_OFFSET Z=...)")
        print("  - paper taped down flat")
        print("  - heaters off, nothing in the gantry's way")
        try:
            if input("start the plot? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not started; the file is uploaded and waiting")
                return 0
        except EOFError:
            print("no tty for the prompt; re-run with --yes to start non-interactively")
            return 0

    api(args.host, "/printer/print/start?filename=" + urllib.parse.quote(name),
        method="POST")
    print("started: %s" % name)
    return watch(args.host)


if __name__ == "__main__":
    sys.exit(main())
