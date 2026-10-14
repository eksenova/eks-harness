---
name: eks-debugging
description: "Debug eks runs: breakpoints in source (s.debug.break_) and on the command line (--break n12, op:ui.tap, label:, when:), pause on error, the paused state (node, resolved args, screen and DOM snapshots, values so far), continue, step, retry, skip, edit args, abort, ad-hoc ops in the paused session, attaching with eks-harness debug, MCP eks_run_control, journals (runs show --journal --events), replays and --repeat to find nondeterminism. Use when a flow fails in a way the report does not explain or behaves differently between runs."
---

# Debugging runs

## Breakpoints

```python
@eks.flow(app="web-app", seed="standard")
def debugging(s: eks.Session):
    s.debug.pause_on_error()
    s.ui.nav("/tenant/invoice/new")
    s.ui.fill("#amount", "2450.10")
    s.debug.break_("before-submit")
    s.ui.tap("#submit")
```

From the command line, without editing the flow:

```bash
eks-harness run flows/x.py --break op:ui.tap                 # before every tap
eks-harness run flows/x.py --break n4k2m7q9xa                # a node id (from plan --json or the run lines)
eks-harness run flows/x.py --break label:before-submit
eks-harness run flows/x.py --break 'op:ui.*,when:attempt > 0'
eks-harness run flows/x.py --break-on-error
```

A spec is criteria joined by commas, all of which must hold: `node:<id>` or a bare id, `op:<glob>`,
`label:<text>`, `when:<expr>` (last, because it takes the rest of the string). Conditions see `node`, `op`,
`label`, `src`, `attempt`, `instance`, `args`, `values`, `error` and may call `len`, `contains`,
`startswith`, `endswith`, `matches(text, pattern)`, `lower`, `upper`. `--break` and `--break-on-error` imply
`--debug`; without `--debug` source breakpoints are recorded but never pause.

## While paused

With a terminal, `eks-harness run` opens a prompt at the pause. It shows the reason, node, op, source line,
resolved args, the error (if any), screen and DOM snapshots (artifacts tagged `pause`) and the values
computed so far.

| Command | Does |
| --- | --- |
| `continue` | resume |
| `step` | resume and pause before the next node |
| `retry` | run the paused node again (with edited args when edited) |
| `skip [json]` | mark it skipped, with an optional output value |
| `edit <json>` | replace the paused node's args; stays paused |
| `do <op> <json>` | run an ad-hoc op in the paused session, for example `do ui.tree {}` or `do capture.shot {"name": "now"}` |
| `break <spec>`, `delete <id>`, `breaks` | manage breakpoints |
| `status`, `abort`, `help` | |

Without a terminal the run exits 12 and prints how to attach: `eks-harness debug <run>` follows the run and
opens the prompt at every pause. One command at a time from scripts:

```bash
eks-harness runs control <run> status
eks-harness runs control <run> do --op ui.text --args '{"target": "#total"}'
eks-harness runs control <run> edit --args '{"target": "#submit-2"}'
eks-harness runs control <run> continue
```

MCP: `eks_run(..., breakpoints=["op:ui.tap"], debug=True)` returns `{run, pause}` when it pauses;
`eks_run_control(run_id, action="do", op="ui.tree", args={})`, `action="continue"`, ... ; `eks_run_status(run_id,
wait=30)` waits for the next pause or the end. A pause longer than the hub's pause timeout aborts the run.

## Journals, replays, repeats

```bash
eks-harness runs list [--status failed]
eks-harness runs show <run> --journal --events      # every node instance with args, outputs, timings, errors
eks-harness runs replay <run>                        # the same plan again
eks-harness run flows/x.py --repeat 5                # five runs, compared node by node
```

`--repeat N` links every run to the first, compares the journals and prints the nodes whose outcome or value
diverged; the exit code is the worst run's. Use it before calling a failure flaky.

Cheap first steps when a node fails: read the failure block (code, message, source line), Read the failure
screenshot, rerun with `--break-on-error` and run `do ui.tree {}` or `do web.logs {"kind": "errors"}` in the
paused session.

## Not available yet

The recorder (`eks-harness record`, actions to DSL source) and the plan debugger page in the web UI are
planned; use the CLI prompt or `eks_run_control` meanwhile.
