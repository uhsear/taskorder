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

This case comes from reading a pipeline of this shape, not from an incident that I watched fail.
The tool shows what it would catch. Every example in this README is synthetic.

The tool reads a run history (one row per task run) and the order you declare for the chain. It
judges the latest compare and gives one verdict. If the history is empty, the verdict is
`UNKNOWN`. It is never "nothing changed".

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
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  importing the file runs nothing and exposes the core
PASS  the import probe writes no bytecode beside the script
--------------------------------------------------------------------
138 assertions, 0 failed
```

The full run prints all 138 assertions. The `...` lines are where this block is cut.

## What already exists

- **[Healthchecks.io](https://healthchecks.io/docs/)** (and tools like it) watch for a job that
  did not report. Each check has a ping URL. A job pings it to signal a start, a success or a
  failure, and the service alerts when a ping is late. This is the right tool to learn that a job
  did not run on time. It judges one job against its own schedule. It does not know that the
  snapshot job must run before the refresh job, and it does not read a row count.
- **[Apache Airflow](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)**
  fixes the order by construction. Its documentation says that declared dependencies make up the
  Dag structure, and that by default Airflow waits for all direct upstream tasks to succeed before
  it runs a task. If you build the chain in Airflow, a refresh that never ran stops the compare.
  It does not audit jobs that already run as independent Windows or cron tasks, which is the case
  this tool is for.
- **[taskpulse](https://github.com/uhsear/taskpulse)** (my own tool) audits Windows scheduled
  tasks one at a time. Its README describes per-task checks. It does not describe a check on the
  order between tasks.
- **GitHub.** A search with `gh search repos` on 2026-10-22 for `taskorder` returned eight
  repositories. By their descriptions they are an interview exercise, a scheduling-problem study
  and six with no description. Searches for "scheduled task order dependency empty report",
  "task scheduler pipeline stale snapshot compare" and "cron job ordering check ran before
  upstream" returned nothing. A search finds
  public repositories only. It does not show that no such tool exists, and I do not claim that one
  does not.

Healthchecks.io and Airflow are better at what they do than this tool is. This tool answers
a narrower question after the fact, from a history you already have: given these runs, was the
chain in order, and can an empty report be believed?

## Requirements

Python 3.9 or newer and nothing else. No third-party package, no network, no credentials.

The same 138 assertions pass on Windows (Python 3.13.2), on Windows with the Python 3.13.7 that
ships with ArcGIS Pro, and on Ubuntu 24.04.5 (Python 3.12.3). The file passes
`ast.parse(..., feature_version=(3, 9))`, and it uses no syntax from a newer release. No Python
3.9 interpreter was available, so the self-test was not run on 3.9.

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
the only way the tool writes. The tool refuses a `--report` that is the history file.

Exit codes: 0 the chain ran in order, 1 it did not, 2 the input or a flag was unusable, 3 the
history cannot answer.

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

The tool does not read Task Scheduler, cron or a log. You produce the file from whatever record
you keep. A blank file, or a file with only the header, is an empty history.

## What it checks

The tool takes the latest `ok` run of the last stage. Then it walks back through the chain. For
each earlier stage it looks for the latest `ok` run that finished before the next stage started,
inside one cycle (`--window-hours` before the judged run). Then it looks for runs that broke the
order. The verdict is the first of these that applies:

| Verdict | Exit | When |
|---|---|---|
| `NOT_RUN` | 1 | A stage has no run at all in the cycle, and the history reaches back far enough to show it. |
| `FAILED` | 1 | A stage ran in the cycle and every run failed. |
| `INVERTED` | 1 | A stage only ran after the next stage had started, or an earlier stage ran again after a later stage finished and before the last stage started. |
| `UNKNOWN` | 3 | The history is empty, the last stage has no `ok` run, or a stage is missing and the history starts too late to show that it did not run. |
| `CHANGED` | 0 | The chain ran in order and `--rows` is above 0. |
| `NOTHING_CHANGED` | 0 | The chain ran in order and `--rows` is 0. |
| `ORDERED` | 0 | The chain ran in order and `--rows` was not given. |

The report lists every finding, not only the one in the verdict. When `--rows` is 0 and the
verdict is not a clean one, the report adds a line that says the empty report does not show that
nothing changed.

A missing stage is `NOT_RUN` only when the history reaches back past the start of the cycle. A log
that starts after that point may simply have been rotated or cleared. The tool says `UNKNOWN`
there and names the time the history starts.

### Worked example

These are real outputs for the synthetic three-day history in "The history file", with every day
run at the same times. The fourth case is an empty file.

```
$ python taskorder.py --history healthy.csv --chain snapshot,refresh,compare --rows 0
taskorder: chain snapshot -> refresh -> compare, one cycle = 24 h
judged: compare, started 2026-03-05 06:00:00, rows reported: 0
  snapshot     2026-03-05 01:00:00 .. 2026-03-05 01:02:00  ok
  refresh      2026-03-05 02:00:00 .. 2026-03-05 02:20:00  ok
  compare      2026-03-05 06:00:00 .. 2026-03-05 06:05:00  ok
MEANING: the chain ran in order and the last stage found no rows
VERDICT: NOTHING_CHANGED
```

On the last day the refresh did not run (exit 1):

```
  refresh      no run chosen
FINDING NOT_RUN: no run of refresh in the 24 h before compare started
NOTE: the empty report does not show that nothing changed.
VERDICT: NOT_RUN
```

On the last day the snapshot ran at 03:00, after the refresh (exit 1):

```
FINDING INVERTED: snapshot only ran at 2026-03-05 03:00:00, after refresh had started at 2026-03-05 02:00:00
VERDICT: INVERTED
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

- **The window must match the period.** A cycle is `--window-hours` long. With a daily chain
  and a 72 hour window, the tool accepts the refresh from two days earlier as this cycle's, and
  then reports the snapshot as `INVERTED`. I ran this on the example above. The self-test also
  covers a 72 hour window that accepts an older refresh. Set the window to the real period.
- **A failed rerun is not seen.** If a refresh ran at 02:00 and succeeded, and then ran again at
  04:00 and failed part way, the tool judges the 02:00 run and says `ORDERED`. A failed rerun that
  truncated a table before it failed is invisible to it. The self-test pins this as a known limit.
- **Exit 0 is not proof of work.** The tool trusts the `result` column. A refresh that exits 0
  after loading nothing is an `ok` run.
- **You supply the row count.** The tool does not open the report. It judges the order of the
  runs, and `--rows` only chooses between the three clean verdicts.
- **Only the latest compare is judged.** An older empty report is not re-judged.
- **Times without an offset are taken as written.** Across a daylight saving change, a nightly
  task at 02:00 can run twice or not at all, and a naive history can then order two runs the wrong
  way round. Export times with an offset or in UTC.
- **Python 3.9 was not run.** The syntax is checked for 3.9, and the self-test ran on 3.12 and
  3.13 only.

## How each claim was checked

| Claim | How it was run |
|---|---|
| An empty history is `UNKNOWN`, exit 3 | The self-test (three cases) and a real run on an empty CSV on Windows and Ubuntu. |
| A missing refresh is `NOT_RUN`, exit 1 | The self-test and a real run on the synthetic history, on both hosts. |
| A snapshot after the refresh is `INVERTED` | The self-test (a late snapshot and a second snapshot) and a real run. |
| A history that is too short gives `UNKNOWN`, not `NOT_RUN` | The self-test, and a real run on a history cut to one day. |
| CSV and JSON give the same verdict | The self-test, and a real run on a JSON copy of the same history. |
| `--ap` does not write the report | The self-test, and a real run: exit 2, no file. |
| `--report` alone writes nothing, and `--apply` adds only that file | The self-test (it lists the folder before and after), and real runs. |
| `--report` pointing at the history is refused | The self-test (the history bytes are unchanged), and a real run: exit 2. |
| A task name cannot forge a `VERDICT:` line | The self-test, with a newline and an escape character in the name. |
| Branch coverage | `coverage run --branch taskorder.py --self-test` gave 568 statements, 146 branches and 100 percent. |
| The checks can fail | A sweep of 22 deliberate edits to the decision core and the writer: 21 made the self-test fail. One edit (`rows == 0` written as `not rows`) changes no output, so no test can fail on it. |

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
  resume and a run lock, so each stage leaves a record that this tool can read.
- [logsift](https://github.com/uhsear/logsift) - mine a scheduled job's own log files into a
  metric series.
