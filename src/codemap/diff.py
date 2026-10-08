"""Map a git diff to the symbols it touches, and walk callers to find the blast radius."""
import re
import subprocess
from collections import defaultdict, deque

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def changed_ranges(repo, base):
    """{file: [(start, end)]} of new-side lines changed since the merge base with `base`.

    Diffs against the working tree, so uncommitted edits count too.
    """
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, check=True).stdout

    try:
        start = git('merge-base', base, 'HEAD').strip()
    except subprocess.CalledProcessError:
        start = base
    out = git('diff', '-U0', '--no-color', '--no-ext-diff', start, '--', '*.py')
    ranges, current = defaultdict(list), None
    for line in out.splitlines():
        if line.startswith('+++ '):
            current = line[6:] if line.startswith('+++ b/') else None
        elif current and (m := HUNK.match(line)):
            first, count = int(m.group(1)), int(m.group(2) or 1)
            ranges[current].append((first, first + max(count, 1) - 1))  # count 0 = pure deletion
    return dict(ranges)


def changed_symbols(index, ranges):
    """Innermost symbols whose lines overlap a changed range; the module if none does."""
    by_file = defaultdict(list)
    for n in index.nodes.values():
        if n['file'] and n['kind'] in ('class', 'function', 'method'):
            by_file[n['file']].append(n)
    modules = {n['file']: n['id'] for n in index.nodes.values() if n['kind'] == 'module'}
    changed = set()
    for file, spans in ranges.items():
        if file not in modules:
            continue
        for lo, hi in spans:
            hits = [n for n in by_file[file] if n['line'] <= hi and n['end'] >= lo]
            parents = {n['parent'] for n in hits}
            inner = [n['id'] for n in hits if n['id'] not in parents]
            changed.update(inner or [modules[file]])
    return changed


def blast_radius(index, changed, depth):
    """{node id: hops} for changed nodes (0) and everything that reaches them."""
    rev = defaultdict(set)
    for (src, dst, kind) in index.edges:
        if kind in ('calls', 'references'):
            rev[dst].add(src)
    rev_imports = defaultdict(set)
    for (src, dst, kind) in index.edges:
        if kind == 'imports':
            rev_imports[dst].add(src)
    hops = {c: 0 for c in changed}
    queue = deque((c, 0) for c in changed)
    while queue:
        nid, d = queue.popleft()
        if d == depth:
            continue
        # A module-level change reaches its importers; a symbol change reaches its callers.
        sources = rev_imports[nid] if index.nodes[nid]['kind'] == 'module' else rev[nid]
        for src in sources:
            if src not in hops:
                hops[src] = d + 1
                queue.append((src, d + 1))
    return hops
