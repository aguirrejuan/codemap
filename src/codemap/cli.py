"""codemap: see a Python repo as nested packages, modules, classes and functions.

    codemap REPO [--diff BASE] [--depth N] [--out PATH] [--open]
    codemap REPO --diff origin/main --mermaid   # for a PR comment
    codemap REPO --json
"""
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone

from . import diff as diffmod
from . import parse, render


def summarise(index, hops):
    n = index.nodes.values()
    mods = sum(1 for x in n if x['kind'] == 'module')
    syms = sum(1 for x in n if x['kind'] in ('class', 'function', 'method'))
    resolved = index.calls_total - sum(index.unresolved.values())
    pct = round(100 * resolved / index.calls_total) if index.calls_total else 0
    base = (f'{mods} modules, {syms} symbols, {len(index.edges)} edges. '
            f'{pct}% of {index.calls_total} calls resolve to project code; the rest are '
            f'stdlib, third-party or dynamic.')
    if hops is None:
        return base, None
    changed = [k for k, h in hops.items() if h == 0]
    reached = len(hops) - len(changed)
    tops = {k.split('.')[0] for k in hops}
    s = lambda n, word: f'{n} {word}' + ('' if n == 1 else 's')
    pr = (f'{s(len(changed), "changed symbol")} reach{"es" if len(changed) == 1 else ""} '
          f'{s(reached, "other")} across {s(len(tops), "top-level package")}.')
    return base, pr


def main(argv=None):
    ap = argparse.ArgumentParser(prog='codemap', description=__doc__.splitlines()[0])
    ap.add_argument('repo', nargs='?', default='.')
    ap.add_argument('--diff', metavar='BASE', help='highlight changes since the merge base with BASE')
    ap.add_argument('--depth', type=int, default=2, help='caller hops in the blast radius (default 2)')
    ap.add_argument('--out', help='HTML path (default ~/codemap-<repo>.html)')
    ap.add_argument('--open', action='store_true', help='open the page in a browser')
    ap.add_argument('--mermaid', action='store_true', help='print a Mermaid PR comment instead')
    ap.add_argument('--json', action='store_true', help='print the graph as JSON instead')
    ap.add_argument('--no-source', action='store_true', help='leave source code out of the page')
    a = ap.parse_args(argv)

    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        sys.exit(f'codemap: {a.repo} is not a directory')
    index = parse.build(repo)
    if not index.mods:
        sys.exit(f'codemap: no Python files found in {repo}')

    hops, ranges = None, {}
    if a.diff:
        try:
            ranges = diffmod.changed_ranges(repo, a.diff)
        except Exception as e:  # git missing, bad ref
            sys.exit(f'codemap: git diff against {a.diff} failed: {e}')
        hops = diffmod.blast_radius(index, diffmod.changed_symbols(index, ranges), a.depth)
    base, pr = summarise(index, hops)

    data = index.data()
    data.update(repo=os.path.basename(repo), root=repo,
                generated=datetime.now(timezone.utc).isoformat(timespec='seconds'), summary=base,
                sources={} if a.no_source else index.sources,
                diff={'base': a.diff, 'depth': a.depth, 'hops': hops, 'summary': pr, 'lines': ranges}
                if a.diff else None)

    if a.json:
        json.dump(data, sys.stdout, indent=1)
        print(base, file=sys.stderr)
        return
    if a.mermaid:
        print(render.mermaid(index, hops or {}, pr or base))
        return

    out = os.path.expanduser(a.out or f'~/codemap-{os.path.basename(repo)}.html')
    render.html(data, out)
    print(base)
    if pr:
        print(pr)
    if index.errors:
        print(f'{len(index.errors)} files failed to parse: ' + ', '.join(index.errors[:5]))
    print(out)
    if a.open:
        webbrowser.open('file://' + out)


if __name__ == '__main__':
    main()
