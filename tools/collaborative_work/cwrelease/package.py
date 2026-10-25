"""Validate the runtime bundle and build or verify deterministic release artifacts."""

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from markdown_it import MarkdownIt
import yaml


class ReleaseError(RuntimeError):
    """A candidate or release artifact does not satisfy the release contract."""


SKILLS = ("brainstorm", "complexity", "develop", "help", "maintain", "matt", "start", "story", "team")
REFERENCES = ("brainstorming-dialogue", "collaboration-patterns", "commands", "matt-execution",
              "skill-maintenance", "software-development", "story-learning", "task-complexity",
              "team-orchestration")
REQUIRED = {".codex-plugin/plugin.json", ".agents/plugins/marketplace.json", "README.md",
            "skills/start/LICENSE.superpowers"}
REQUIRED.update(f"skills/{name}/{leaf}" for name in SKILLS for leaf in ("SKILL.md", "agents/openai.yaml"))
REQUIRED.update(f"skills/start/references/{name}.md" for name in REFERENCES)
OPTIONAL = {"CHANGELOG.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.matt", "THIRD_PARTY_NOTICES.md",
            "THIRD-PARTY-NOTICES.md", "THIRD_PARTY.md", "compatibility.json"}
ALLOWED = REQUIRED | OPTIONAL
DIRECTORIES = {str(parent) for name in ALLOWED for parent in PurePosixPath(name).parents if str(parent) != "."}
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"Duplicate mapping key: {key}")
        result[key] = value
    return result


class _UniqueLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader, node):
    loader.flatten_mapping(node)
    return _unique_pairs((loader.construct_object(key, deep=True), loader.construct_object(value, deep=True))
                         for key, value in node.value)


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _json(text, label):
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError) as error:
        raise ReleaseError(f"Invalid JSON in {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"Expected an object in {label}")
    return value


def _yaml(text, label):
    try:
        value = yaml.load(text, Loader=_UniqueLoader)
    except (yaml.YAMLError, TypeError, ValueError) as error:
        raise ReleaseError(f"Invalid YAML in {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"Expected a YAML mapping in {label}")
    return value


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (UnicodeError, OSError) as error:
        raise ReleaseError(f"Cannot read UTF-8 text {path}: {error}") from error


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(c) < 32 for c in value) or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ReleaseError(f"Invalid relative file path: {value!r}")
    return value


def _scan(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"Expected a real directory: {root}")
    files, directories = {}, set()
    try:
        for parent, dirs, names in os.walk(root, followlinks=False):
            for name in sorted(dirs + names):
                path = Path(parent) / name
                relative = path.relative_to(root).as_posix()
                _relative(relative)
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    directories.add(relative)
                elif stat.S_ISREG(mode):
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                    with os.fdopen(os.open(path, flags), "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ReleaseError(f"Not a regular file: {path}")
                        digest = sha256()
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    files[relative] = digest.hexdigest()
                else:
                    raise ReleaseError(f"Symlinks and special files are not accepted: {path}")
    except OSError as error:
        raise ReleaseError(f"Cannot inventory {root}: {error}") from error
    return dict(sorted(files.items())), directories


def snapshot_tree(root):
    """Return every regular file's POSIX relative path and SHA-256; reject links."""
    return _scan(root)[0]


def _version(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ReleaseError("Plugin version must be a SemVer string")
    return value


def _markdown_targets(text):
    parser = MarkdownIt("commonmark")
    # Parse all schemes so our source policy can reject them. The parser's default
    # URL filter otherwise turns e.g. file: links into text. We never render/fetch.
    parser.validateLink = lambda target: True
    for block in parser.parse(text):
        if block.type != "inline":
            continue
        for token in block.children or ():
            attribute = {"link_open": "href", "image": "src"}.get(token.type)
            if attribute is not None:
                yield token.attrGet(attribute)
            # Image children describe alt text, not separately rendered links.
            # Code/fence tokens have no parsed link destinations to inspect.


def _links(root, relative, text):
    for target in _markdown_targets(text):
        if not isinstance(target, str):
            raise ReleaseError(f"Invalid Markdown link in {relative}")
        try:
            parsed = urlsplit(target)
        except ValueError as error:
            raise ReleaseError(f"Invalid link in {relative}: {target}") from error
        if parsed.scheme in ("https", "http", "mailto"):
            continue
        if parsed.scheme or parsed.netloc:
            raise ReleaseError(f"Unsupported local link in {relative}: {target}")
        local = unquote(parsed.path)
        if not local:
            continue
        if "\\" in local or "\x00" in local or Path(local).is_absolute():
            raise ReleaseError(f"Unsafe link in {relative}: {target}")
        resolved = (root / relative).parent.joinpath(local).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ReleaseError(f"Missing or escaping link in {relative}: {target}")


def validate_source(root):
    """Read-only validation of the exact supported collaborative-work runtime layout."""
    root = Path(root)
    files, directories = _scan(root)
    root = root.resolve()
    missing, extra = REQUIRED - files.keys(), files.keys() - ALLOWED
    if missing or extra or directories - DIRECTORIES:
        raise ReleaseError(f"Runtime layout mismatch: missing={sorted(missing)}, extra={sorted(extra)}, "
                           f"unexpected_directories={sorted(directories - DIRECTORIES)}")
    manifest = _json(_read_text(root / ".codex-plugin/plugin.json"), "plugin.json")
    if manifest.get("name") != "collaborative-work" or manifest.get("skills") != "./skills/":
        raise ReleaseError("Plugin name or skills directory does not match the runtime contract")
    if manifest.keys() - {"name", "version", "description", "author", "skills", "interface", "license"}:
        raise ReleaseError("Unexpected plugin components; this bundle supports skills only")
    if "license" in manifest and (not isinstance(manifest["license"], str) or not manifest["license"].strip()):
        raise ReleaseError("Plugin license metadata must be a nonempty string")
    version = _version(manifest.get("version"))
    catalog = _json(_read_text(root / ".agents/plugins/marketplace.json"), "marketplace.json")
    marketplace = catalog.get("name")
    if not isinstance(marketplace, str) or not SLUG.fullmatch(marketplace):
        raise ReleaseError("Marketplace name must be a lowercase slug")
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        raise ReleaseError("The bundled marketplace must contain exactly one plugin")
    if plugins[0].get("name") != manifest["name"] or plugins[0].get("source") != {"source": "local", "path": "./"}:
        raise ReleaseError("Bundled marketplace must point to this plugin's local root")
    for name in SKILLS:
        path = f"skills/{name}/SKILL.md"
        text = _read_text(root / path)
        frontmatter = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.DOTALL)
        if not frontmatter:
            raise ReleaseError(f"Missing YAML frontmatter: {path}")
        header = _yaml(frontmatter[1], path)
        metadata = header.get("metadata")
        if (header.get("name") != name or not isinstance(header.get("description"), str)
                or not header["description"].strip() or not isinstance(metadata, dict)
                or metadata.get("version") != version):
            raise ReleaseError(f"Skill identity, description or version mismatch: {path}")
        ui_path = f"skills/{name}/agents/openai.yaml"
        ui = _yaml(_read_text(root / ui_path), ui_path)
        interface = ui.get("interface")
        if not isinstance(interface, dict):
            raise ReleaseError(f"Missing interface: {ui_path}")
        namespace = f"collaborative-work:{name}"
        for field in ("display_name", "short_description", "default_prompt"):
            if not isinstance(interface.get(field), str) or not interface[field].strip():
                raise ReleaseError(f"Missing {field}: {ui_path}")
        if not re.search(re.escape(namespace) + r"(?![\w:-])", interface["display_name"]):
            raise ReleaseError(f"Interface display name has the wrong namespace: {ui_path}")
        if not re.search(r"\$" + re.escape(namespace) + r"(?![\w:-])", interface["default_prompt"]):
            raise ReleaseError(f"Default prompt must invoke ${namespace}: {ui_path}")
        policy = ui.get("policy", {})
        if name == "team" and (not isinstance(policy, dict) or policy.get("allow_implicit_invocation") is not False):
            raise ReleaseError("team must explicitly disable implicit invocation")
    for relative in files:
        if relative.endswith(".md"):
            _links(root, relative, _read_text(root / relative))
        elif relative == "compatibility.json":
            _json(_read_text(root / relative), relative)
    if snapshot_tree(root) != files:
        raise ReleaseError("Source changed during validation")
    return {"name": manifest["name"], "version": version, "marketplace": marketplace,
            "skills": list(SKILLS), "files": files}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _same_or_absent(path, content):
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise ReleaseError(f"Existing version has different bytes; refusing to overwrite: {path}")
        return True
    return False


def build(source, output_dir):
    """Build ZIP_STORED plus external manifest/checksum; existing versions are immutable."""
    metadata = validate_source(source)
    source = Path(source).resolve()
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink() or output_dir.resolve().is_relative_to(source):
        raise ReleaseError("Build output must be outside the source tree and cannot be a symlink")
    memory = BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, digest in metadata["files"].items():
            content = (source / relative).read_bytes()
            if sha256(content).hexdigest() != digest:
                raise ReleaseError("Source changed during packaging")
            info = zipfile.ZipInfo(f"{metadata['name']}/{relative}", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, content)
    if snapshot_tree(source) != metadata["files"]:
        raise ReleaseError("Source changed during packaging")
    stem = f"{metadata['name']}-plugin-v{metadata['version']}"
    archive_name, manifest_name, checksum_name = f"{stem}.zip", f"{stem}.manifest.json", f"{stem}.sha256"
    archive_bytes = memory.getvalue()
    receipt = {"schema_version": 1, **metadata,
               "archive": {"name": archive_name, "sha256": sha256(archive_bytes).hexdigest()}}
    contents = {archive_name: archive_bytes, manifest_name: _json_bytes(receipt)}
    contents[checksum_name] = "".join(f"{sha256(value).hexdigest()}  {name}\n"
                                    for name, value in sorted(contents.items())).encode("ascii")
    for name, content in contents.items():
        _same_or_absent(output_dir / name, content)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        path = output_dir / name
        if not _same_or_absent(path, content):
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                _same_or_absent(path, content)
    return {"name": metadata["name"], "version": metadata["version"],
            "archive": str(output_dir / archive_name), "manifest": str(output_dir / manifest_name),
            "checksum": str(output_dir / checksum_name), "sha256": receipt["archive"]["sha256"]}


def verify(archive, manifest_path, checksum_path, extract_to=None):
    """Verify receipts, safe ZIP paths and every member before optional fresh extraction."""
    archive, manifest_path, checksum_path = map(Path, (archive, manifest_path, checksum_path))
    for path in (archive, manifest_path, checksum_path):
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"Expected a regular release artifact: {path}")
    receipt = _json(_read_text(manifest_path), manifest_path)
    if receipt.get("schema_version") != 1 or receipt.get("name") != "collaborative-work":
        raise ReleaseError("Unsupported release manifest")
    version = _version(receipt.get("version"))
    stem = f"collaborative-work-plugin-v{version}"
    if archive.name != f"{stem}.zip" or manifest_path.name != f"{stem}.manifest.json" or checksum_path.name != f"{stem}.sha256":
        raise ReleaseError("Artifact names do not match the declared version")
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("Release manifest must contain a nonempty file inventory")
    for relative, digest in files.items():
        _relative(relative)
        if relative not in ALLOWED or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ReleaseError(f"Invalid release inventory entry: {relative}")
    checksums = {}
    for line in _read_text(checksum_path).splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match[2] in checksums:
            raise ReleaseError("Invalid or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if set(checksums) != {archive.name, manifest_path.name}:
        raise ReleaseError("Checksum file must bind the archive and external manifest")
    for path in (archive, manifest_path):
        if sha256(path.read_bytes()).hexdigest() != checksums[path.name]:
            raise ReleaseError(f"Artifact checksum mismatch: {path.name}")
    if receipt.get("archive") != {"name": archive.name, "sha256": checksums[archive.name]}:
        raise ReleaseError("Manifest archive identity does not match the archive")
    if extract_to is not None and os.path.lexists(extract_to):
        raise ReleaseError("Extraction requires a new destination directory")
    with tempfile.TemporaryDirectory(prefix="cwrelease-verify-") as temporary:
        root = Path(temporary) / "collaborative-work"
        root.mkdir()
        seen = set()
        try:
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    if not member.orig_filename.startswith("collaborative-work/"):
                        raise ReleaseError(f"Unexpected ZIP root: {member.filename}")
                    relative = _relative(member.orig_filename[len("collaborative-work/"):])
                    if relative in seen or relative not in files:
                        raise ReleaseError(f"Duplicate or unexpected ZIP member: {member.filename}")
                    if (member.is_dir() or stat.S_IFMT(member.external_attr >> 16) != stat.S_IFREG
                            or member.compress_type != zipfile.ZIP_STORED or member.flag_bits & 1):
                        raise ReleaseError(f"Non-regular or non-normalized ZIP member: {member.filename}")
                    content = package.read(member)
                    if sha256(content).hexdigest() != files[relative]:
                        raise ReleaseError(f"ZIP member checksum mismatch: {relative}")
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    seen.add(relative)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            if isinstance(error, ReleaseError):
                raise
            raise ReleaseError(f"Invalid ZIP archive: {error}") from error
        if seen != set(files):
            raise ReleaseError("ZIP members do not match the full release inventory")
        metadata = validate_source(root)
        if any(receipt.get(key) != value for key, value in metadata.items()):
            raise ReleaseError("Manifest metadata does not match the extracted plugin")
        if extract_to is not None:
            destination = Path(extract_to).absolute()
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(root, destination)
            except OSError as error:
                raise ReleaseError(f"Cannot extract to fresh destination {destination}: {error}") from error
    return {**metadata, "archive_sha256": checksums[archive.name],
            "extracted_to": str(Path(extract_to).absolute()) if extract_to is not None else None}
