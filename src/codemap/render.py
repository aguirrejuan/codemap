"""Write the HTML viewer, or a module-level Mermaid diagram for PR comments."""
import json
import os
from collections import defaultdict

TEMPLATE = os.path.join(os.path.dirname(__file__), 'template.html')
PLACEHOLDER = '/*DATA*/null'


def html(data, out):
    blob = json.dumps(data, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
    page = open(TEMPLATE, encoding='utf-8').read()
    if PLACEHOLDER not in page:
        raise SystemExit('template.html has no /*DATA*/null placeholder')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(page.replace(PLACEHOLDER, '/*DATA*/' + blob, 1))


def _safe(nid):
    return 'n_' + ''.join(c if c.isalnum() else '_' for c in nid)


def mermaid(index, hops, summary):
    """Changed and affected modules, nested in their packages, with folded call edges."""
    mod_hop = {}
    for nid, h in hops.items():
        m = index.module_of(nid)
        if m and index.nodes[m]['kind'] == 'module':
            mod_hop[m] = min(h, mod_hop.get(m, h))
    if not mod_hop:
        return f'### Blast radius\n\n{summary}\n'

    folded = defaultdict(int)
    for (src, dst, kind), c in index.edges.items():
        a, b = index.module_of(src), index.module_of(dst)
        if a != b and a in mod_hop and b in mod_hop and kind != 'imports':
            folded[(a, b)] += c

    by_pkg = defaultdict(list)
    for m in mod_hop:
        by_pkg[index.nodes[m]['parent'] or ''].append(m)

    lines = ['```mermaid', 'flowchart LR']
    for pkg in sorted(by_pkg):
        indent = '  '
        if pkg:
            lines.append(f'  subgraph {_safe(pkg)}["{pkg}/"]')
            indent = '    '
        for m in sorted(by_pkg[pkg]):
            tag = 'changed' if mod_hop[m] == 0 else f'hop {mod_hop[m]}'
            lines.append(f'{indent}{_safe(m)}["{index.nodes[m]["label"]}<br/><small>{tag}</small>"]')
        if pkg:
            lines.append('  end')
    for (a, b), c in sorted(folded.items()):
        label = f'|{c}|' if c > 1 else ''
        lines.append(f'  {_safe(a)} -->{label} {_safe(b)}')
    lines.append('  classDef changed fill:#fcebeb,stroke:#a32d2d,color:#501313')
    lines.append('  classDef affected fill:#faeeda,stroke:#ba7517,color:#412402')
    changed = [_safe(m) for m, h in mod_hop.items() if h == 0]
    affected = [_safe(m) for m, h in mod_hop.items() if h > 0]
    if changed:
        lines.append(f'  class {",".join(changed)} changed')
    if affected:
        lines.append(f'  class {",".join(affected)} affected')
    lines.append('```')

    changed_syms = sorted(n for n, h in hops.items() if h == 0)
    detail = '\n'.join(f'- `{n}`' for n in changed_syms)
    return (f'### Blast radius\n\n{summary}\n\n' + '\n'.join(lines) +
            f'\n\n<details><summary>Changed symbols</summary>\n\n{detail}\n</details>\n')
