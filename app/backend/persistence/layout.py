from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from backend.persistence.paths import (  # noqa: F401 -- re-exported for callers that already do `from backend.persistence import layout`
    copy_file,
    long_path,
    move_file,
    path_is_file,
    read_text,
    unlink,
    workspace_root,
    write_text,
)
from backend.persistence.slug import validate_safe_id

if TYPE_CHECKING:
    from backend.models.job import FuzzJob

# The on-disk layout. Every id argument
# is validated before touching a path, so a project/target/job id can never
# escape its own directory (isolation guarantee) -- this is
# the single place that guarantee is enforced structurally.


def workspace_file() -> Path:
    return workspace_root() / "workspace.json"


def projects_dir() -> Path:
    return workspace_root() / "projects"


def project_dir(project_id: str) -> Path:
    return projects_dir() / validate_safe_id(project_id, label="project_id")


def project_file(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def targets_dir(project_id: str) -> Path:
    return project_dir(project_id) / "targets"


def target_dir(project_id: str, target_id: str) -> Path:
    return targets_dir(project_id) / validate_safe_id(target_id, label="target_id")


def target_file(project_id: str, target_id: str) -> Path:
    return target_dir(project_id, target_id) / "target.json"


def tree_file(project_id: str, target_id: str) -> Path:
    return target_dir(project_id, target_id) / "tree.json"


def hide_rules_file(project_id: str, target_id: str) -> Path:
    """Per-target Results hide rules. Kept on disk, not in the browser: a
    localStorage copy is tied to the page's origin (port included), so any
    launch that lands on a different port silently lost every rule."""
    return target_dir(project_id, target_id) / "hide_rules.json"


def results_seen_file(project_id: str, target_id: str) -> Path:
    """Per-target watermark for the Results `NEW` marker: how many
    of each job's results the pentester has already had a chance to see."""
    return target_dir(project_id, target_id) / "results_seen.json"


def jobs_dir(project_id: str, target_id: str) -> Path:
    return target_dir(project_id, target_id) / "jobs"


def job_file(project_id: str, target_id: str, job_id: str) -> Path:
    validate_safe_id(job_id, label="job_id")
    return jobs_dir(project_id, target_id) / f"{job_id}.json"


def job_wordlist_dir(project_id: str, target_id: str, job_id: str) -> Path:
    validate_safe_id(job_id, label="job_id")
    return jobs_dir(project_id, target_id) / job_id / "wordlist"


def outputs_dir(project_id: str, target_id: str) -> Path:
    return target_dir(project_id, target_id) / "outputs"


def job_output_base_legacy(project_id: str, target_id: str, job_id: str) -> Path:
    """Old flat naming (outputs/job_<id>.{txt,json}), kept only so jobs
    created before the human-readable naming scheme still resolve to their
    real files on disk."""
    validate_safe_id(job_id, label="job_id")
    return outputs_dir(project_id, target_id) / f"job_{job_id}"


def job_output_dir(project_id: str, target_id: str, job_id: str) -> Path:
    validate_safe_id(job_id, label="job_id")
    return outputs_dir(project_id, target_id) / job_id


def job_output_base(project_id: str, target_id: str, job_id: str, stem: str) -> Path:
    """outputs/<job_id>/<stem>.{txt,json} -- the second (now superseded, but
    still resolvable for jobs already created this way) naming generation:
    nested per job so two jobs whose human-readable stem collides (e.g.
    re-running the same path) never share, and silently merge into, the
    same report file. Superseded by job_output_base_flat, which solves the
    same collision problem without a subfolder per job -- see its own
    docstring."""
    return job_output_dir(project_id, target_id, job_id) / stem


def job_output_base_flat(project_id: str, target_id: str, job_id: str, stem: str) -> Path:
    """outputs/<stem>_<job_id>.{txt,json} -- every job's report files
    directly in outputs/, no per-job subfolder (explicit user request: "que
    los outputs txt no se guarden en subcarpetas... si quieres añadirle el
    id hazlo al final del nombre"). The job id suffix is what keeps two
    jobs with the same human-readable stem (e.g. re-running the same path)
    from colliding, now that there's no subfolder to separate them."""
    return outputs_dir(project_id, target_id) / f"{stem}_{job_id}"


def job_output_base_for(
    project_id: str, target_id: str, job_id: str, output_stem: str, output_flat: bool = False
) -> Path:
    """Resolves a job's actual report-file base, whichever naming
    generation it was created under: `output_stem` is "" for jobs created
    before the human-readable scheme (flat, legacy naming); non-empty with
    `output_flat` False for jobs created under the nested-per-job scheme
    (kept resolvable, not reused for new jobs); non-empty with
    `output_flat` True for the current scheme."""
    if not output_stem:
        return job_output_base_legacy(project_id, target_id, job_id)
    if output_flat:
        return job_output_base_flat(project_id, target_id, job_id, output_stem)
    return job_output_base(project_id, target_id, job_id, output_stem)


def job_output_base_working(project_id: str, target_id: str, job_id: str) -> Path:
    """outputs/_working_<job_id>.{txt,json} -- short, always safely under
    Windows' MAX_PATH (job_id alone is 12 hex chars) regardless of how long
    the fuzzed path itself is. What dirsearch's OWN subprocess actually
    writes to while a job is still QUEUED/RUNNING/PAUSED (i.e. might still
    be appended to) -- renamed to its full, human-readable
    `job_output_base_for` name (see `job_output_base_current` and
    `JobManager._finalize_output_filename`) once the job reaches a terminal
    status and nothing will ever write to it again. Real, reported bug this
    solves: a long discovered/fuzzed path embedded directly in the output
    filename could push the full on-disk path past MAX_PATH and fail the
    job outright.
    """
    validate_safe_id(job_id, label="job_id")
    return outputs_dir(project_id, target_id) / f"_working_{job_id}"


def job_output_base_current(project_id: str, target_id: str, job: "FuzzJob") -> Path:
    """Where a job's report files ACTUALLY live right now.

    Prefers the full, human-readable final name if it's already there (the
    normal case once a job is done, or a job that predates this working/
    final split and was always written directly to its final name), falling
    back to the short working name otherwise (still QUEUED/RUNNING/PAUSED,
    or the rare case where a terminal job's rename failed and left it at
    its working name instead -- self-healing, no separate error state
    needed for that). `JobManager._run_job`/`._finalize_output_filename`
    decide which name to actually WRITE to the same way; this is the read
    side every other caller (Results, the outputs mirror, delete) should
    go through instead of calling `job_output_base_for` directly.
    """
    final_base = job_output_base_for(project_id, target_id, job.id, job.output_stem, job.output_flat)
    # path_is_file, not a plain Path.is_file() -- final_base can genuinely
    # exceed MAX_PATH (the whole point of this working/final split), and a
    # plain is_file() on a path that long silently reports "doesn't exist"
    # even once it's really there (see paths.py::long_path's own docstring
    # -- reproduced directly, not theoretical).
    if path_is_file(final_base.parent / f"{final_base.name}.txt"):
        return final_base
    working_base = job_output_base_working(project_id, target_id, job.id)
    if path_is_file(working_base.parent / f"{working_base.name}.txt"):
        return working_base
    return final_base


def sessions_dir(project_id: str, target_id: str) -> Path:
    return target_dir(project_id, target_id) / "sessions"
