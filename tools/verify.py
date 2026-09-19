#!/usr/bin/env python3
"""Offline checks, including rebuilding every generated file without private inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

ROOT=Path(__file__).resolve().parents[1]
EMIT='ss,ps,ps-vendor,ps-presets,ss-presets,orca,orca-filament,orca-vendor,ps3'


def inventory(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def document_links():
    errors=[]
    for p in ROOT.rglob('*.md'):
        if any(x in p.relative_to(ROOT).parts for x in ('.git','.venv','dist','.cache')):continue
        for target in re.findall(r'\]\(([^)]+)\)',p.read_text()):
            target=target.strip('<>').split('#',1)[0]
            if not target or re.match(r'\w+://',target) or target.startswith('mailto:'):continue
            if not (p.parent/unquote(target)).exists():errors.append(str(p.relative_to(ROOT))+': '+target)
    if errors:raise RuntimeError('Broken documentation links:\n'+'\n'.join(errors))


def run(*args,cwd=ROOT):
    subprocess.run([sys.executable,*args],cwd=cwd,check=True)


def main():
    import jinja2
    if jinja2.__version__!='2.11.3':raise SystemExit('Use requirements-dev.txt: macros require Jinja 2.11.3 for this check')
    version=(ROOT/'VERSION').read_text().strip()
    for file in ('tools/start_gcode.py','svzero_pack.cfg','source/presets.json'):
        if version not in (ROOT/file).read_text():raise RuntimeError('Version mismatch: '+file)
    if 'hosts' in json.loads((ROOT/'source/model.json').read_text()):raise RuntimeError('Public model contains personal hosts')
    if (ROOT/'svzero-personal.cfg').exists():raise RuntimeError('Personal config must not ship')
    provenance=json.loads((ROOT/'source/vendor-profiles/provenance.json').read_text())
    for item in provenance['files']:
        p=ROOT/'source/vendor-profiles'/item['path']
        if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('Upstream checksum mismatch: '+item['path'])
    document_links()
    run('-m','unittest','discover','-s','tests','-p','test_*.py')
    run('tools/validate-bundle.py')
    macros=['nozzle_brush.cfg','chamber_fan.cfg','start_print.cfg','end_print.cfg','cancel_print.cfg','cooldown.cfg','purge_line.cfg','svzero_pack.cfg','filament_load.cfg','orca_compat.cfg','chamber_orbit.cfg']
    run('tools/check-macros.py',*macros)
    run('tools/check-purge-geometry.py')
    with tempfile.TemporaryDirectory(prefix='svzero-rebuild-') as tmp:
        work=Path(tmp)/'pack'
        # No old bundles: proves missing or undeclared generator inputs fail.
        shutil.copytree(ROOT,work,ignore=shutil.ignore_patterns('.git','.venv','dist','.cache','__pycache__','bundles','*.log'))
        run('tools/generate.py','--check','--emit',EMIT,cwd=work)
        before=inventory(ROOT/'bundles');after=inventory(work/'bundles')
        if before!=after:
            changed=sorted(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k))
            raise RuntimeError('Generated bundle drift: '+', '.join(changed))
    print('PASS: offline tests, macro rendering, links, provenance and clean deterministic rebuild')

if __name__=='__main__':main()
