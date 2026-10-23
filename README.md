# taskorder

Tell "the refresh never ran" from "nothing changed", from a run history and the order your tasks should run in.

A nightly chain has three scheduled jobs. The first copies a feature class to a "yesterday" copy.
The second refreshes the live feature class from a vendor feed. The third compares the two and
writes a change report. On most mornings the report lists some changes. One morning it lists none.

An empty report has four causes, and they look the same. Nothing changed. Or the refresh never
ran, so today equals yesterday. Or the refresh ran and failed. Or the snapshot job ran after the
refresh, so it copied the new data over the old and the compare saw two identical copies. In all
four cases the compare job exits 0 and writes a clean file with zero rows. The person who reads
it concludes "a quiet night". Only one of the four is a quiet night.

A fifth cause looks the same to a person who opens the folder. The compare job itself failed or
stopped, and the empty file is an older one. A tool that judges "the last good compare" would
bless it.

This case comes from reading a pipeline of this shape, not from an incident that I watched fail.
The tool shows what it would catch. Every example in this README is synthetic.

The tool reads a run history (one row per task run) and the order you declare for the chain. It
judges the newest `ok` run of the last stage and gives one verdict. It does not trust that run if
anything newer contradicts it. If the history is empty, the verdict is `UNKNOWN`. It is never
"nothing changed".

```
$ python taskorder.py --self-test
taskorder self-test: offline, no Task Scheduler, no network, no credentials
--------------------------------------------------------------------
PASS  a healthy chain with an empty report is NOTHING_CHANGED
PASS  the latest ok run of the last stage is the one judged
...
PASS  yesterday's refresh does not stand in for today's: NOT_RUN  <-- pinned defect
...
PASS  a snapshot taken after the refresh is INVERTED  <-- pinned defect
...
PASS  an EMPTY history is UNKNOWN, never NOTHING_CHANGED  <-- pinned defect
...
PASS  a newer compare that failed is FAILED, not a quiet night from the older compare  <-- pinned defect
...
PASS  a compare that stopped running while the chain went on is NOT_RUN, not the old compare's verdict  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  --report that resolves to the history file is refused, whatever its spelling  <-- pinned defect
...
PASS  importing the file runs nothing and exposes the core
PASS  the import probe writes no bytecode beside the script
--------------------------------------------------------------------
179 assertions, 0 failed
```

The full run prints all 179 assertions. The `...` lines are where this block is cut.

## What already exists

- **[Healthchecks.io](https://healthchecks.io/docs/)** (and tools like it) watch for a job that
  did not report. Its documentation says each check has a ping URL, and that a job can append
  `/start` or `/fail` to it to send a start or a failure signal. The service raises an alert when
  a ping does not arrive on time. This is the right tool to learn that a job did not run on
  time. It judges one job against its own schedule. It does not know that the snapshot job must
  run before the refresh job, and it does not read a row count.
- **[Apache Airflow](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)**
  fixes the order by construction. Its documentation says that declared dependencies make up the
  Dag structure, and that by default Airflow waits for all direct upstream tasks to succeed before
  it runs a task. If you build the chain in Airflow, a refresh that never ran stops the compare.
  It does not audit jobs that already run as independent Windows or cron tasks, which is the case
  this tool is for.
- **[taskpulse](https://github.com/uhsear/taskpulse)** (my own tool) audits Windows scheduled
  tasks one at a time. Its README describes per-task checks, a lint of each task's settings and a
  diff of two exports. It does not describe a check on the order between tasks.
- **GitHub.** A search with `gh search repos taskorder --match name` on 2026-10-23 returned 35
  repositories, this one among them. Of the others, 21 have no description. The rest are
  to-do and task-priority apps, a task-ordering service, two interview or test exercises, three
  scheduling-problem studies (one a genetic algorithm, one a dependency-ordering exercise), a
  repository of project files and a small helper that waits for tasks. None describes a tool that reads a run history. The searches "scheduled task order
  dependency empty report", "task scheduler pipeline stale snapshot compare" and "cron job
  ordering check ran before upstream" returned nothing on the same day. A search finds public
  repositories only. It does not show that no such tool exists, and I do not claim that one
  does not.

Healthchecks.io and Airflow are better at what they do than this tool is. This tool answers
a narrower question after the fact, from a history you already have: given these runs, was the
chain in order, and can an empty report be believed?

## Requirements

Python 3.9 or newer and nothing else. No third-party package, no network, no credentials.

The same 179 assertions pass on Windows (Python 3.13.2), on Windows with the Python 3.13.7 that
ships with ArcGIS Pro, and on Ubuntu 24.04.5 (Python 3.12.3). The three runs print identical
lines apart from line endings. The file passes `ast.parse(..., feature_version=(3, 9))`, and it
uses no syntax from a newer release. No Python 3.9 interpreter was available, so the self-test
was not run on 3.9.

```
git clone https://github.com/uhsear/taskorder.git
```

## Usage

1. Export the run history of your tasks to a CSV or JSON file (see "The history file").
2. Name the stages in the order they must run. The last stage is the one that wrote the report.
3. Give the number of rows the last stage reported, if you know it.

```
python taskorder.py --history runs.csv --chain snapshot,refresh,compare --rows 0
python taskorder.py --history runs.csv --chain snapshot,refresh,compare
python taskorder.py --history runs.csv --chain snapshot,refresh,compare --window-hours 168
python taskorder.py --history runs.csv --chain snapshot,refresh,compare --rows 0 --report order.txt --apply
python taskorder.py --self-test
```

| Flag | Default | What it does |
|---|---|---|
| `--history` | none | The run history, CSV or JSON. Required. |
| `--chain` | none | Stage task names in order, comma separated. At least two. The last stage is judged. Required. |
| `--rows` | not given | Rows the last stage reported. With `0`, a clean chain gives `NOTHING_CHANGED`. Without it, a clean chain gives `ORDERED`. |
| `--window-hours` | `24` | The length of one cycle. Set it to the real period of the chain (see Limits). |
| `--report` | off | Write the report text to this file. Needs `--apply`. |
| `--apply` | off | Write `--report`. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. Takes no other flag. |

The parser refuses an abbreviated flag, so `--ap` is not `--apply`. The self-test pins this for
`--ap`, `--rep`, `--his`, `--cha`, `--ro`, `--window` and `--self`. `--report` and `--apply` are
the only way the tool writes. The tool refuses a `--report` that resolves to the history file,
including through a symbolic link.

Exit codes: 0 the chain ran in order, 1 it did not, 2 the input or a flag was unusable, 3 the
history cannot answer. A time or `--window-hours` that is too large to calculate with exits 2.

### The history file

One row per run, with these columns in any order and any case. Extra columns are ignored.

```
task,started,finished,result
snapshot,2026-03-05 01:00:00,2026-03-05 01:02:00,ok
refresh,2026-03-05 02:00:00,2026-03-05 02:20:00,ok
compare,2026-03-05 06:00:00,2026-03-05 06:05:00,ok
```

A JSON file is an array of objects with the same keys. A file that starts with `[` or `{` is read
as JSON, and any other file is read as CSV. Times are `YYYY-MM-DD HH:MM:SS` in 24-hour form, with
a space or a `T`, and an optional `Z` or `+HH:MM` offset. The tool folds an offset into UTC. One
file must use offsets throughout or not at all. `result` is `ok` or `fail`, and any other text is
refused. `finished` may be blank only on a `fail` row. Task names match without regard to case.
The file must be UTF-8.

The tool does not read Task Scheduler, cron or a log. You produce the file from whatever record
you keep. A blank file, or a file with only the header, is an empty history. Keep only the tasks
of the chain in the file (see Limits).

## What it checks

1. The judged run is the newest `ok` run of the last stage.
2. The tool looks for newer evidence. A failed run of any stage in the chain that started after
   the judged run is `FAILED`. If the chain ran on for more than one cycle after the judged run
   and the last stage has no newer run at all, that is `NOT_RUN`.
3. The tool walks back through the chain. For each earlier stage it looks for the latest `ok`
   run that finished before the next stage started, inside one cycle (`--window-hours` before the
   judged run).
4. It looks for runs that broke the order.

The verdict is the first of these that applies:

| Verdict | Exit | When |
|---|---|---|
| `NOT_RUN` | 1 | A stage has no run at all in the cycle, and the history reaches back far enough to show it. Or the last stage has no run after the judged one while the chain ran on for more than one cycle. |
| `FAILED` | 1 | A stage ran in the cycle and every run failed. Or a stage of the chain has a failed run that started after the judged run. |
| `INVERTED` | 1 | A stage only ran after the next stage had started, or an earlier stage ran again after a later stage finished and before the last stage started. |
| `UNKNOWN` | 3 | The history is empty, the last stage has no `ok` run, or a stage is missing and the history starts too late to show that it did not run. |
| `CHANGED` | 0 | The chain ran in order and `--rows` is above 0. |
| `NOTHING_CHANGED` | 0 | The chain ran in order and `--rows` is 0. |
| `ORDERED` | 0 | The chain ran in order and `--rows` was not given. |

Equal times follow one rule. A stage that finishes the second the next one starts is in order. A
stage that starts the second the next stage starts did not run before it. An earlier stage that
starts again the second a later stage finishes is out of order.

The report lists every finding, not only the one in the verdict. When `--rows` is 0 and the
verdict is not a clean one, the report adds a line that says the empty report does not show that
nothing changed. It does not add that line when `--rows` is missing or above 0.

A missing stage is `NOT_RUN` only when the history reaches back past the start of the cycle. A log
that starts after that point may simply have been rotated or cleared. The tool says `UNKNOWN`
there and names the time the history starts.

### Worked example

These are real outputs for the synthetic three-day history in "The history file", with every day
run at the same times, on Windows and on Ubuntu. Each run uses `--chain snapshot,refresh,compare
--rows 0`. The `MEANING` line of each run is left out to save space. Every other line is as
printed.

The healthy history (exit 0):

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-05 06:00:00, rows reported: 0
  snapshot     2026-03-05 01:00:00 .. 2026-03-05 01:02:00  ok
  refresh      2026-03-05 02:00:00 .. 2026-03-05 02:20:00  ok
  compare      2026-03-05 06:00:00 .. 2026-03-05 06:05:00  ok
VERDICT: NOTHING_CHANGED
```

On the last day the refresh did not run (exit 1):

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-05 06:00:00, rows reported: 0
  snapshot     2026-03-05 01:00:00 .. 2026-03-05 01:02:00  ok
  refresh      no run chosen
  compare      2026-03-05 06:00:00 .. 2026-03-05 06:05:00  ok
FINDING NOT_RUN: no run of refresh in the 24 h before compare started
NOTE: the empty report does not show that nothing changed.
VERDICT: NOT_RUN
```

On the last day the snapshot ran at 03:00, after the refresh (exit 1):

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-05 06:00:00, rows reported: 0
  snapshot     no run chosen
  refresh      2026-03-05 02:00:00 .. 2026-03-05 02:20:00  ok
  compare      2026-03-05 06:00:00 .. 2026-03-05 06:05:00  ok
FINDING INVERTED: snapshot only ran at 2026-03-05 03:00:00, after refresh had started at 2026-03-05 02:00:00
NOTE: the empty report does not show that nothing changed.
VERDICT: INVERTED
```

The three days were fine, and on the fourth day the compare failed (exit 1). The older compare
is still the one judged, and the newer failure beats it:

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-05 06:00:00, rows reported: 0
  snapshot     2026-03-05 01:00:00 .. 2026-03-05 01:02:00  ok
  refresh      2026-03-05 02:00:00 .. 2026-03-05 02:20:00  ok
  compare      2026-03-05 06:00:00 .. 2026-03-05 06:05:00  ok
FINDING FAILED: compare failed at 2026-03-06 06:00:00, after the judged compare started at 2026-03-05 06:00:00
NOTE: the empty report does not show that nothing changed.
VERDICT: FAILED
```

The compare stopped after 2026-03-04 while the snapshot and refresh went on (exit 1):

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-04 06:00:00, rows reported: 0
  snapshot     2026-03-04 01:00:00 .. 2026-03-04 01:02:00  ok
  refresh      2026-03-04 02:00:00 .. 2026-03-04 02:20:00  ok
  compare      2026-03-04 06:00:00 .. 2026-03-04 06:05:00  ok
FINDING NOT_RUN: no run of compare after the judged one at 2026-03-04 06:00:00, but the chain ran as late as 2026-03-06 02:00:00, more than 24 h later
NOTE: the empty report does not show that nothing changed.
VERDICT: NOT_RUN
```

An empty history (exit 3):

```
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
FINDING UNKNOWN: the history holds no runs
NOTE: the empty report does not show that nothing changed.
MEANING: the history cannot answer; do not read an empty report as 'nothing changed'
VERDICT: UNKNOWN
```

## Limits

- **The window must match the period.** A cycle is `--window-hours` long. Take the three-day
  example and remove only the refresh of 2026-03-05. With a 72 hour window, the tool takes the
  refresh of 2026-03-04 as this cycle's and then reports the snapshot as `INVERTED`, where the
  24 hour window gives `NOT_RUN`. I ran this. The self-test also covers a 72 hour window that
  accepts an older refresh. Set the window to the real period.
- **The tool has no clock.** It judges the newest `ok` run of the last stage in the file. If
  every stage stopped, an old clean chain still gives a clean verdict. Check the judged time that
  the tool prints. A history exported long after the last compare gets a clean verdict if that
  compare was in order.
- **A compare that is not yet due is not flagged.** If the snapshot and refresh of a new cycle
  have run and the compare has not, the tool judges the compare of the cycle before. It flags the
  missing compare only when the chain has run on for more than one cycle.
- **A failed rerun of an earlier stage is mostly not seen.** If a refresh ran at 02:00 and
  succeeded, and then ran again at 04:00 and failed part way, the tool judges the 02:00 run and
  says `ORDERED`. A failed snapshot rerun between the refresh and the compare is not seen either.
  The tool does see a failed run that starts after the judged compare. The self-test pins both
  known gaps.
- **An earlier-stage rerun that starts during a later stage is only partly seen.** A second `ok`
  snapshot at 02:05-02:07, during a refresh at 02:00-02:20, is not flagged when the normal 01:00
  snapshot also exists. The tool says `NOTHING_CHANGED`. A rerun of an `ok` stage that overlaps the
  start of the last stage is not flagged either. An example is a refresh at 05:55-06:10 when the
  compare starts at 06:00. The scan for reruns checks earlier stages only, not the stage itself.
  The self-test pins both cases.
- **Exit 0 is not proof of work.** The tool trusts the `result` column. A refresh that exits 0
  after loading nothing is an `ok` run.
- **You supply the row count.** The tool does not open the report. It judges the order of the
  runs, and `--rows` only chooses between the three clean verdicts.
- **Filter the history to the chain.** The tool decides that the history reaches back far enough
  from the earliest run of any task in the file. A long-lived unrelated task can make it say
  `NOT_RUN` for a young stage, where `UNKNOWN` would be the honest answer.
- **Export only finished runs.** A row with no result stops the tool with exit 2, so a run
  still going when you exported the history must be left out.
- **Times without an offset are taken as written.** Across a daylight saving change, a local
  time can occur twice or not at all, and a naive history can then order two runs the wrong way
  round. Export times with an offset or in UTC. With offsets, only the `judged:` line carries
  the label `UTC`. The chosen-run lines show the folded times without it.
- **The tool never guesses a format.** Fractional seconds (`01:00:00.123`), 12-hour clock text,
  an offset such as `+0100` with no colon, and a semicolon-delimited CSV are refused with exit 2.
  A UTF-16 CSV, as Windows PowerShell 5 writes with `Export-Csv -Encoding Unicode` or with `>`,
  is refused as "not UTF-8 text". Save it again as UTF-8.
- **PowerShell 5.1 `Export-Csv` needs `-NoTypeInformation`.** Its `#TYPE ...` first line, and a
  trailing comma on data rows, make a row longer than the header and stop the tool with exit 2.
  A whitespace-only line in the middle of a CSV stops it too, with "no started column". The same
  line at the end of the file is dropped.
- **A header with the wrong names gives `UNKNOWN`.** A header-only CSV with other column names is
  an empty history, so the verdict is the safe one, but the message does not name the cause.
- **A repeated key in a JSON object keeps the last value.** This is what Python's JSON reader
  does. The duplicate check covers CSV headers and case variants of a name, not repeated JSON keys.
- **One late run can be reported twice.** A run that is out of order after two later stages
  gets one `INVERTED` line for each. The verdict is the same.
- **`--report` replaces what is at that path.** With `--apply` the tool overwrites any file there.
  Only the history file is guarded, by its resolved path. A hard link to the history is not
  detected.
- **A closed output pipe is not handled.** The tool prints once and has no handler for a reader
  that closes early.
- **A large number of levels or a huge field is refused, not parsed.** Nesting a JSON history
  100000 levels deep, and a CSV field over the size limit of Python's `csv` module, give exit 2.
- **Python 3.9 was not run.** The syntax is checked for 3.9, and the self-test ran on 3.12 and
  3.13 only. A NUL byte in a CSV was not tried on 3.9 or 3.10. The oversize-field test reaches
  the same `csv.Error` handler.

## How each claim was checked

| Claim | How it was run |
|---|---|
| An empty history is `UNKNOWN`, exit 3 | The self-test (three cases) and a real run on an empty CSV on Windows and Ubuntu. |
| A missing refresh is `NOT_RUN`, exit 1 | The self-test and a real run on the synthetic history, on both hosts. |
| A snapshot after the refresh is `INVERTED`, exit 1 | The self-test (a late snapshot, a second snapshot, a snapshot still running when the refresh started) and a real run. |
| A newer failed compare is `FAILED`, exit 1 | The self-test (newer failure, no finished time, two failures, a failed compare that started with the judged one) and a real run on both hosts. |
| A compare that stopped is `NOT_RUN`, exit 1; one that is not yet due is not flagged | The self-test, with the one-cycle edge on both sides, and real runs on both hosts. |
| A history that is too short gives `UNKNOWN`, not `NOT_RUN` | The self-test, and a real run on a history cut to one day. |
| CSV and JSON give the same verdict | The self-test, and a real run on a JSON copy of the same history. |
| The exit code of every verdict | The self-test runs the CLI for `FAILED`, `INVERTED`, `NOT_RUN`, `NOTHING_CHANGED`, `CHANGED`, `ORDERED` and `UNKNOWN`. |
| `--ap` does not write the report | The self-test, and a real run: exit 2, no file. |
| `--report` alone writes nothing, and `--apply` adds only that file | The self-test (it lists the folder before and after, and reads the report back as bytes to check for plain newlines), and real runs. |
| `--report` pointing at the history is refused | The self-test (the history bytes are unchanged), and a real run: exit 2. |
| `--report` pointing at the history through a symbolic link is refused | A real run on Ubuntu with `ln -s`: exit 2, history unchanged. The self-test stubs `os.path.realpath` because a symbolic link needs a privilege on Windows. |
| A huge `--window-hours`, or a time near year 1 with an offset, exits 2 | The self-test (`1e9`, `1e300` and `0001-01-01 00:00:00+05:00`). |
| A deeply nested JSON file or an oversize CSV field exits 2 | The self-test (100000 levels, and a field of 200000 characters). |
| A task name cannot forge a `VERDICT:` line | The self-test, with a newline and an escape character in the name. |
| The format limits in "Limits" | Real runs on a UTF-16 file, fractional seconds, 12-hour text, `+0100`, a semicolon file, a `#TYPE` line, a trailing comma, a whitespace-only line, a header with other names, a row with no result and a repeated JSON key. |
| Branch coverage | `coverage run --branch taskorder.py --self-test` gave 664 statements, 158 branches and 100 percent, with nothing missing. |
| The checks can fail | A sweep of 71 deliberate one-line edits to the decision core, the readers, the parser and the writer: 69 made the self-test fail. The other two change no behaviour (see below). |
| The Healthchecks.io and taskpulse statements | The Healthchecks.io page and the taskpulse README were fetched on 2026-10-23 and read for the statements above. |

The two edits the sweep did not catch are equivalent ones. Changing `finished > bound` to
`finished >= bound` in the late-run branch changes nothing, because a run that finishes exactly at
the bound has already been taken by the branch before it. Changing `rows == 0` to `rows <= 0` in the
verdict changes nothing for the command line, which refuses a negative `--rows`. The sweep script
is a scratch file and is not part of this repository.

There is no network path. The tool never opens a socket, so there is nothing to stub.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

- [taskpulse](https://github.com/uhsear/taskpulse) - audit each Windows scheduled task for silent
  failures and for the configuration that causes them. It looks at one task at a time. This tool
  looks at the order of several.
- [safe-republish](https://github.com/uhsear/safe-republish) - refuse to truncate a feature class
  when the staged replacement fails a plausibility check. It guards the refresh stage. This tool
  checks that the refresh happened, and when.
- [jobharness](https://github.com/uhsear/jobharness) - give an unattended script logging, retry,
  resume and a run lock. It leaves a log, a checkpoint file and a run summary. It does not write
  the history file that this tool reads, so you export that file yourself.
- [logsift](https://github.com/uhsear/logsift) - mine a scheduled job's own log files into a
  metric series.
