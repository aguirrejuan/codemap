# codemap

See a Python repo as nested boxes — packages hold modules, modules hold
classes, classes hold methods — with the calls between them. Point it at a
branch and it marks what the branch changed and every caller within N hops:
the blast radius.

Tools like graphify draw every function, file and docstring as a peer node.
codemap parses with `ast`, so it knows what each thing is and where it lives,
and it keeps that nesting in the picture.

## Install

```bash
uv tool install git+https://github.com/aguirrejuan/codemap
```

No runtime dependencies. The page loads React Flow and ELK from a CDN.

## Use

```bash
codemap ~/code/myrepo --open
```

```bash
codemap ~/code/myrepo --diff origin/main --open
```

```bash
codemap ~/code/myrepo --pr 397 --open
```

```bash
codemap https://github.com/OWNER/REPO/pull/397 --open
```

The page starts at a zoom that fits the repo's size. Use the level buttons to
show packages, modules, classes or functions. Double-click a box to open or
close it. Edges between hidden children fold into one edge on the nearest
visible box, labelled with the number of call sites.

`--pr` shows what GitHub's "Files changed" tab shows: the PR's last commit
against the point where it left its base branch. It works on open and merged
PRs alike, and needs `gh` signed in. Given only a PR link, codemap keeps its
own clone of the repo in `~/.cache/codemap` and fetches it on each run;
`--no-cache` clones into a temporary directory instead and deletes it when done. `--head` and `--pr` check the commit out
in a temporary worktree and delete it afterwards, so your working tree stays
as it is.

Click a box to read its source, with the branch's changed lines marked, and
to list what it calls and what calls it. **Focus** strips the map down to
that symbol and its callers and callees, one to three hops out, still nested
in their packages; Esc leaves it. Tests stay hidden until you untick **Hide
tests**. Hovering a box lights its edges and fades the rest.

| Flag | Effect |
| --- | --- |
| `--diff BASE` | mark symbols changed since the merge base with `BASE`, uncommitted edits included, and their callers |
| `--head REV` | map commit `REV` instead of the working tree; with `--diff BASE`, compares two commits |
| `--pr N` | map GitHub pull request `N` against its base branch, through `gh`; writes `~/codemap-<repo>-pr<N>.html` |
| `--no-cache` | with a PR link, keep no clone: download the repo, map it, delete it |
| `--depth N` | caller hops in the blast radius (default 2) |
| `--out PATH` | where to write the page (default `~/codemap-<repo>.html`) |
| `--mermaid` | print a Markdown PR comment instead of a page |
| `--json` | print the graph instead of a page |
| `--no-source` | leave source code out of the page, for sharing or a smaller file |

Edge kinds: **calls**; **references**, for a function passed as a value, such
as Airflow's `python_callable=`; **reads**, for code that uses a module-level
constant; and **imports**, hidden until you ask.

A **constant** is a module-level name in upper case, such as `DEFAULT_FEED` or
`_GRAMMAR`. When a branch changes one, the constant counts as changed, not its
whole module, and the blast radius follows the functions and constants that
read it. Constants appear on the map only when the diff reaches them, or in
focus mode. A hunk of only comments marks nothing, and a function's
decorators belong to the function.

Tick **External packages** to draw the imports that leave the repo as grey
boxes at the map's edge, third-party apart from the standard library. Click
one to list every module that imports it.

An **import cycle** is a group of modules that import each other, directly or
through a chain. codemap counts only imports that run at load time, so an
import inside a function or under `if TYPE_CHECKING:` breaks no cycle and
makes none. Modules in a cycle carry a red badge, the imports that close it
draw red, and the summary line counts the cycles. Click a cycle in the header
to show only its modules.

Each class and function counts its **callers** and **callees**: the distinct
symbols that call it or pass it as a value, and those it calls. The side panel
shows both counts, and with nothing selected it lists the most-called symbols
and those that call the most. A function with many callers is risky to
change; one with many callees often does several jobs. The most-called
symbols, about the top 5% and never fewer than five callers, draw with a blue
border and tint.

A function marked **no callers** fades to a dashed outline. It has no call or reference anywhere in the
repo. Tests, dunder methods, `main` and upper-case config hooks never get the
mark, and neither does a function that any of these applies to:

- another module imports it by name;
- a decorator other than `staticmethod`, `classmethod` or a cache might register it;
- three or more modules define a top-level function of that name, as Alembic
  migrations define `upgrade`;
- for a method: a class in its hierarchy defines the same name, a base class
  lives outside the repo and may call it as a hook, or the repo calls a method
  of that name on an object of unknown type.

The mark means "no callers found", not "dead". Before deleting a function,
search for `getattr`, string paths in config and framework hooks.

## Post the blast radius on every PR

`--mermaid` prints the changed and affected modules nested in their
packages; GitHub renders it inline.

```yaml
# .github/workflows/codemap.yml
name: codemap
on: pull_request
permissions:
  pull-requests: write
jobs:
  map:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from git+https://github.com/aguirrejuan/codemap codemap . --diff origin/${{ github.base_ref }} --mermaid > codemap.md
      - uses: marocchino/sticky-pull-request-comment@v2
        with:
          header: codemap
          path: codemap.md
```

## Limits

- Python only.
- Calls resolve through imports, re-exports, `self`/`cls`, base classes, and
  locals typed by `x = Foo()`, annotations or `with`. Calls through untyped
  attributes or loop variables, `super()`, `getattr` and other dynamic dispatch
  stay unresolved. The summary line reports the share that resolved;
  stdlib and third-party calls make up most of the rest.
- A change at module level other than a constant, such as an import or a
  lower-case variable, counts as a change to the module, and its blast radius
  follows importers rather than callers.

## Develop

```bash
uv run pytest
```
