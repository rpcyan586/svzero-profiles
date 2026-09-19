#!/usr/bin/env python3
"""Build deterministic release archives from a clean committed public checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def archive(path,files,version):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name in sorted(files):
            info=zipfile.ZipInfo('svzero-profiles-'+version+'/'+name,(1980,1,1,0,0,0))
            info.create_system=3;info.external_attr=0o100644<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,(ROOT/name).read_bytes())


def main():
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=all'],cwd=ROOT).strip():
        raise SystemExit('Commit or remove working-tree changes before packaging; dist/ is ignored')
    version=(ROOT/'VERSION').read_text().strip()
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().strip('\0').split('\0')
    if not tracked:raise SystemExit('No committed public files')
    for n in tracked:
        if (ROOT/n).is_symlink():raise SystemExit('Release symlink refused: '+n)
    manifest=json.loads((ROOT/'release-files.json').read_text())
    out=ROOT/'dist';out.mkdir(exist_ok=True)
    products={}
    for kind,names in manifest.items():
        files=set(names)
        for prefix in list(files):
            if prefix.endswith('/'):
                files.remove(prefix);members=[p for p in tracked if p.startswith(prefix)]
                if not members:raise SystemExit('Empty release prefix: '+prefix)
                files.update(members)
        if not files.issubset(tracked):raise SystemExit('Untracked release inputs: '+str(files-set(tracked)))
        products[kind]=files
    products['source']=set(tracked)
    sums=[]
    for kind,files in products.items():
        path=out/('svzero-profiles-'+version+'-'+kind+'.zip')
        archive(path,files,version)
        sums.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)
        print(path.name,len(files),'files')
    (out/'SHA256SUMS').write_text('\n'.join(sums)+'\n')

if __name__=='__main__':main()
