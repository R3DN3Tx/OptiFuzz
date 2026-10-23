from __future__ import annotations

import time
from pathlib import Path

from backend.models.project import Project
from backend.persistence import layout
from backend.persistence.atomic import write_json_atomic
from backend.persistence.paths import path_is_file
from backend.persistence.slug import slugify
from backend.services import project_service, target_service

# Optional, per-project: a COPY of every target's own .txt outputs also
# saved under this directory, one subfolder per target -- the primary
# outputs/ storage under the workspace (backend/persistence/layout.py) is
# never affected either way; this is purely an additional convenience copy.
# "" (Project.outputs_mirror_dir's own default) means no mirroring at all.


def mirror_target_subdir(project: Project, target_name: str) -> Path:
    """<mirror_dir>/<slugified target name>/ -- slugified (not the raw
    name) so a target name with characters unsafe for a folder (e.g. a URL
    fragment containing "/" or ":") never breaks the copy; still
    human-readable, unlike using the target's own opaque id."""
    return Path(project.outputs_mirror_dir) / slugify(target_name)


def mirror_file_path(project: Project, target_name: str, filename: str) -> Path | None:
    """Where the mirrored copy of a job's raw .txt is (or will be), or None when no mirror is set."""
    if not project.outputs_mirror_dir:
        return None
    return mirror_target_subdir(project, target_name) / filename


def open_file(path: Path) -> None:
    """Opens a file with the application the system associates with it (the user's text editor)."""
    import os
    import subprocess
    import sys

    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 -- a path built server-side, never client input
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mirror_output_file(project: Project, target_name: str, txt_path: Path) -> None:
    """Copies one finished job's .txt report into its mirror subfolder --
    best-effort: a mirror-copy failure (e.g. the configured drive is
    unplugged) must never fail or roll back the job itself, which already
    completed successfully in its real, primary location.

    `txt_path` can be the job's full, human-readable final name (once
    renamed by JobManager._finalize_output_filename), which can genuinely
    exceed MAX_PATH -- path_is_file/copy_file (not a plain Path.is_file()/
    shutil.copy2()) are what actually make that safe (see
    persistence/paths.py::long_path's own docstring).
    """
    if not project.outputs_mirror_dir or not path_is_file(txt_path):
        return
    try:
        target_dir = mirror_target_subdir(project, target_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        layout.copy_file(txt_path, target_dir / txt_path.name)
    except OSError:
        pass


def remove_mirrored_file(project: Project, target_name: str, txt_filename: str) -> None:
    """Deleting a job removes its output .txt from the primary location
    (job_manager/manager.py) -- explicit request: the mirrored copy should
    disappear along with it, not linger as a stale leftover.

    Retries briefly on a locked file (PermissionError, an OSError
    subclass) -- reproduced directly: a file just written by
    mirror_output_file() above can still be briefly locked (Windows
    Defender/Search Indexer scanning a freshly-created file is a known,
    common cause) at the exact moment a near-immediate delete tries to
    remove it. A production delete click is never this fast after the
    file was created, but this makes the removal itself genuinely robust
    against that window rather than just hoping it never gets hit.
    """
    if not project.outputs_mirror_dir:
        return
    path = mirror_target_subdir(project, target_name) / txt_filename
    for attempt in range(5):
        try:
            layout.unlink(path, missing_ok=True)
            return
        except OSError:
            if attempt == 4:
                return  # best-effort -- a stale mirrored copy is not worth failing the job's own deletion over
            time.sleep(0.1)


def default_mirror_dir_placeholder(project_id: str) -> str:
    """Shown as the mirror-directory input's placeholder -- purely a
    reference point (where this project's own data already lives inside
    OptiFuzz's workspace), not an actual default mirror location (mirroring
    a target's outputs back into its own already-canonical storage would be
    pointless)."""
    return str(layout.project_dir(project_id))


def set_mirror_dir(project_id: str, new_dir: str) -> Project:
    """Updates the project's mirror directory.

    Two separate things happen, and both were an explicit request:
    - If one was already configured, every already-MIRRORED file MOVES from
      the old location to the new one (_migrate_mirror_dir) -- these already
      exist somewhere, they just need to follow the path change.
    - Every already-COMPLETED job's primary .txt output, across every
      target in the project, gets BACKFILLED into the new location
      (_backfill_from_primary_outputs) -- first-tried behavior only ever
      mirrored incrementally from that point forward, which read as "my
      already-done fuzzes never show up here, only new ones do". A plain
      copy, not a move: the primary outputs/ storage is untouched either
      way.
    Backfilling is naturally idempotent (shutil.copy2 just overwrites an
    identical copy) -- safe to run on every call, not just the first.
    """
    project = project_service.get_project(project_id)
    new_dir = new_dir.strip()
    old_dir = project.outputs_mirror_dir

    if new_dir == old_dir:
        return project

    if old_dir and Path(old_dir).is_dir() and new_dir:
        _migrate_mirror_dir(project, Path(old_dir), Path(new_dir))

    project.outputs_mirror_dir = new_dir
    write_json_atomic(layout.project_file(project_id), project.to_dict())

    if new_dir:
        _backfill_from_primary_outputs(project)

    return project


def _backfill_from_primary_outputs(project: Project) -> None:
    """Copies every target's already-completed .txt reports (found directly
    under its primary outputs/ dir, however deep -- job naming has gone
    through several generations, some nested per-job, see
    persistence/layout.py) into the mirror, skipping nothing: mirror_output_file()
    is a plain overwrite-safe copy2, so a file already mirrored just gets
    copied again harmlessly.
    """
    for target in target_service.list_targets(project.id):
        outputs_dir = layout.outputs_dir(project.id, target.id)
        if not outputs_dir.is_dir():
            continue
        for txt_path in outputs_dir.rglob("*.txt"):
            mirror_output_file(project, target.name, txt_path)


def _migrate_mirror_dir(project: Project, old_dir: Path, new_dir: Path) -> None:
    # Mirrored files carry the SAME filenames as their primary outputs/
    # counterparts, so a genuinely long fuzzed path means these moves can exceed MAX_PATH too --
    # layout.move_file, not a plain shutil.move(), for that reason (see
    # persistence/paths.py::long_path's own docstring). The per-target
    # SUBFOLDER names themselves (`child`/`target` below) are always short
    # (slugified target names), so only the individual files inside a
    # merge need this -- but using it everywhere here is simpler and costs
    # nothing when the plain path was short anyway.
    #
    # Real, reported bug this fixes: this used to move EVERY child of
    # old_dir, then rmdir() it -- if the pentester pointed the mirror at a
    # directory they also kept other, unrelated files in, those got swept
    # up and moved too, and the directory they still used for other things
    # vanished out from under them. Only our own per-target subfolders
    # (mirror_target_subdir's own slugify(target.name), for a target that
    # still exists in THIS project) are ours to move -- anything else in
    # old_dir is left exactly where it is, which also means rmdir() below
    # now only ever succeeds when nothing unrelated was left behind.
    own_subfolders = {slugify(target.name) for target in target_service.list_targets(project.id)}
    new_dir.mkdir(parents=True, exist_ok=True)
    for child in old_dir.iterdir():
        if child.name not in own_subfolders:
            continue
        target = new_dir / child.name
        try:
            if target.exists():
                # Same target subfolder already exists at the destination
                # (e.g. switching back to a directory used before) -- merge
                # file-by-file instead of letting shutil.move refuse to
                # overwrite a directory.
                if child.is_dir():
                    for f in child.iterdir():
                        layout.move_file(f, target / f.name)
                    child.rmdir()
                else:
                    layout.move_file(child, target)
            else:
                layout.move_file(child, target)
        except OSError:
            pass  # best-effort -- a stuck file stays behind rather than failing the whole move
    try:
        old_dir.rmdir()  # only succeeds if now empty -- leaves it otherwise, harmless
    except OSError:
        pass
