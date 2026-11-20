"""Export an allowlisted source tree and freshly built release for manual upload."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('_quota_release_builder',ROOT/'scripts/build_release.py')
builder=importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
build=builder.build

TREES=('quota_resume','scripts','tests','skills','.codex-plugin','.agents','.github')
DOCS=('README.md','CHANGELOG.md','PRIVACY.md','RELEASE_NOTES.md','THIRD_PARTY_NOTICES.md',
      '验证报告.md','演示结果.md','.gitignore')


def export(runtime, output):
    output=Path(output)
    if output.exists():
        raise ValueError('Choose a new output directory; existing exports are never overwritten')
    # Only explicitly selected project files enter the publication tree.
    output.mkdir(parents=True)
    repo=output/'repository';repo.mkdir()
    for name in TREES:
        origin=ROOT/name
        if origin.is_symlink() or any(p.is_symlink() for p in origin.rglob('*')):
            raise ValueError('Linked source files refused')
        shutil.copytree(origin,repo/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in DOCS:
        shutil.copy2(ROOT/name,repo/name)
    if (ROOT/'LICENSE').is_file():shutil.copy2(ROOT/'LICENSE',repo/'LICENSE')
    manifest=json.loads((repo/'.codex-plugin/plugin.json').read_text(encoding='utf-8-sig'))
    version=manifest['version']
    assets=output/'release-assets';assets.mkdir()
    archive=assets/f'QuotaResumeLite-v{version}-win-x64.zip'
    build(runtime,archive)
    source=assets/f'QuotaResumeLite-v{version}-source.zip'
    with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(repo.rglob('*')):
            if p.is_file():z.write(p,arcname=p.relative_to(repo).as_posix())
    source.with_suffix('.zip.sha256').write_text(hashlib.sha256(source.read_bytes()).hexdigest()+'  '+source.name+'\n',encoding='ascii')
    shutil.copy2(repo/'RELEASE_NOTES.md',output/'RELEASE_BODY.md')
    summary={
        'version':version,
        'publisher_confirmed':bool(manifest.get('author',{}).get('name')),
        'license_confirmed':bool(manifest.get('license') and (repo/'LICENSE').is_file()),
        'files':{p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob('*')) if p.is_file()},
    }
    summary['ready_to_publish']=summary['publisher_confirmed'] and summary['license_confirmed']
    (output/'UPLOAD_MANIFEST.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'上传说明.txt').write_text(
        'GitHub 仓库只上传 repository 目录里的内容（包括 .agents、.codex-plugin 和 .gitignore）。\n'
        'Release 标签：v'+version+'；标题：Quota Resume Lite v'+version+'（预览版）；勾选 Pre-release。\n'
        'Release 正文复制 RELEASE_BODY.md；附件选择 release-assets 中的四个文件。\n'
        '源码 ZIP 是可选的便携副本，不含独立运行时；安装用户应下载 win-x64 ZIP。\n'
        '不要上传整个工作目录、旧 dist、.local、UPLOAD_MANIFEST.json 或此上传说明。\n'
        'ready_to_publish 为 false 时，公开署名或许可证尚未确认，不应正式发布。\n',encoding='utf-8')
    return {'output':str(output.resolve()),'ready_to_publish':summary['ready_to_publish'],
            'publisher_confirmed':summary['publisher_confirmed'],'license_confirmed':summary['license_confirmed']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.runtime,args.output),ensure_ascii=True))
