"""Back up and replace existing desktop configuration files."""

import os
import shutil
import stat
import tempfile


def backup_config(path):
    backup = path.with_name(path.name + ".spen-mirror.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def replace_config(path, content):
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        try:
            output.write(content)
            output.close()
            os.chmod(output.name, mode)
            os.replace(output.name, path)
        finally:
            if os.path.exists(output.name):
                os.unlink(output.name)
