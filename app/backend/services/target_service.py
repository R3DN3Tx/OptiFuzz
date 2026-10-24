from __future__ import annotations

import re
import shutil
from urllib.parse import urljoin, urlsplit

from backend.models.target import Target
from backend.models.timestamps import now_iso
from backend.persistence import layout
from backend.persistence.atomic import DamagedFileError, load_model, write_json_atomic
from backend.persistence.slug import clean_name, make_id
from backend.services.project_service import get_project


class TargetNotFoundError(Exception):
    pass


class DuplicateTargetNameError(Exception):
    pass


_URL_FORBIDDEN_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
MAX_URL_LENGTH = 2000


def validate_base_url(base_url: str) -> str:
    """The base URL as it is stored: http or https, a host, and a port inside 1-65535. Anything else
    (no scheme, ftp:, javascript:, spaces, control characters) is refused when the target is added, not
    later when a job fails to start."""
    base_url = base_url.strip()
    if not base_url:
        raise ValueError("Base URL is required")
    if len(base_url) > MAX_URL_LENGTH:
        raise ValueError(f"Base URL must be {MAX_URL_LENGTH} characters or fewer")
    if _URL_FORBIDDEN_CHARS.search(base_url):
        raise ValueError("Base URL cannot contain spaces or control characters")
    parts = urlsplit(base_url)  # never raises for these inputs except on a malformed bracketed host
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or "://" not in base_url:
        raise ValueError("Base URL must start with http:// or https:// (for example https://example.com)")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("The port in the Base URL must be a number between 1 and 65535") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The port in the Base URL must be a number between 1 and 65535")
    if not parts.hostname:
        raise ValueError("Base URL needs a host name or address (for example https://example.com)")
    return base_url


def create_target(project_id: str, name: str, base_url: str) -> Target:
    get_project(project_id)  # raises ProjectNotFoundError if the project is missing

    name = clean_name(name, "Target")
    base_url = validate_base_url(base_url)

    if any(existing.name.strip().lower() == name.lower() for existing in list_targets(project_id)):
        raise DuplicateTargetNameError(f"A target named {name!r} already exists in this project")

    target = Target(
        id=make_id(name),
        project_id=project_id,
        name=name,
        base_url=base_url,
        created_at=now_iso(),
    )
    write_json_atomic(layout.target_file(project_id, target.id), target.to_dict())

    # Pre-create the isolated subtree so it exists even before any job runs
    # (one target's data never shares a directory with another's).
    layout.jobs_dir(project_id, target.id).mkdir(parents=True, exist_ok=True)
    layout.outputs_dir(project_id, target.id).mkdir(parents=True, exist_ok=True)
    layout.sessions_dir(project_id, target.id).mkdir(parents=True, exist_ok=True)

    return target


def list_targets(project_id: str) -> list[Target]:
    targets_dir = layout.targets_dir(project_id)
    if not targets_dir.is_dir():
        return []

    targets = []
    for entry in sorted(targets_dir.iterdir()):
        target_file = entry / "target.json"
        if target_file.is_file():
            try:
                targets.append(load_model(target_file, Target.from_dict))
            except DamagedFileError:
                continue  # listed by damage_service instead, so one bad file never hides the others
    return targets


def get_target(project_id: str, target_id: str) -> Target:
    path = layout.target_file(project_id, target_id)
    if not path.is_file():
        raise TargetNotFoundError(target_id)
    return load_model(path, Target.from_dict)


def rename_target(project_id: str, target_id: str, new_name: str) -> Target:
    new_name = clean_name(new_name, "Target")

    target = get_target(project_id, target_id)
    if any(
        existing.id != target_id and existing.name.strip().lower() == new_name.lower()
        for existing in list_targets(project_id)
    ):
        raise DuplicateTargetNameError(f"A target named {new_name!r} already exists in this project")

    target.name = new_name
    write_json_atomic(layout.target_file(project_id, target_id), target.to_dict())
    return target


def delete_target(project_id: str, target_id: str) -> None:
    target_dir = layout.target_dir(project_id, target_id)
    if not target_dir.is_dir():
        raise TargetNotFoundError(target_id)
    shutil.rmtree(target_dir)


def normalize_fuzz_path(path: str) -> str:
    """The path a job is stored under: leading and trailing "/" on the path part.

    dirsearch always fuzzes a URL's path as a directory (its requester appends
    the "/"), so a job started on "/app" really fuzzes "/app/". Storing it as
    typed made Results and the tree tag "/app" instead of "/app/". A query
    string ("?a=a") is kept as typed, after the slash.
    """
    path = path.strip()
    cut = min((i for i in (path.find("?"), path.find("#")) if i != -1), default=len(path))
    head, tail = path[:cut], path[cut:]
    if not head.startswith("/"):
        head = "/" + head
    if not head.endswith("/"):
        head += "/"
    return head + tail


def resolve_fuzz_url(base_url: str, path: str) -> str:
    """Join a Target's base_url with a sub-path the same way a human would
    expect."""
    if not base_url.endswith("/"):
        base_url += "/"
    return urljoin(base_url, path.lstrip("/"))
