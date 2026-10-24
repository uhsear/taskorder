#!/usr/bin/env python3
"""taskorder - tell "the refresh never ran" from "nothing changed".

A nightly chain of scheduled tasks (snapshot, refresh, compare) can end in a
compare report with zero rows. That report is the same file whether nothing
changed, the refresh never ran, the refresh failed, or the snapshot ran AFTER
the refresh and so copied the new data over the old. This tool reads a run
history (one row per task run) and the declared order of the chain, and says
which one it was. An empty history is UNKNOWN, never "nothing changed".

    python taskorder.py --history runs.csv --chain snapshot,refresh,compare --rows 0
    python taskorder.py ... --now "2026-03-06 07:00:00"
    python taskorder.py ... --report order.txt --apply
    python taskorder.py --self-test

History columns (any case, CSV or a JSON array of objects):
    task, started, finished, result
started and finished are YYYY-MM-DD HH:MM:SS (24-hour, or with T), with an
optional Z or +HH:MM. result is ok or fail. finished may be blank only for a
fail row. --now is when the history was exported, in the same form; without it
the system clock is used. A last stage that has not run for more than one
cycle before that time is NOT_RUN.

Nothing is written without --apply, and only the --report file is ever
written. The tool needs only the Python standard library, no network and no
credentials. Python 3.9 or newer.
"""
import argparse
import csv
import datetime
import importlib.util
import io
import json
import math
import os
import re
import sys
import tempfile
from collections import namedtuple

CHANGED = "CHANGED"
NOTHING_CHANGED = "NOTHING_CHANGED"
ORDERED = "ORDERED"
NOT_RUN = "NOT_RUN"
FAILED = "FAILED"
INVERTED = "INVERTED"
UNKNOWN = "UNKNOWN"

# Definite problems beat UNKNOWN. Among them, this order picks the headline.
DEFINITE = (NOT_RUN, FAILED, INVERTED)

EXIT = {CHANGED: 0, NOTHING_CHANGED: 0, ORDERED: 0,
        NOT_RUN: 1, FAILED: 1, INVERTED: 1, UNKNOWN: 3}

MEANING = {
    CHANGED: "the chain ran in order and the last stage found rows",
    NOTHING_CHANGED: "the chain ran in order and the last stage found no rows",
    ORDERED: "the chain ran in order (no row count was given)",
    NOT_RUN: "a stage did not run when it should have; an empty report "
             "proves nothing",
    FAILED: "a stage ran and failed; an empty report proves nothing",
    INVERTED: "stages ran out of the declared order; the last stage compared "
              "the wrong data",
    UNKNOWN: "the history cannot answer; do not read an empty report as "
             "'nothing changed'",
}

DEFAULT_WINDOW_HOURS = 24.0
FIELDS = ("task", "started", "finished", "result")

Run = namedtuple("Run", "task key started finished ok utc")

_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
                   r"(Z|[+-]\d{2}:\d{2})?", re.ASCII)


class InputError(ValueError):
    """The input or the flags cannot be used. The tool exits 2."""


def stamp(when):
    """YYYY-MM-DD HH:MM:SS. strftime('%Y') pads years below 1000 on Windows
    and not on Linux, so the report would differ by host."""
    return "%04d-%02d-%02d %02d:%02d:%02d" % (
        when.year, when.month, when.day, when.hour, when.minute, when.second)


def system_now(utc):
    """The clock, in the same form as the history: UTC when its times carry
    an offset, local wall time when they do not."""
    if utc:
        return datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None)
    return datetime.datetime.now()


def safe(text):
    """Printable ASCII only, so a name cannot forge a line or hide one."""
    return re.sub(r"[^\x20-\x7e]", "?", str(text))


# ---------------------------------------------------------------- reading

def parse_time(text, label):
    """(naive datetime, was_aware). An offset is folded into UTC."""
    m = _TIME.fullmatch(text.strip())
    if not m:
        raise InputError("%s: '%s' is not YYYY-MM-DD HH:MM:SS (24-hour) with "
                         "an optional Z or +HH:MM" % (label, safe(text)))
    y, mo, d, h, mi, s = [int(g) for g in m.groups()[:6]]
    try:
        when = datetime.datetime(y, mo, d, h, mi, s)
    except ValueError as exc:
        raise InputError("%s: '%s' is not a real time (%s)"
                         % (label, safe(text), exc))
    zone = m.group(7)
    if zone is None:
        return when, False
    if zone == "Z":
        return when, True
    oh, om = int(zone[1:3]), int(zone[4:6])
    if oh > 23 or om > 59:
        raise InputError("%s: '%s' has an impossible offset"
                         % (label, safe(text)))
    delta = datetime.timedelta(hours=oh, minutes=om)
    return (when + delta if zone[0] == "-" else when - delta), True


def runs_from_pairs(records):
    """Run list from a list of rows, each a list of (column, value) pairs."""
    runs = []
    aware = set()
    for n, pairs in enumerate(records, 1):
        label = "row %d" % n
        low = {}
        for key, value in pairs:
            name = key.strip().casefold()
            if name in low:
                raise InputError("%s: column '%s' appears twice"
                                 % (label, safe(name)))
            low[name] = value
        vals = {}
        for field in FIELDS:
            if field not in low:
                raise InputError("%s: no '%s' column or cell" % (label, field))
            value = low[field]
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise InputError("%s: '%s' must be text" % (label, field))
            vals[field] = value.strip()
        if not vals["task"]:
            raise InputError("%s: task is blank" % label)
        result = vals["result"].casefold()
        if result not in ("ok", "fail"):
            raise InputError("%s: result '%s' is not ok or fail"
                             % (label, safe(vals["result"])))
        started, a1 = parse_time(vals["started"], label + " started")
        aware.add(a1)
        finished = None
        if vals["finished"]:
            finished, a2 = parse_time(vals["finished"], label + " finished")
            aware.add(a2)
            if finished < started:
                raise InputError("%s: finished is before started" % label)
        elif result == "ok":
            raise InputError("%s: an ok run needs a finished time" % label)
        runs.append(Run(vals["task"], vals["task"].casefold(), started,
                        finished, result == "ok", a1))
    if len(aware) > 1:
        raise InputError("the history mixes times with and without a "
                         "Z or +HH:MM offset; use one form")
    return runs


def load_runs(path):
    """Read a history file. A blank file is an empty history, not an error."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8-sig")
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (safe(path), exc.strerror))
    except UnicodeDecodeError:
        raise InputError("%s is not UTF-8 text" % safe(path))
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise InputError("%s is not valid JSON (%s)" % (safe(path), exc))
        except RecursionError:
            raise InputError("%s is nested too deeply for JSON" % safe(path))
        if not isinstance(data, list):
            raise InputError("%s: JSON must be an array of objects"
                             % safe(path))
        pairs = []
        for item in data:
            if not isinstance(item, dict):
                raise InputError("%s: JSON must be an array of objects"
                                 % safe(path))
            pairs.append(list(item.items()))
        return runs_from_pairs(pairs)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise InputError("%s is not readable CSV (%s)" % (safe(path), exc))
    header = rows[0]
    pairs = []
    for row in rows[1:]:
        if not row:
            continue
        if len(row) > len(header):
            raise InputError("%s: a row has more cells than the header"
                             % safe(path))
        pairs.append(list(zip(header, row)))
    return runs_from_pairs(pairs)


def parse_chain(text):
    """Stage names in order. At least two, none blank, none repeated."""
    names = [part.strip() for part in text.split(",")]
    if any(not name for name in names):
        raise InputError("--chain has a blank stage name")
    if len(names) < 2:
        raise InputError("--chain needs at least two stages, separated by "
                         "commas")
    if len(set(name.casefold() for name in names)) != len(names):
        raise InputError("--chain names a stage twice")
    return names


# ---------------------------------------------------------------- decision

def _end(run):
    return run.finished if run.finished is not None else run.started


def analyse(runs, chain, rows=None, window_hours=DEFAULT_WINDOW_HOURS,
            now=None):
    """The pure decision core. No I/O.

    Judges the latest ok run of the last stage. Walking back, each earlier
    stage must have an ok run that finished before the next stage started and
    inside one cycle (window_hours before the judged run). A run of an earlier
    stage that starts after a later chosen stage finished, but before the
    judged run, means the order was broken. The judged run is only believed
    if nothing newer contradicts it: a failed run of any stage after it, or a
    chain that ran on for more than one cycle without a newer last-stage run.
    With now (the same form as the run times), a last stage with no run for
    more than one cycle since the judged run finished is NOT_RUN. The core
    calls no clock: with now=None that check is skipped.
    """
    keys = [name.casefold() for name in chain]
    last = len(chain) - 1
    findings = []

    def add(code, stage, text):
        findings.append({"code": code, "stage": stage, "text": text})

    report = {"chain": list(chain), "window_hours": window_hours,
              "rows": rows, "judged": None, "chosen": {},
              "findings": findings, "verdict": None}
    if not runs:
        add(UNKNOWN, None, "the history holds no runs")
        return _finish(report)
    done = [r for r in runs if r.key == keys[last] and r.ok]
    if not done:
        add(UNKNOWN, chain[last], "no successful run of %s in the history"
            % chain[last])
        return _finish(report)
    judged = max(done, key=lambda r: (r.started, r.finished))
    report["judged"] = judged
    start = judged.started
    newest = max(_end(r) for r in runs)
    if now is not None and now < newest:
        add(UNKNOWN, None, "now is %s, before the newest run in the history "
            "at %s; check --now, the clock and the time zone"
            % (stamp(now), stamp(newest)))
        now = None
    floor = start - datetime.timedelta(hours=window_hours)
    covered = min(r.started for r in runs) <= floor
    chosen = {last: judged}
    # The newest evidence beats the judged run. The tool has no clock, so
    # "newer" means newer than the judged run inside the history.
    for i, key in enumerate(keys):
        bad = [r.started for r in runs
               if r.key == key and not r.ok and r.started > start]
        if bad:
            add(FAILED, chain[i], "%s failed at %s, after the judged %s "
                "started at %s" % (chain[i], stamp(max(bad)),
                                   chain[last], stamp(start)))
    ahead = [r.started for r in runs if r.key in keys
             and r.started > start + datetime.timedelta(hours=window_hours)]
    no_newer = not any(r.key == keys[last] and r.started > start
                       for r in runs)
    if ahead and no_newer:
        add(NOT_RUN, chain[last], "no run of %s after the judged one at %s, "
            "but the chain ran as late as %s, more than %s h later"
            % (chain[last], stamp(start), stamp(max(ahead)),
               _hours(window_hours)))
    elif (now is not None and no_newer and now > judged.finished
          + datetime.timedelta(hours=window_hours)):
        add(NOT_RUN, chain[last], "no run of %s after the judged one at %s, "
            "and now is %s, more than %s h after it finished"
            % (chain[last], stamp(start), stamp(now), _hours(window_hours)))
    bound = start
    earlier = [r.started for r in done if r.started < start]
    prev = max(earlier) if earlier else None
    for i in range(last - 1, -1, -1):
        mine = [r for r in runs if r.key == keys[i]
                and _end(r) >= floor and r.started <= start]
        ok = [r for r in mine if r.ok]
        before = [r for r in ok if r.finished <= bound]
        if before:
            pick = max(before, key=lambda r: r.finished)
            # An ok run from before the previous compare belongs to the
            # previous cycle. A failed run of this stage since then is this
            # cycle's, and the stage did not refresh.
            lost = [r.started for r in runs
                    if r.key == keys[i] and not r.ok
                    and prev is not None and prev < r.started <= start
                    and pick.finished <= prev]
            if lost:
                add(FAILED, chain[i], "%s failed at %s in this cycle, and its "
                    "only ok run, at %s, was before the previous %s at %s"
                    % (chain[i], stamp(max(lost)), stamp(pick.started),
                       chain[last], stamp(prev)))
                continue
            chosen[i] = pick
            bound = pick.started
        elif [r for r in ok if r.finished > bound]:
            late = max(r.started for r in ok if r.finished > bound)
            add(INVERTED, chain[i], "%s only ran at %s, after %s had started "
                "at %s" % (chain[i], stamp(late), chain[i + 1],
                           stamp(bound)))
        elif mine:
            add(FAILED, chain[i], "%s ran in this cycle and every run failed"
                % chain[i])
        elif covered:
            add(NOT_RUN, chain[i], "no run of %s in the %s h before %s "
                "started" % (chain[i], _hours(window_hours), chain[last]))
        else:
            add(UNKNOWN, chain[i], "no run of %s in the window, and the "
                "history starts at %s, after the window opens at %s"
                % (chain[i], stamp(min(r.started for r in runs)),
                   stamp(floor)))
    report["chosen"] = chosen
    for i in sorted(chosen):
        if i == last:
            continue
        # Only stages that have a chosen run: a stage with none already has
        # its own finding above.
        for j in [k for k in chosen if k < i]:
            for r in runs:
                if (r.key == keys[j] and r.ok
                        and chosen[i].finished <= r.started <= start):
                    add(INVERTED, chain[j], "%s ran again at %s, after %s "
                        "finished at %s and before %s started"
                        % (chain[j], stamp(r.started), chain[i],
                           stamp(chosen[i].finished), chain[last]))
    return _finish(report)


def _hours(value):
    return ("%d" % value) if value == int(value) else ("%.2f" % value)


def _finish(report):
    codes = [f["code"] for f in report["findings"]]
    verdict = None
    for code in DEFINITE:
        if code in codes:
            verdict = code
            break
    if verdict is None and UNKNOWN in codes:
        verdict = UNKNOWN
    if verdict is None:
        if report["rows"] is None:
            verdict = ORDERED
        elif report["rows"] == 0:
            verdict = NOTHING_CHANGED
        else:
            verdict = CHANGED
    report["verdict"] = verdict
    return report


def render(report, utc=False):
    """The report as text lines. Every name goes through safe()."""
    chain = report["chain"]
    out = ["taskorder: chain %s, one cycle = %s h"
           % (" -> ".join(safe(c) for c in chain),
              _hours(report["window_hours"]))]
    judged = report["judged"]
    if judged is not None:
        rows = report["rows"]
        out.append("judged: %s, started %s%s, rows reported: %s"
                   % (safe(chain[-1]), stamp(judged.started),
                      " UTC" if utc else "",
                      "not given" if rows is None else rows))
        for i, name in enumerate(chain):
            run = report["chosen"].get(i)
            if run is None:
                out.append("  %-12s no run chosen" % safe(name))
            else:
                out.append("  %-12s %s .. %s  ok" % (
                    safe(name), stamp(run.started),
                    stamp(run.finished)))
    for f in report["findings"]:
        out.append("FINDING %s: %s" % (f["code"], safe(f["text"])))
    if report["rows"] == 0 and report["verdict"] in DEFINITE + (UNKNOWN,):
        out.append("NOTE: the empty report does not show that nothing "
                   "changed.")
    out.append("MEANING: %s" % MEANING[report["verdict"]])
    out.append("VERDICT: %s" % report["verdict"])
    return out


# ---------------------------------------------------------------- cli

def build_parser():
    # allow_abbrev=False: a unique prefix of --apply must not write the report.
    ap = argparse.ArgumentParser(
        prog="taskorder.py", allow_abbrev=False,
        description="Tell 'the refresh never ran' from 'nothing changed'. "
                    "Nothing is written without --apply, and only --report "
                    "is ever written.")
    ap.add_argument("--history", help="CSV or JSON run history")
    ap.add_argument("--chain", help="stage task names in order, comma "
                    "separated; the last stage is judged")
    ap.add_argument("--rows", type=int, help="rows the last stage reported")
    ap.add_argument("--window-hours", dest="window_hours", type=float,
                    help="length of one cycle (default %s)"
                    % _hours(DEFAULT_WINDOW_HOURS))
    ap.add_argument("--now", help="when the history was exported, in the "
                    "form of its times (default: the system clock)")
    ap.add_argument("--report", help="write the report to this file")
    ap.add_argument("--apply", action="store_true",
                    help="write --report; without it nothing is written")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap


def fail(message):
    sys.stderr.write("taskorder: error: %s\n" % message)
    return 2


def run_cli(args, clock=system_now):
    if not args.history:
        raise InputError("--history is required")
    if not args.chain:
        raise InputError("--chain is required")
    if args.apply and not args.report:
        raise InputError("--apply needs --report")
    window = DEFAULT_WINDOW_HOURS if args.window_hours is None \
        else args.window_hours
    if not math.isfinite(window) or window <= 0:
        raise InputError("--window-hours must be a number above 0")
    if args.rows is not None and args.rows < 0:
        raise InputError("--rows cannot be negative")
    chain = parse_chain(args.chain)
    if args.apply and (os.path.normcase(os.path.realpath(args.report))
                       == os.path.normcase(os.path.realpath(args.history))):
        raise InputError("--report is the history file; refusing to "
                         "overwrite it")
    runs = load_runs(args.history)
    utc = bool(runs) and runs[0].utc
    if args.now is None:
        now = clock(utc)
    else:
        now, aware = parse_time(args.now, "--now")
        if runs and aware != utc:
            raise InputError("--now and the history must both use a Z or "
                             "+HH:MM offset, or both none")
    report = analyse(runs, chain, args.rows, window, now)
    lines = render(report, utc=utc)
    print("\n".join(lines))
    if args.report:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % safe(args.report))
        else:
            try:
                with open(args.report, "w", encoding="ascii",
                          newline="\n") as handle:
                    handle.write("\n".join(lines) + "\n")
            except OSError as exc:
                raise InputError("cannot write %s: %s"
                                 % (safe(args.report), exc.strerror))
    return EXIT[report["verdict"]]


def main(argv=None, clock=system_now):
    args = build_parser().parse_args(argv)
    if args.self_test:
        if (args.history or args.chain or args.rows is not None
                or args.window_hours is not None or args.report
                or args.now is not None or args.apply):
            return fail("--self-test takes no other flag")
        return self_test()
    try:
        return run_cli(args, clock)
    except InputError as exc:
        return fail(str(exc))
    except OverflowError:
        return fail("a time or --window-hours is outside the range that "
                    "dates can be calculated in")


# ---------------------------------------------------------------- self-test

def finish(total, failed):
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


def harness():
    """(passed, failed, check, raises). A factory, so the self-test can run a
    second copy against deliberate failures and prove it counts them."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, exc_type=InputError, text=None):
        """fn must raise exc_type; with text, the message must hold it."""
        try:
            fn()
        except exc_type as exc:
            check(text is None or text in str(exc),
                  label if text is None or text in str(exc)
                  else "%s (message %r)" % (label, str(exc)))
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    return passed, failed, check, raises


def self_test():
    """Assertions over the decision core and the CLI. Offline."""
    passed, failed, check, raises = harness()

    noon = datetime.datetime(2026, 3, 5, 7, 0, 0)

    def cli(argv, clock_at=noon):
        """(exit code, stdout, stderr) of main(argv) in this process, with a
        fixed clock so that no assertion depends on today's date."""
        real = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            try:
                code = main(argv) if clock_at is None else main(
                    argv, clock=lambda utc: clock_at)
            except SystemExit as exc:
                code = exc.code
            return code, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = real

    def ts(day, hhmm):
        return "2026-03-%02d %s:00" % (day, hhmm)

    def mk(task, day, start, end, ok=True, end_day=None):
        return Run(task, task.casefold(), parse_time(ts(day, start), "t")[0],
                   parse_time(ts(end_day or day, end), "t")[0] if end
                   else None, ok, False)

    chain = ["snapshot", "refresh", "compare"]

    def day(d, snap="01:00", ref="02:00", cmp_="06:00"):
        out = []
        if snap:
            out.append(mk("snapshot", d, snap, snap[:3] + "02"))
        if ref:
            out.append(mk("refresh", d, ref, ref[:3] + "20"))
        if cmp_:
            out.append(mk("compare", d, cmp_, cmp_[:3] + "05"))
        return out

    healthy = day(3) + day(4) + day(5)

    print("taskorder self-test: offline, no Task Scheduler, no network, "
          "no credentials")
    print("-" * 68)

    # ---- the disaster: an empty report with three different causes
    rep = analyse(healthy, chain, rows=0)
    check(rep["verdict"] == NOTHING_CHANGED,
          "a healthy chain with an empty report is NOTHING_CHANGED")
    check(rep["judged"].started == parse_time(ts(5, "06:00"), "t")[0],
          "the latest ok run of the last stage is the one judged")
    check(sorted(rep["chosen"]) == [0, 1, 2],
          "every stage of a healthy chain has a chosen run")
    check(rep["chosen"][1].started == parse_time(ts(5, "02:00"), "t")[0],
          "the refresh chosen is today's, not yesterday's")
    check(analyse(healthy, chain, rows=12)["verdict"] == CHANGED,
          "a healthy chain with rows is CHANGED")
    check(analyse(healthy, chain)["verdict"] == ORDERED,
          "a healthy chain with no row count is ORDERED")

    no_refresh = day(3) + day(4) + day(5, ref=None)
    rep = analyse(no_refresh, chain, rows=0)
    check(rep["verdict"] == NOT_RUN,
          "yesterday's refresh does not stand in for today's: NOT_RUN  "
          "<-- pinned defect")
    check(rep["findings"][0]["stage"] == "refresh",
          "and the finding names the stage that did not run")
    check(any("NOTE: the empty report" in line for line in render(rep)),
          "the report says an empty report proves nothing here")

    failed_ref = day(3) + day(4) + day(5, ref=None) + \
        [mk("refresh", 5, "02:00", "02:09", ok=False)]
    check(analyse(failed_ref, chain, rows=0)["verdict"] == FAILED,
          "a refresh that ran and failed is FAILED, not NOT_RUN")
    check(analyse(failed_ref, chain, rows=0)["findings"][0]["stage"]
          == "refresh", "and the finding names the stage that failed")
    open_fail = day(3) + day(4) + day(5, ref=None) + \
        [mk("refresh", 5, "02:00", None, ok=False)]
    check(analyse(open_fail, chain)["verdict"] == FAILED,
          "a failed run with no finished time still counts as a run")

    inverted = day(3) + day(4) + day(5, snap="03:00")
    rep = analyse(inverted, chain, rows=0)
    check(rep["verdict"] == INVERTED,
          "a snapshot taken after the refresh is INVERTED  <-- pinned defect")
    check("only ran at" in rep["findings"][0]["text"],
          "and the report says the snapshot ran late")

    twice = day(3) + day(4) + day(5) + [mk("snapshot", 5, "03:00", "03:02")]
    rep = analyse(twice, chain, rows=0)
    check(rep["verdict"] == INVERTED,
          "a second snapshot between refresh and compare is INVERTED  "
          "<-- pinned defect")
    check("ran again at" in rep["findings"][0]["text"],
          "and the report says the snapshot ran again")

    check(analyse([], chain, rows=0)["verdict"] == UNKNOWN,
          "an EMPTY history is UNKNOWN, never NOTHING_CHANGED  "
          "<-- pinned defect")
    check(analyse([], chain)["verdict"] == UNKNOWN,
          "an empty history is UNKNOWN with no row count too")
    check(analyse([], chain, rows=12)["verdict"] == UNKNOWN,
          "an empty history is UNKNOWN even when rows were reported")
    rep = analyse(day(5, cmp_=None), chain, rows=0)
    check(rep["verdict"] == UNKNOWN and rep["judged"] is None,
          "a history with no ok run of the last stage is UNKNOWN")
    failed_cmp = day(3, cmp_=None) + [mk("compare", 3, "06:00", "06:05",
                                         ok=False)]
    check(analyse(failed_cmp, chain, rows=0)["verdict"] == UNKNOWN,
          "a last stage that only failed is UNKNOWN: there is no report")

    short = day(5)
    rep = analyse(short, chain, rows=0)
    check(rep["verdict"] == NOTHING_CHANGED,
          "a one-day history whose stages all ran is judged")
    short_missing = day(5, ref=None)
    rep = analyse(short_missing, chain, rows=0)
    check(rep["verdict"] == UNKNOWN,
          "a missing stage in a history too short to cover the window is "
          "UNKNOWN, not NOT_RUN  <-- pinned defect")
    check("history starts at" in rep["findings"][0]["text"],
          "and the report says where the history starts")
    check(analyse(short_missing, chain, rows=0, window_hours=4)["verdict"]
          == NOT_RUN,
          "the same history with a window it does cover is NOT_RUN")

    # ---- the newest evidence beats the judged run
    cmp_fail = healthy + [mk("compare", 6, "06:00", "06:01", ok=False)]
    rep = analyse(cmp_fail, chain, rows=0)
    check(rep["verdict"] == FAILED and rep["findings"][0]["stage"] == "compare"
          and "failed at 2026-03-06 06:00:00" in rep["findings"][0]["text"],
          "a newer compare that failed is FAILED, not a quiet night from the "
          "older compare  <-- pinned defect")
    check(rep["judged"].started == parse_time(ts(5, "06:00"), "t")[0],
          "and the report still shows which ok compare it looked at")
    check(any("NOTE: the empty report" in line for line in render(rep)),
          "and says the empty report proves nothing")
    check(analyse(healthy + [mk("compare", 6, "06:00", None, ok=False)],
                  chain)["verdict"] == FAILED,
          "a newer compare that failed and has no finished time is FAILED")
    check(analyse(healthy + [mk("compare", 5, "06:00", "06:01", ok=False)],
                  chain)["verdict"] == ORDERED,
          "a failed compare that started with the judged one is not newer")
    stopped = day(3) + day(4) + day(5, cmp_=None) + day(6, cmp_=None)
    rep = analyse(stopped, chain, rows=0)
    check(rep["verdict"] == NOT_RUN and rep["findings"][0]["stage"] == "compare"
          and "ran as late as 2026-03-06 02:00:00" in rep["findings"][0]["text"],
          "a compare that stopped running while the chain went on is NOT_RUN, "
          "not the old compare's verdict  <-- pinned defect")
    waiting = day(3) + day(4) + day(5, cmp_=None)
    check(analyse(waiting, chain, rows=0)["verdict"] == NOTHING_CHANGED,
          "a new cycle that is still waiting for its compare is not flagged")
    edge = day(3) + day(4) + [mk("snapshot", 5, "06:00", "06:01")]
    check(analyse(edge, chain)["verdict"] == ORDERED,
          "a run exactly one cycle after the judged compare is not flagged")
    check(analyse(edge, chain, window_hours=23.5)["verdict"] == NOT_RUN,
          "a run just over one cycle after the judged compare is NOT_RUN")
    check(analyse(healthy + [mk("compare", 7, "06:00", "06:01")], chain,
                  rows=0)["judged"].started
          == parse_time(ts(7, "06:00"), "t")[0],
          "a newer ok compare is the one judged")
    fail_after = day(3) + day(4) + day(5, cmp_=None, ref=None) + \
        [mk("refresh", 5, "02:00", "02:09", ok=False)]
    rep = analyse(fail_after, chain, rows=0)
    check(rep["verdict"] == FAILED and rep["findings"][0]["stage"] == "refresh",
          "a refresh that failed after the judged compare is FAILED  "
          "<-- pinned defect")
    other = healthy + [mk("other", 6, "06:00", "06:01", ok=False),
                       mk("other", 9, "06:00", "06:01")]
    check(analyse(other, chain, rows=0)["verdict"] == NOTHING_CHANGED,
          "a task that is not in the chain is ignored, failed or late")
    check(not any("NOTE:" in line
                  for line in render(analyse(no_refresh, chain))),
          "no empty-report note when no row count was given")
    check(not any("NOTE:" in line
                  for line in render(analyse(no_refresh, chain, rows=4))),
          "no empty-report note when the report had rows")

    # ---- precedence and ordering of findings
    mixed = day(5, snap=None, ref=None)
    rep = analyse(mixed, chain, rows=0)
    check(rep["verdict"] == UNKNOWN and len(rep["findings"]) == 2,
          "two uncovered stages give two UNKNOWN findings")
    mixed_def = day(3, snap=None, ref=None) + day(5, snap=None, ref=None)
    rep = analyse(mixed_def, chain, rows=0)
    check(rep["verdict"] == NOT_RUN,
          "with the history covering the window both stages are NOT_RUN")
    after = day(3) + day(4) + day(5, snap=None) + \
        [mk("snapshot", 5, "08:00", "08:02")]
    rep = analyse(after, chain)
    check(rep["verdict"] == NOT_RUN and rep["findings"][0]["stage"]
          == "snapshot",
          "a snapshot that starts after the compare is not this cycle's: "
          "NOT_RUN, not INVERTED")

    # four stages, so one chain can hold two different problems at once
    c4 = ["a", "b", "c", "d"]
    old = [mk("other", 3, "01:00", "01:01")]
    late_a = [mk("a", 5, "03:00", "03:01"), mk("b", 5, "02:00", "02:10"),
              mk("d", 5, "06:00", "06:05")]
    rep = analyse(old + late_a, c4)
    check(sorted(f["code"] for f in rep["findings"]) == [INVERTED, NOT_RUN]
          and rep["verdict"] == NOT_RUN,
          "NOT_RUN outranks INVERTED in the headline, and a late stage is "
          "listed once, not twice  <-- pinned defect")
    rep = analyse(old + late_a + [mk("c", 5, "04:00", "04:01", ok=False)], c4)
    check(sorted(f["code"] for f in rep["findings"]) == [FAILED, INVERTED]
          and rep["verdict"] == FAILED,
          "FAILED outranks INVERTED in the headline")
    rep = analyse(old + [mk("a", 5, "01:00", "01:01"),
                         mk("b", 5, "02:00", "02:10", ok=False),
                         mk("d", 5, "06:00", "06:05")], c4)
    check(sorted(f["code"] for f in rep["findings"]) == [FAILED, NOT_RUN]
          and rep["verdict"] == NOT_RUN,
          "NOT_RUN outranks FAILED in the headline")

    # ---- boundaries and reruns
    touch = [mk("a", 4, "01:00", "01:10"), mk("b", 4, "01:10", "01:20")]
    check(analyse(touch, ["a", "b"])["verdict"] == ORDERED,
          "a stage that finishes the second the next one starts is in order")
    rerun = day(3) + day(4) + day(5, snap="01:00") + \
        [mk("snapshot", 5, "03:00", "03:02"), mk("refresh", 5, "04:00",
                                                 "04:20")]
    refail = day(3) + day(4) + day(5) + \
        [mk("refresh", 5, "04:00", "04:09", ok=False)]
    check(analyse(refail, chain)["verdict"] == ORDERED,
          "known limit: a failed refresh rerun after an ok one is not seen "
          "(see Limits)")
    rep = analyse(rerun, chain)
    check(rep["verdict"] == ORDERED
          and rep["chosen"][1].started == parse_time(ts(5, "04:00"), "t")[0],
          "a refresh rerun after the snapshot is the one chosen, and in "
          "order")

    # ---- the edges of the window and of the order
    cs = ["a", "b"]
    b_end = mk("b", 5, "06:00", "06:05")
    check(analyse([mk("other", 4, "06:00", "06:01"), b_end], cs)["verdict"]
          == NOT_RUN,
          "a history that starts exactly where the window opens is long "
          "enough: NOT_RUN, not UNKNOWN")
    check(analyse([mk("a", 4, "05:50", "06:00"), b_end], cs)["verdict"]
          == ORDERED,
          "a run that finishes exactly where the window opens counts")
    rep = analyse([mk("other", 3, "01:00", "01:01"),
                   mk("a", 5, "06:00", "06:01"), b_end], cs)
    check(rep["verdict"] == INVERTED,
          "a stage that starts the second the last stage starts was not "
          "before it: INVERTED")
    long_run = mk("a", 4, "07:00", "02:00", end_day=5)
    check(analyse([long_run, mk("b", 5, "08:00", "08:05")], cs)["verdict"]
          == ORDERED,
          "a run that began before the window opened and finished inside it "
          "counts")
    rep = analyse([mk("a", 5, "01:00", "03:00"), mk("a", 5, "02:00", "02:30"),
                   b_end], cs)
    check(rep["chosen"][0].started == parse_time(ts(5, "01:00"), "t")[0],
          "of two ok runs, the one that finished last is chosen")
    long_snap = [mk("snapshot", 5, "01:00", "02:10"),
                 mk("refresh", 5, "02:00", "02:20"),
                 mk("compare", 5, "06:00", "06:05")]
    rep = analyse(long_snap, chain)
    check(rep["verdict"] == INVERTED
          and rep["findings"][0]["stage"] == "snapshot",
          "a snapshot still running when the refresh started is INVERTED "
          "in a three-stage chain")
    at_finish = day(5) + [mk("snapshot", 5, "02:20", "02:21")]
    check(analyse(at_finish, chain)["verdict"] == INVERTED,
          "a snapshot rerun that starts the second the refresh finished is "
          "INVERTED")
    at_start = day(5) + [mk("snapshot", 5, "06:00", "06:01")]
    check(analyse(at_start, chain)["verdict"] == INVERTED,
          "a snapshot rerun that starts the second the compare starts is "
          "INVERTED")
    instant = day(5, cmp_=None) + [mk("compare", 5, "06:00", "06:00"),
                                   mk("snapshot", 5, "06:00", "06:01")]
    check(len(analyse(instant, chain)["findings"]) == 1,
          "a late snapshot is reported once, not again against the last "
          "stage")
    unk = day(5, snap=None, ref=None) + [mk("refresh", 5, "02:00", "02:09",
                                            ok=False)]
    rep = analyse(unk, chain, rows=0)
    check(rep["verdict"] == FAILED
          and sorted(f["code"] for f in rep["findings"]) == [FAILED, UNKNOWN],
          "a definite problem beats UNKNOWN in the headline, and both are "
          "listed")
    late_fail = analyse(healthy + [mk("compare", 7, "07:00", "07:01",
                                      ok=False)], chain, rows=0)
    check([f["code"] for f in late_fail["findings"]] == [FAILED],
          "a failed compare more than a cycle later is FAILED alone, not "
          "also NOT_RUN")
    two_fail = analyse(healthy + [mk("compare", 6, "06:00", "06:01", ok=False),
                                  mk("compare", 8, "06:00", "06:01",
                                     ok=False)], chain, rows=0)
    check("failed at 2026-03-08 06:00:00" in two_fail["findings"][0]["text"],
          "the finding names the latest failed run")
    # known limits, pinned so a change is seen (see Limits in the README)
    check(analyse(day(3) + day(4) + day(5)
                  + [mk("snapshot", 5, "03:00", "03:02", ok=False)],
                  chain)["verdict"] == ORDERED,
          "known limit: a failed snapshot rerun between refresh and compare "
          "is not seen")
    check(analyse(day(3) + day(4) + day(5)
                  + [mk("snapshot", 5, "02:05", "02:07")],
                  chain)["verdict"] == ORDERED,
          "known limit: a snapshot rerun that starts inside the refresh and "
          "ends before the compare is not seen")
    check(analyse(day(3) + day(4) + day(5)
                  + [mk("refresh", 5, "05:55", "06:10")],
                  chain)["verdict"] == ORDERED,
          "known limit: a refresh rerun that overlaps the start of the "
          "compare is not seen")

    # ---- window and the cycle
    wide = day(3) + day(5, snap=None, ref=None)
    check(analyse(wide, chain, window_hours=72)["verdict"] == ORDERED,
          "a 72 hour cycle accepts a refresh from two days earlier")
    check(analyse(wide, chain, window_hours=24)["verdict"] == NOT_RUN,
          "a 24 hour cycle does not")
    check(analyse(wide, chain, window_hours=0.5)["verdict"] == NOT_RUN,
          "a half hour cycle works and finds the stages missing")
    check(_hours(0.5) == "0.50" and _hours(24.0) == "24",
          "hours print whole when whole")

    two = [mk("a", 4, "01:00", "01:10"), mk("b", 4, "02:00", "02:10")]
    check(analyse(two, ["a", "b"], rows=3)["verdict"] == CHANGED,
          "a two-stage chain works")
    check(analyse(two, ["A", "B"], rows=3)["verdict"] == CHANGED,
          "stage names match without regard to case")
    three = two + [mk("c", 4, "02:30", "02:40")]
    overlap = [mk("a", 4, "01:00", "01:10"), mk("b", 4, "01:05", "01:20")]
    check(analyse(overlap, ["a", "b"])["verdict"] == INVERTED,
          "a stage that overlaps the next one is INVERTED, not chosen")
    check(analyse(three, ["a", "b", "c"])["verdict"] == ORDERED,
          "three stages in order are ORDERED")

    # ---- the clock: a compare that stopped, seen on the first morning
    def when(d, hhmm, sec="00"):
        return parse_time("2026-03-%02d %s:%s" % (d, hhmm, sec), "t")[0]

    day1 = healthy + day(6, cmp_=None)
    rep = analyse(day1, chain, rows=0, now=when(6, "08:00"))
    check(rep["verdict"] == NOT_RUN and rep["findings"][0]["stage"] == "compare"
          and "now is 2026-03-06 08:00:00" in rep["findings"][0]["text"],
          "the morning after the compare stopped, with the clock given, is "
          "NOT_RUN, not a quiet night  <-- pinned defect")
    check(analyse(day1, chain, rows=0)["verdict"] == NOTHING_CHANGED,
          "the core calls no clock: with no now it cannot tell, and says so "
          "only through the CLI")
    check(analyse(day1, chain, rows=0, now=when(6, "05:00"))["verdict"]
          == NOTHING_CHANGED,
          "a compare that is not yet due at the clock time is not flagged")
    check(analyse(day1, chain, rows=0, now=when(6, "06:05"))["verdict"]
          == NOTHING_CHANGED,
          "a compare due exactly now is not yet late")
    check(analyse(day1, chain, rows=0, now=when(6, "06:05", "01"))["verdict"]
          == NOT_RUN,
          "a compare one second later than the judged finish plus one cycle "
          "is late")
    check(analyse(day1, chain, rows=0, now=when(6, "07:00"),
                  window_hours=48)["verdict"] == NOTHING_CHANGED,
          "a longer cycle moves the due time with it")
    rep = analyse(healthy, chain, rows=0,
                  now=datetime.datetime(2026, 10, 24, 9, 0, 0))
    check(rep["verdict"] == NOT_RUN,
          "a whole chain that stopped months ago is NOT_RUN, not a clean "
          "verdict  <-- pinned defect")
    rep = analyse(stopped, chain, rows=0,
                  now=datetime.datetime(2026, 10, 24, 9, 0, 0))
    check([f["code"] for f in rep["findings"]] == [NOT_RUN],
          "a stopped compare that the chain ran past is listed once, not "
          "twice")
    rep = analyse(cmp_fail, chain, rows=0,
                  now=datetime.datetime(2026, 10, 24, 9, 0, 0))
    check([f["code"] for f in rep["findings"]] == [FAILED],
          "a newer failed compare is FAILED alone: the compare did run")
    rep = analyse(healthy, chain, rows=0, now=when(4, "00:00"))
    check(rep["verdict"] == UNKNOWN and rep["findings"][0]["stage"] is None
          and "before the newest run" in rep["findings"][0]["text"],
          "a clock earlier than the newest run is UNKNOWN: the clock or the "
          "zone is wrong")
    check(analyse(healthy, chain, rows=0, now=when(5, "06:05"))["verdict"]
          == NOTHING_CHANGED,
          "a clock equal to the newest finish is accepted")
    running = healthy + [mk("snapshot", 6, "05:00", "09:00")]
    rep = analyse(running, chain, rows=0, now=when(6, "07:00"))
    check([f["code"] for f in rep["findings"]] == [UNKNOWN],
          "a clock inside the newest run is UNKNOWN alone: the clock cannot "
          "also date a late compare")
    check(analyse([], chain, rows=0, now=when(5, "06:05"))["verdict"]
          == UNKNOWN, "an empty history stays UNKNOWN with a clock")

    # ---- the judged run is the newest start, not the newest finish
    rep = analyse(day(4) + day(5, cmp_=None)
                  + [mk("compare", 5, "06:00", "07:00"),
                     mk("compare", 5, "06:30", "06:35")], chain, rows=0)
    check(rep["judged"].started == when(5, "06:30"),
          "of two ok compares, the one that started last is judged, not the "
          "one that finished last  <-- pinned defect")

    # ---- an ok run from the previous cycle does not hide this cycle's failure
    prev_cycle = [mk("snapshot", 4, "06:30", "06:32"),
                  mk("refresh", 4, "07:00", "07:20"),
                  mk("compare", 4, "08:00", "08:05"),
                  mk("compare", 5, "06:00", "06:05")]
    rep = analyse(day(3) + prev_cycle
                  + [mk("refresh", 5, "01:00", "01:09", ok=False),
                     mk("refresh", 5, "02:00", "02:09", ok=False)],
                  chain, rows=0)
    check(rep["verdict"] == FAILED and [f["stage"] for f in rep["findings"]]
          == ["refresh"] and "only ok run" in rep["findings"][0]["text"],
          "today's failed refresh is not hidden by yesterday's late ok "
          "refresh  <-- pinned defect")
    check("failed at 2026-03-05 02:00:00" in rep["findings"][0]["text"]
          and 1 not in rep["chosen"],
          "the finding names the latest failure, and the stale ok refresh is "
          "not chosen")
    check(analyse(day(3) + prev_cycle, chain, rows=0)["verdict"]
          == NOTHING_CHANGED,
          "without the failure, that late refresh is still accepted by the "
          "cycle length")
    check(analyse(prev_cycle + [mk("refresh", 4, "08:00", "08:09", ok=False)],
                  chain, rows=0)["verdict"] == NOTHING_CHANGED,
          "a failure that started the second the previous compare started "
          "is that cycle's, not this one's")
    check(analyse(prev_cycle + [mk("refresh", 5, "05:59", "06:30")],
                  chain, rows=0)["verdict"] == NOTHING_CHANGED,
          "known limit: an ok refresh that overlaps the compare is not seen, "
          "and is never counted as a failure")
    check(analyse(day(5) + [mk("refresh", 5, "04:00", "04:09", ok=False)],
                  chain)["verdict"] == ORDERED,
          "a failed rerun with no earlier compare to date the cycle is not "
          "seen (see Limits)")

    # ---- years below 1000 print the same on every host
    check(stamp(datetime.datetime(500, 3, 5, 1, 2, 3)) == "0500-03-05 01:02:03",
          "a year below 1000 is zero padded on every host  <-- pinned defect")
    old = [mk("a", 5, "01:00", "01:10"), mk("b", 5, "02:00", "02:10")]
    old = [r._replace(started=r.started.replace(year=500),
                      finished=r.finished.replace(year=500)) for r in old]
    check("judged: b, started 0500-03-05 02:00:00, rows reported: not given"
          in render(analyse(old, ["a", "b"])),
          "a report for such a year says 0500, not 500")

    # ---- the clock the CLI reads
    local_gap = system_now(False) - system_now(True)
    check(abs(local_gap - datetime.datetime.now().astimezone().utcoffset())
          < datetime.timedelta(seconds=5),
          "system_now gives local time for a plain history and UTC for an "
          "offset one")

    # ---- reading input
    check(parse_time("2026-03-05 06:00:00", "t") ==
          (datetime.datetime(2026, 3, 5, 6, 0, 0), False),
          "a plain time is naive")
    check(parse_time("2026-03-05T06:00:00Z", "t") ==
          (datetime.datetime(2026, 3, 5, 6, 0, 0), True),
          "a T and a Z are accepted")
    check(parse_time("2026-03-05 06:00:00-05:00", "t")[0] ==
          datetime.datetime(2026, 3, 5, 11, 0, 0),
          "a negative offset is folded into UTC")
    check(parse_time("2026-03-05 06:00:00+01:30", "t")[0] ==
          datetime.datetime(2026, 3, 5, 4, 30, 0),
          "a positive offset is folded into UTC")
    check(parse_time("2026-03-05 06:00:00+01:00", "t")[1] is True
          and parse_time("2026-03-05 06:00:00-01:00", "t")[1] is True,
          "a numeric offset marks the time as aware  <-- pinned defect")
    check(parse_time("2026-03-05 06:00:00+23:59", "t")[0] ==
          datetime.datetime(2026, 3, 4, 6, 1, 0),
          "the largest offset, +23:59, is accepted")
    for bad, why in (("2026-03-05 06:00", "no seconds"),
                     ("2026-03-05 6:00:00", "one digit hour"),
                     ("2026-03-05 06:00:00 PM", "12-hour text"),
                     ("2026-13-05 06:00:00", "month 13"),
                     ("2026-03-05 06:00:60", "second 60"),
                     ("2026-03-05 06:00:00+24:00", "offset hour 24"),
                     ("2026-03-05 06:00:00+01:60", "offset minute 60"),
                     ("2026-03-05 06:00:00+0100", "offset without colon"),
                     ("2026-03-05 06:00:00\n06", "a second line"),
                     ("2026-03-05 \u0666\u0666:00:00", "non-ASCII digits")):
        raises(lambda b=bad: parse_time(b, "t"),
               "a time with %s is refused  <-- pinned defect" % why)

    def pairs(**kw):
        row = {"task": "compare", "started": ts(5, "06:00"),
               "finished": ts(5, "06:05"), "result": "ok"}
        row.update(kw)
        return [list(row.items())]

    check(len(runs_from_pairs(pairs())) == 1, "a good row becomes a run")
    check(runs_from_pairs([[("Task", "x"), (" STARTED", ts(5, "01:00")),
                            ("Finished", ts(5, "01:01")),
                            ("Result", " OK ")]])[0].ok is True,
          "column names and result are read without regard to case or "
          "spaces")
    check(runs_from_pairs(pairs(finished=ts(5, "06:00")))[0].finished
          == runs_from_pairs(pairs())[0].started,
          "a run that finishes the second it starts is accepted")
    r = runs_from_pairs(pairs(finished="", result="fail"))[0]
    check(r.finished is None and r.ok is False,
          "a fail row may have no finished time")
    check(runs_from_pairs(pairs(finished=None, result="fail"))[0].ok is False,
          "a missing JSON value is a blank")
    raises(lambda: runs_from_pairs(pairs(finished="")),
           "an ok row with no finished time is refused  <-- pinned defect")
    raises(lambda: runs_from_pairs(pairs(result="success")),
           "a result other than ok or fail is refused, never guessed")
    raises(lambda: runs_from_pairs(pairs(result="")),
           "a blank result is refused")
    raises(lambda: runs_from_pairs(pairs(task="  ")),
           "a blank task is refused")
    raises(lambda: runs_from_pairs(pairs(started="")),
           "a blank started time is refused")
    raises(lambda: runs_from_pairs(pairs(finished=ts(5, "05:00"))),
           "a run that finishes before it starts is refused")
    raises(lambda: runs_from_pairs(pairs(started=7)),
           "a number where text belongs is refused")
    full = [("started", ts(5, "01:00")), ("finished", ts(5, "01:01")),
            ("result", "ok")]
    raises(lambda: runs_from_pairs([[("task", "x"), ("TASK", "y")] + full]),
           "a column that appears twice is refused  <-- pinned defect",
           text="appears twice")
    raises(lambda: runs_from_pairs([[("task", "x")] + full
                                    + [("Result", "fail")]]),
           "a repeated result column is refused, not read as the last one  "
           "<-- pinned defect", text="appears twice")
    raises(lambda: runs_from_pairs([[("task", "x")]]),
           "a row missing a column is refused")
    raises(lambda: runs_from_pairs(
        pairs() + [[("task", "compare"), ("started", "2026-03-05 06:00:00Z"),
                    ("finished", "2026-03-05 06:05:00Z"),
                    ("result", "ok")]]),
           "a history mixing offset and plain times is refused  "
           "<-- pinned defect")
    raises(lambda: runs_from_pairs(pairs(started=ts(5, "06:00") + "Z")),
           "one row mixing offset and plain times is refused")
    raises(lambda: runs_from_pairs(
        pairs() + [[("task", "compare"),
                    ("started", "2026-03-05 06:00:00+01:00"),
                    ("finished", "2026-03-05 06:05:00+01:00"),
                    ("result", "ok")]]),
           "a history mixing a numeric offset and plain times is refused  "
           "<-- pinned defect", text="mixes times")

    check(parse_chain("a, b ,c") == ["a", "b", "c"],
          "a chain is split on commas and trimmed")
    raises(lambda: parse_chain("only"), "a one-stage chain is refused")
    raises(lambda: parse_chain("a,,b"), "a blank stage name is refused")
    raises(lambda: parse_chain("a,A"), "a repeated stage is refused")

    # ---- the files
    with tempfile.TemporaryDirectory() as tmp:
        def put(name, data, mode="w"):
            path = os.path.join(tmp, name)
            if mode == "w":
                with open(path, "w", encoding="utf-8", newline="") as h:
                    h.write(data)
            else:
                with open(path, "wb") as h:
                    h.write(data)
            return path

        head = "task,started,finished,result\n"
        body = "".join("%s,%s,%s,ok\n" % (r.task,
                                          stamp(r.started),
                                          stamp(r.finished))
                       for r in healthy)
        csv_path = put("runs.csv", head + body)
        check(len(load_runs(csv_path)) == len(healthy),
              "a CSV history is read")
        extra = load_runs(put("extra.csv", "Host,Result,Task,Started,Finished\n"
                              "box1,OK,a,2026-03-05 01:00:00,"
                              "2026-03-05 01:01:00\n"))
        check(len(extra) == 1 and extra[0].task == "a" and extra[0].ok,
              "columns in any order and any case are read, and extra columns "
              "are ignored")
        check(len(load_runs(put("bom.csv", "\ufeff" + head + "\n" +
                                body + "\n\n"))) == len(healthy),
              "a byte order mark and blank lines are tolerated")
        js = json.dumps([{"Task": r.task, "Started": stamp(r.started),
                          "Finished": stamp(r.finished),
                          "Result": "ok"} for r in healthy])
        json_path = put("runs.json", js)
        check(len(load_runs(json_path)) == len(healthy),
              "a JSON history is read, with keys in any case")
        check(load_runs(put("blank.csv", "  \n\n")) == [],
              "a blank file is an empty history, not an error")
        check(load_runs(put("hdr.csv", head)) == [],
              "a header-only file is an empty history")
        check(load_runs(put("none.json", "[]")) == [],
              "an empty JSON array is an empty history")
        raises(lambda: load_runs(os.path.join(tmp, "missing.csv")),
               "a missing file is refused")
        raises(lambda: load_runs(tmp),
               "a directory in place of a file is refused")
        raises(lambda: load_runs(put("bad.bin", b"\xff\xfe\x00bad", "b")),
               "a file that is not UTF-8 is refused")
        raises(lambda: load_runs(put("bad.json", "[{")),
               "broken JSON is refused")
        raises(lambda: load_runs(put("obj.json", "{}")),
               "a JSON object in place of an array is refused")
        raises(lambda: load_runs(put("arr.json", "[1]")),
               "a JSON array of non-objects is refused")
        raises(lambda: load_runs(put("long.csv", head + "a,%s,%s,ok,extra\n"
                                     % (ts(5, "01:00"), ts(5, "01:01")))),
               "a CSV row with more cells than the header is refused  "
               "<-- pinned defect", text="more cells")
        raises(lambda: load_runs(put("short.csv", head + "a,b\n")),
               "a CSV row with too few cells is refused")
        raises(lambda: load_runs(put("dup.csv", "task,Task,started,finished,"
                                     "result\na,b,%s,%s,ok\n"
                                     % (ts(5, "01:00"), ts(5, "01:01")))),
               "a CSV header with a repeated column is refused",
               text="appears twice")
        raises(lambda: load_runs(put("big.csv", head + "a,%s,%s,ok\n"
                                     % ("9" * 200000, ts(5, "01:01")))),
               "a CSV field over the csv size limit is refused, not a "
               "traceback", text="not readable CSV")
        raises(lambda: load_runs(put("deep.json", "[" * 100000)),
               "a JSON history nested 100000 deep is refused, not a "
               "traceback", text="nested too deeply")

        # ---- the CLI on real files
        base = ["--history", csv_path, "--chain", "snapshot,refresh,compare"]
        code, out, err = cli(base + ["--rows", "0"])
        check(code == 0 and "VERDICT: NOTHING_CHANGED" in out
              and out.rstrip().splitlines()[-1].startswith("VERDICT: "),
              "the CLI prints the verdict last and exits 0")
        check("rows reported: 0" in out and "UTC" not in out,
              "the CLI shows the row count and no UTC note for plain times")
        code, out, _ = cli(base + ["--rows", "5"])
        check(code == 0 and "VERDICT: CHANGED" in out,
              "the CLI reports CHANGED with rows")
        code, out, _ = cli(base)
        check(code == 0 and "rows reported: not given" in out
              and "VERDICT: ORDERED" in out,
              "the CLI works without --rows")

        broken = put("broken.csv", head + "".join(
            "%s,%s,%s,ok\n" % (r.task, stamp(r.started),
                               stamp(r.finished))
            for r in no_refresh))
        code, out, _ = cli(["--history", broken, "--chain",
                            "snapshot,refresh,compare", "--rows", "0"])
        check(code == 1 and "VERDICT: NOT_RUN" in out
              and "does not show that nothing changed" in out,
              "the CLI exits 1 for NOT_RUN and says the empty report proves "
              "nothing")
        empty = put("empty.csv", head)
        code, out, _ = cli(["--history", empty, "--chain",
                            "snapshot,refresh,compare", "--rows", "0"])
        check(code == 3 and "VERDICT: UNKNOWN" in out
              and "the history holds no runs" in out,
              "the CLI exits 3 for an empty history  <-- pinned defect")
        utc_path = put("utc.csv", head + "snapshot,2026-03-05T01:00:00Z,"
                       "2026-03-05T01:01:00Z,ok\nrefresh,2026-03-05T02:00:00Z,"
                       "2026-03-05T02:10:00Z,ok\ncompare,"
                       "2026-03-05T06:00:00Z,2026-03-05T06:05:00Z,ok\n")
        code, out, _ = cli(["--history", utc_path, "--chain",
                            "snapshot,refresh,compare", "--rows", "1"])
        check(code == 0 and "06:00:00 UTC" in out,
              "an offset history says its times are UTC")
        code, out, _ = cli(["--history", json_path, "--chain",
                            "snapshot,refresh,compare"])
        check(code == 0, "the CLI reads a JSON history")

        # a hostile task name cannot forge or hide a line
        evil = "x\nVERDICT: CHANGED\x1b[2K"
        hostile = put("hostile.csv", head + '"%s",%s,%s,ok\n'
                      % (evil, ts(5, "01:00"), ts(5, "01:01")) +
                      "compare,%s,%s,ok\n" % (ts(5, "06:00"), ts(5, "06:05")))
        code, out, _ = cli(["--history", hostile, "--chain",
                            evil + ",compare", "--rows", "0"])
        lines = out.splitlines()
        check(code == 0 and lines[-1] == "VERDICT: NOTHING_CHANGED"
              and sum(1 for l in lines if l.startswith("VERDICT:")) == 1
              and "\x1b" not in out,
              "a task name with a newline and an escape cannot forge a "
              "VERDICT line  <-- pinned defect")

        # ---- --report and --apply: the only write
        rpt = os.path.join(tmp, "order.txt")
        code, out, _ = cli(base + ["--rows", "0", "--report", rpt])
        check(code == 0 and not os.path.exists(rpt)
              and "Check only" in out,
              "--report without --apply writes nothing  <-- pinned defect")
        code, out, err = cli(base + ["--rows", "0", "--report", rpt, "--ap"])
        check(code == 2 and not os.path.exists(rpt) and "--ap" in err,
              "a unique prefix --ap of --apply is REFUSED by the parser  "
              "<-- pinned defect")
        for prefix in ("--rep", "--his", "--cha", "--ro", "--window",
                       "--self", "--no"):
            code, _, err = cli(base + [prefix, "x"])
            check(code == 2 and "unrecognized arguments" in err,
                  "the prefix %s is refused, not read as its full flag"
                  % prefix)
        listed = set(os.listdir(tmp))
        code, out, _ = cli(base + ["--rows", "0", "--report", rpt,
                                   "--apply"])
        with open(rpt, "rb") as h:
            written = h.read().decode("ascii")
        check(code == 0 and written.endswith("VERDICT: NOTHING_CHANGED\n")
              and written.count("\n") == len(out.splitlines())
              and "\r" not in written,
              "--report with --apply writes the report, with plain newlines "
              "on every host")
        check(set(os.listdir(tmp)) - listed == set(["order.txt"]),
              "the report file is the only file --apply adds")
        with open(csv_path, "rb") as h:
            before = h.read()
        code, _, err = cli(base + ["--report", csv_path, "--apply"])
        with open(csv_path, "rb") as h:
            after = h.read()
        check(code == 2 and "refusing to overwrite" in err
              and after == before,
              "--report naming the history file is refused and the history "
              "is intact  <-- pinned defect")
        # A symlink needs a privilege on Windows, so the alias is a stub of
        # os.path.realpath. The real symlink run is in the README.
        alias = os.path.join(tmp, "alias.csv")
        real_path = os.path.realpath
        os.path.realpath = lambda p: real_path(
            csv_path if os.path.basename(p) == "alias.csv" else p)
        try:
            code, _, err = cli(base + ["--report", alias, "--apply"])
        finally:
            os.path.realpath = real_path
        with open(csv_path, "rb") as h:
            after = h.read()
        check(code == 2 and "refusing to overwrite" in err
              and after == before and not os.path.exists(alias),
              "--report that resolves to the history file is refused, "
              "whatever its spelling  <-- pinned defect")
        code, _, err = cli(base + ["--report", tmp, "--apply"])
        check(code == 2 and "cannot write" in err,
              "a report path that cannot be written exits 2")
        code, _, err = cli(base + ["--apply"])
        check(code == 2 and "--apply needs --report" in err,
              "--apply without --report exits 2")

        # ---- exit codes are a contract: a scheduler reads them
        def dump(name, runs):
            lines = [head.rstrip("\n")]
            for r in runs:
                lines.append("%s,%s,%s,%s" % (
                    r.task, stamp(r.started),
                    stamp(r.finished) if r.finished else "",
                    "ok" if r.ok else "fail"))
            return put(name, "\n".join(lines) + "\n")

        later = datetime.datetime(2026, 3, 7, 0, 0, 0)
        for name, runs, verdict, at in (
                ("FAILED", failed_ref, "FAILED", noon),
                ("newer-fail", cmp_fail, "FAILED", later),
                ("INVERTED", inverted, "INVERTED", noon),
                ("stopped", stopped, "NOT_RUN", later)):
            code, out, _ = cli(["--history", dump(name + ".csv", runs),
                                "--chain", "snapshot,refresh,compare",
                                "--rows", "0"], at)
            check(code == 1 and "VERDICT: %s" % verdict in out,
                  "the CLI exits 1 for %s (%s)" % (verdict, name))
        for bad in ("0", "-1", "nan", "inf"):
            code, _, err = cli(base + ["--window-hours", bad])
            check(code == 2 and "--window-hours must be" in err,
                  "--window-hours %s is refused by the flag check" % bad)
        for big in ("1e9", "1e300"):
            code, out, err = cli(base + ["--window-hours", big])
            check(code == 2 and "outside the range" in err and not out,
                  "--window-hours %s exits 2, not a traceback  "
                  "<-- pinned defect" % big)
        year1 = put("year1.csv", head + "a,0001-01-01 00:00:00+05:00,"
                    "0001-01-01 00:01:00+05:00,ok\n")
        code, _, err = cli(["--history", year1, "--chain", "a,b"])
        check(code == 2 and "outside the range" in err,
              "a time near year 1 with an offset exits 2, not a traceback  "
              "<-- pinned defect")

        # ---- --now
        code, out, _ = cli(base + ["--rows", "0", "--now",
                                   "2026-03-05 07:00:00"])
        check(code == 0 and "VERDICT: NOTHING_CHANGED" in out,
              "--now just after the compare gives a clean verdict")
        code, out, _ = cli(base + ["--rows", "0", "--now",
                                   "2026-03-06 08:00:00"])
        check(code == 1 and "VERDICT: NOT_RUN" in out
              and "now is 2026-03-06 08:00:00" in out
              and "does not show that nothing changed" in out,
              "--now a day after the compare gives NOT_RUN, exit 1  "
              "<-- pinned defect")
        code, out, _ = cli(base + ["--rows", "0"], datetime.datetime(
            2026, 3, 6, 8, 0, 0))
        check(code == 1 and "VERDICT: NOT_RUN" in out,
              "with no --now the clock is read, so the same day gives "
              "NOT_RUN  <-- pinned defect")
        code, out, _ = cli(base + ["--rows", "0"], datetime.datetime(
            2026, 3, 4, 0, 0, 0))
        check(code == 3 and "VERDICT: UNKNOWN" in out
              and "before the newest run" in out,
              "a clock before the newest run exits 3")
        code, out, _ = cli(base + ["--rows", "0"], None)
        check(code == 1 and "VERDICT: NOT_RUN" in out,
              "with the real clock, a 2026-03 history is long stale")
        code, _, err = cli(base + ["--now", "2026-03-06T08:00:00Z"])
        check(code == 2 and "both use a Z" in err,
              "--now with an offset against a plain history is refused")
        code, _, err = cli(["--history", utc_path, "--chain",
                            "snapshot,refresh,compare", "--now",
                            "2026-03-06 08:00:00"])
        check(code == 2 and "both use a Z" in err,
              "--now without an offset against an offset history is refused")
        code, out, _ = cli(["--history", utc_path, "--chain",
                            "snapshot,refresh,compare", "--now",
                            "2026-03-05T07:00:00+00:00"])
        check(code == 0 and "06:00:00 UTC" in out,
              "an offset history with --now in the same form is judged, and "
              "says UTC  <-- pinned defect")
        code, _, err = cli(base + ["--now", "yesterday"])
        check(code == 2 and "--now:" in err,
              "a --now that is not a time is refused")
        code, out, _ = cli(["--history", empty, "--chain",
                            "snapshot,refresh,compare", "--now",
                            "2026-03-06T08:00:00Z"])
        check(code == 3 and "the history holds no runs" in out,
              "--now with an empty history is UNKNOWN, whatever its form")

    # ---- usage errors
    code, _, err = cli(["--chain", "a,b"])
    check(code == 2 and "--history is required" in err,
          "no --history exits 2")
    code, _, err = cli(["--history", "x"])
    check(code == 2 and "--chain is required" in err, "no --chain exits 2")
    code, _, err = cli(["--history", "x", "--chain", "a,b", "--rows", "-1"])
    check(code == 2 and "negative" in err, "--rows -1 is refused")
    code, _, err = cli(["--history", "x", "--chain", "a,b", "--rows", "z"])
    check(code == 2, "--rows z is refused by the parser")
    code, _, err = cli(["--history", "x", "--chain", "a"])
    check(code == 2 and "at least two" in err, "a one-stage chain exits 2")
    code, _, err = cli(["--history", "nope.csv", "--chain", "a,b"])
    check(code == 2 and "cannot read" in err, "a missing history exits 2")
    for extra in (["--history", "x"], ["--chain", "a,b"], ["--rows", "1"],
                  ["--window-hours", "5"], ["--report", "r"], ["--apply"],
                  ["--now", "2026-03-05 07:00:00"]):
        code, _, err = cli(["--self-test"] + extra)
        check(code == 2 and "takes no other flag" in err,
              "--self-test refuses %s" % extra[0])
    check(build_parser().allow_abbrev is False,
          "the parser is built with allow_abbrev=False")

    # ---- the checks above must be able to fail
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        p2, f2, check2, raises2 = harness()
        check2(True, "good")
        check2(False, "bad")
        raises2(lambda: None, "no raise")
        raises2(lambda: 1 / 0, "wrong raise")
        raises2(lambda: parse_chain("x"), "right raise")
        raises2(lambda: parse_chain("x"), "right text", text="at least two")
        raises2(lambda: parse_chain("x"), "wrong text", text="zzz")
    finally:
        sys.stdout = real
    check(p2[0] == 3 and f2 == ["bad", "no raise (no error raised)",
                                "wrong raise (wrong exception "
                                "ZeroDivisionError('division by zero'))",
                                "wrong text (message '--chain needs at least "
                                "two stages, separated by commas')"],
          "the harness counts a failed check, a missing raise, a wrong "
          "exception and a message without the expected text")
    check(analyse(healthy, chain, rows=0)["verdict"]
          != analyse([], chain, rows=0)["verdict"],
          "a healthy history and an empty one do not give the same verdict")
    check(EXIT[UNKNOWN] not in (EXIT[NOTHING_CHANGED], EXIT[NOT_RUN]),
          "UNKNOWN has its own exit code")
    check(set(MEANING) == set(EXIT),
          "every verdict has an exit code and a meaning")
    check(safe("a\nb\u00e9") == "a?b?", "safe() keeps printable ASCII only")
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        ok_code, bad_code = finish(0, []), finish(1, ["x"])
        footer = sys.stdout.getvalue()
    finally:
        sys.stdout = real
    check(ok_code == 0 and bad_code == 1 and "0 assertions, 0 failed" in footer
          and "FAILED: x" in footer,
          "the footer returns 0 for none failed and 1 for any, and names "
          "each failure")
    # ---- importing the file runs nothing and writes no bytecode
    here = os.path.abspath(__file__)
    pyc = importlib.util.cache_from_source(here)
    before = os.path.exists(pyc) and os.path.getmtime(pyc)
    spec = importlib.util.spec_from_file_location("taskorder_probe", here)
    probe = importlib.util.module_from_spec(spec)
    no_cache = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.dont_write_bytecode = no_cache
    check(callable(probe.analyse),
          "importing the file runs nothing and exposes the core")
    check((os.path.exists(pyc) and os.path.getmtime(pyc)) == before,
          "the import probe writes no bytecode beside the script")
    print("-" * 68)
    return finish(passed[0] + len(failed), failed)


if __name__ == "__main__":
    sys.exit(main())
