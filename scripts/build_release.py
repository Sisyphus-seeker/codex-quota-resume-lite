"""Build an offline Windows ZIP with an independent PSF embedded runtime."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(runtime, output):
    runtime = Path(runtime).resolve()
    if any(p.is_symlink() for p in runtime.iterdir()) or not (runtime/'LICENSE.txt').is_file():
        raise ValueError("Runtime must be an unmodified PSF embedded distribution")
    for filename in ("python.exe", "pythonw.exe", "python313.dll"):
        # Fixed PowerShell script; path travels only as an environment variable.
        import os
        from quota_resume.windows import ps_path, NO_WINDOW
        env = os.environ.copy()
        env['PSModulePath'] = str(ps_path().parent/'Modules')
        env["QUOTA_LITE_VERIFY_FILE"] = str(runtime/filename)
        check = subprocess.run([str(ps_path()), "-NoProfile", "-NonInteractive", "-Command",
            "$s=Get-AuthenticodeSignature -LiteralPath $env:QUOTA_LITE_VERIFY_FILE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') {exit 1}"],
            env=env, timeout=30, creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode:
            raise ValueError("Python signature verification failed")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quota-lite-build-") as work:
        work = Path(work)
        app = work / "app"
        app.mkdir()
        for name in ("quota_resume", "scripts", "skills", ".codex-plugin", ".agents"):
            shutil.copytree(ROOT/name, app/name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "THIRD_PARTY_NOTICES.md", "验证报告.md", "CHANGELOG.md", "PRIVACY.md", "RELEASE_NOTES.md"):
            shutil.copy2(ROOT/name, app/name)
        if (ROOT/'LICENSE').is_file():
            shutil.copy2(ROOT/'LICENSE', app/'LICENSE')
        shutil.copytree(runtime, work/'runtime')
        (work/'runtime/python313._pth').write_text("python313.zip\n.\n", encoding="ascii")
        scripts = {
            "Install.cmd": '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\manage_install.py" install %*',
            "Uninstall.cmd": '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\\scripts\\uninstall_windows.ps1" %*',
            "Status.cmd": '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\quota_resume.py" status',
            "Enable.cmd": '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\quota_resume.py" enable',
            "Pause.cmd": '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\quota_resume.py" pause',
        }
        # The archive uninstaller may run before installation: use its interpreter
        # against the default installed home, instead of the installed wrapper.
        scripts["RemoveFromArchive.cmd"] = '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\manage_install.py" uninstall %*'
        for name, command in scripts.items():
            (work/name).write_text('@echo off\n' + command + '\nset "QUOTA_EXIT=%ERRORLEVEL%"\npause\nexit /b %QUOTA_EXIT%\n', encoding="ascii")
        sums = {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in work.rglob('*') if p.is_file()}
        (work/'SHA256SUMS.json').write_text(json.dumps(sums, sort_keys=True, indent=2), encoding="utf-8")
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(work.rglob('*')):
                if path.is_file():
                    entry = zipfile.ZipInfo(path.relative_to(work).as_posix(), (2026, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n', encoding="ascii")
    return {"archive": str(output), "sha256": digest, "bytes": output.stat().st_size}


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'dist/QuotaResumeLite-v0.1.2-win-x64.zip')
    args = parser.parse_args()
    print(json.dumps(build(args.runtime, args.output)))
