"""Build using the active virtualenv: python build_appimage.py (Linux x86_64)."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'work' / 'appimage'
OUTPUT = ROOT / 'dist'
TOOL_URL = 'https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage'
TOOL_SHA256 = '793fca0ce4bcc309dc65d3f66669f58e3755643e4dadbe03932e7b436128494f'


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise SystemExit('Build this target on Linux x86_64.')
    WORK.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--name', 'Still', '--onedir', '--windowed',
        '--collect-submodules', 'pychromecast',
        '--add-data', str(ROOT / 'packaging' / 'still.png') + ':packaging',
        '--specpath', str(WORK), '--workpath', str(WORK / 'build'),
        '--distpath', str(WORK / 'dist'), str(ROOT / 'player.py'))
    appdir = WORK / 'Still.AppDir'
    if appdir.exists():
        shutil.rmtree(appdir)
    shutil.copytree(WORK / 'dist' / 'Still', appdir / 'usr' / 'bin', symlinks=True)
    for name in ('AppRun', 'still.desktop', 'still.png'):
        shutil.copy2(ROOT / 'packaging' / name, appdir / name)
    (appdir / '.DirIcon').symlink_to('still.png')
    for size in (32, 64, 128, 256, 512):
        icons = appdir / 'usr' / 'share' / 'icons' / 'hicolor' / f'{size}x{size}' / 'apps'
        icons.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'packaging' / f'still-{size}.png', icons / 'still.png')
    applications = appdir / 'usr' / 'share' / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    shutil.copy2(appdir / 'still.desktop', applications / 'still.desktop')
    # Include the editable application source and documentation inside the image.
    source = appdir / 'usr' / 'share' / 'still'
    source.mkdir(parents=True)
    for name in ('player.py', 'casting.py', 'dlna.py', 'subtitles.py', 'forced_subtitles.py', 'single_instance.py', 'requirements.txt', 'README.md'):
        shutil.copy2(ROOT / name, source / name)
    tool = WORK / 'appimagetool.AppImage'
    if not tool.exists():
        urllib.request.urlretrieve(TOOL_URL, tool)
    if hashlib.sha256(tool.read_bytes()).hexdigest() != TOOL_SHA256:
        raise SystemExit('appimagetool checksum changed; verify the official release before updating TOOL_SHA256.')
    tool.chmod(0o755)
    run(str(tool), '--appimage-extract', cwd=WORK, stdout=subprocess.DEVNULL)
    destination = OUTPUT / 'Still-Casting-x86_64.AppImage'
    temporary = OUTPUT / 'Still-Casting-x86_64.build.AppImage'
    if temporary.exists():
        temporary.unlink()
    env = dict(os.environ, ARCH='x86_64')
    run(str(WORK / 'squashfs-root' / 'AppRun'), '--no-appstream',
        str(appdir), str(temporary), env=env)
    temporary.replace(destination)
    destination.chmod(0o755)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    (OUTPUT / 'Still-Casting-x86_64.AppImage.sha256').write_text(f'{checksum}  {destination.name}\n')
    print(f'Built {destination}')


if __name__ == '__main__':
    main()
