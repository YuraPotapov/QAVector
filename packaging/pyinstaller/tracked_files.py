"""What a bundle takes from a directory people also keep their own work in.

``flows/`` and ``cycles/`` ship with the application, and in a checkout they are
also where somebody records a session or writes a scenario for a project of
their own - untracked, beside the shipped ones. Bundling the directory as it
stood packed all of that into the package, for everyone who installed it. So
only what git tracks is shipped: the build is of the working copy, less what
nobody committed.

Imported by the specs rather than written into them, so the rule has a test.
"""

import os
import subprocess


def tracked_datas(root, directory):
    """``directory``'s tracked files as PyInstaller ``datas`` pairs.

    Each pair is (the file, the directory it goes to inside the bundle), so the
    layout under ``directory`` is kept. A tracked file deleted from the working
    copy is left out rather than failing the build: what is on disk is what is
    being built. Stops the build when git cannot be asked or tracks nothing
    there - bundling everything instead is exactly what this exists to stop.
    """
    try:
        listed = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--", directory],
            check=True, capture_output=True).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("Cannot ask git which files in %s are tracked (%s). "
                         "Build from a git checkout." % (directory, exc))
    datas = []
    for name in listed.split("\0"):
        if not name:
            continue
        relative = os.path.join(*name.split("/"))
        source = os.path.join(root, relative)
        if os.path.isfile(source):
            datas.append((source, os.path.dirname(relative)))
    if not datas:
        raise SystemExit("git tracks nothing in %s under %s - is this the "
                         "checkout?" % (directory, root))
    return datas
