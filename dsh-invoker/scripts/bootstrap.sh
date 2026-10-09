#!/usr/bin/env bash
# Read-only discovery works without Python or uv. setup is an explicit,
# post-consent operation; this script never decides whether consent was given.
# macOS Bash 3.2+ / Linux Bash. Never source this file into a user's shell.
set -uo pipefail

fail() {
    printf '{"ok":false,"error":"%s","message":"%s"}\n' "$1" "$2" >&2
    exit 1
}
usage() {
    printf '%s\n' 'Usage: bash bootstrap.sh check|setup|exec --project-root ABSOLUTE [-- PYTHON_ARGS...]' >&2
    exit 2
}
has_controls() { [[ "$1" == *[$'\001'-$'\037'$'\177']* ]]; }
json_string() {
    local s="$1"
    s=${s//\\/\\\\}; s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '"%s"' "$s"
}
# Lexical normalization also works when a candidate home does not exist yet.
normalize_path() {
    local value="$1" part result='' old_ifs="$IFS"
    local -a pieces stack
    IFS='/'; read -r -a pieces <<< "$value"; IFS="$old_ifs"
    stack=()
    for part in "${pieces[@]}"; do
        case "$part" in ''|.) ;; ..) ((${#stack[@]} == 0)) || unset 'stack[${#stack[@]}-1]' ;; *) stack[${#stack[@]}]="$part" ;; esac
    done
    for part in "${stack[@]}"; do result="$result/$part"; done
    printf '%s' "${result:-/}"
}
absolute_executable() {
    local p="$1" parent
    [[ "$p" = /* ]] || p="$START_DIR/$p"
    parent=$(cd -P -- "$(dirname -- "$p")" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s' "$parent" "${p##*/}"
}

[[ $# -ge 3 ]] || usage
ACTION="$1"; shift
case "$ACTION" in check|setup|exec) ;; *) usage ;; esac
PROJECT_ARG=''; PYTHON_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-root) [[ $# -ge 2 && -z "$PROJECT_ARG" ]] || usage; PROJECT_ARG="$2"; shift 2 ;;
        --) shift; PYTHON_ARGS=("$@"); break ;;
        *) usage ;;
    esac
done
[[ "$PROJECT_ARG" = /* && -d "$PROJECT_ARG" ]] || fail project_root 'An existing absolute project root is required.'
has_controls "$PROJECT_ARG" && fail project_root 'Control characters in paths are not supported.'
[[ "$ACTION" = exec || ${#PYTHON_ARGS[@]} = 0 ]] || usage
START_DIR=$(pwd -P) || fail project_root 'Cannot inspect the working directory.'
PROJECT_ROOT=$(cd -P -- "$PROJECT_ARG" 2>/dev/null && pwd -P) || fail project_root 'Cannot resolve the project root.'
SCRIPT_DIR=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || fail script_path 'Cannot resolve the skill scripts directory.'
# Tool and interpreter cwd is trusted skill code, never downloaded content.
cd -P -- "$SCRIPT_DIR" || fail script_path 'Cannot enter the skill scripts directory.'
STATE="${PROJECT_ROOT%/}/.dsh-invoker-profile"
PYTHON="$STATE/venv/bin/python"
MARKER='dsh-invoker-bootstrap-v1'
LOCK_HELD=0

plain_directory() { [[ ! -L "$1" && ( ! -e "$1" || -d "$1" ) ]]; }
plain_file() { [[ ! -L "$1" && ( ! -e "$1" || -f "$1" ) ]]; }
check_state_paths() {
    local name
    plain_directory "$STATE" || fail unsafe_path 'The project runtime directory is a link or is not a directory.'
    for name in bin python python-downloads python-bin tools tool-bin cache credentials tmp staging venv; do
        plain_directory "$STATE/$name" || fail unsafe_path 'A runtime directory is a link or has an unexpected type.'
    done
    for name in .bootstrap-owner runtime.json binding.json; do
        plain_file "$STATE/$name" || fail unsafe_path 'Runtime metadata must be regular files, not links.'
    done
    plain_directory "$STATE/.bootstrap-lock" || fail unsafe_path 'The runtime lock has an unexpected type.'
}
owned_state() {
    local owner=''
    [[ -f "$STATE/.bootstrap-owner" && ! -L "$STATE/.bootstrap-owner" ]] || return 1
    IFS= read -r owner < "$STATE/.bootstrap-owner" || [[ -n "$owner" ]] || return 1
    [[ "$owner" = "$MARKER" ]]
}

OS=$(uname -s 2>/dev/null) || OS=unknown
ARCH=$(uname -m 2>/dev/null) || ARCH=unknown
PLATFORM=unsupported; SUPPORTED=false
CONDITIONS='Requires macOS 14+ arm64/x86_64 or Linux glibc 2.28+ aarch64/x86_64; Windows uses bootstrap.ps1 with pwsh 7.2+.'
case "$OS:$ARCH" in
    Darwin:arm64|Darwin:aarch64) PLATFORM=macos-arm64 ;;
    Darwin:x86_64) PLATFORM=macos-x86_64 ;;
    Linux:aarch64|Linux:arm64) PLATFORM=linux-aarch64 ;;
    Linux:x86_64|Linux:amd64) PLATFORM=linux-x86_64 ;;
esac
case "$PLATFORM" in
    macos-*)
        OS_VERSION=$(sw_vers -productVersion 2>/dev/null) || OS_VERSION=''
        MAJOR=${OS_VERSION%%.*}
        [[ "$MAJOR" =~ ^[0-9]+$ ]] && (( MAJOR >= 14 )) && SUPPORTED=true
        ;;
    linux-*)
        LIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null) || LIBC=''
        if [[ "$LIBC" =~ ^glibc[[:space:]]+([0-9]+)\.([0-9]+) ]]; then
            (( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 28) )) && SUPPORTED=true
        fi
        ;;
esac

UV=''; UV_SOURCE=absent; UV_SHA=''; UV_VERSION=''
PATH_UV=$(type -P uv 2>/dev/null) || PATH_UV=''
if [[ -n "$PATH_UV" && -f "$PATH_UV" && -x "$PATH_UV" ]]; then
    UV=$(absolute_executable "$PATH_UV") || UV=''
    [[ -z "$UV" ]] || UV_SOURCE=path
elif owned_state && [[ ! -L "$STATE/bin" && ! -L "$STATE/bin/uv" && -f "$STATE/bin/uv" && -x "$STATE/bin/uv" ]]; then
    UV="$STATE/bin/uv"; UV_SOURCE=project
fi
has_controls "$UV" && fail unsafe_path 'Control characters in executable paths are not supported.'

report_candidate() {
    local source="$1" home="$2" entry name first=1
    printf '{"source":'; json_string "$source"; printf ',"home":'; json_string "$home"
    printf ',"exists":'; [[ -d "$home" ]] && printf true || printf false
    printf ',"profiles_dir_exists":'; [[ -d "$home/profiles" ]] && printf true || printf false
    printf ',"profiles":['
    # Only names and existence; no config, credentials, dsh command or SDK import.
    while IFS= read -r -d '' entry; do
        [[ -d "$entry" ]] || continue
        name=${entry##*/}; has_controls "$name" && continue
        [[ $first = 1 ]] || printf ','; first=0; json_string "$name"
    done < <(shopt -s nullglob dotglob; for entry in "$home/profiles"/*; do [[ -d "$entry" ]] && printf '%s\0' "$entry"; done)
    printf ']}'
}
if [[ "$ACTION" = check ]]; then
    check_state_paths
    printf '{"schema_version":1,"action":"check","project_root":'; json_string "$PROJECT_ROOT"
    printf ',"state_dir":'; json_string "$STATE"; printf ',"platform":'; json_string "$PLATFORM"
    printf ',"supported":%s,"conditions":' "$SUPPORTED"; json_string "$CONDITIONS"
    printf ',"uv":{"available":'; [[ -n "$UV" ]] && printf true || printf false
    printf ',"source":'; json_string "$UV_SOURCE"; printf ',"path":'; [[ -n "$UV" ]] && json_string "$UV" || printf null
    printf ',"version":null},"runtime":{"manifest_exists":'; [[ -f "$STATE/runtime.json" ]] && printf true || printf false
    printf ',"python_exists":'; [[ -f "$PYTHON" ]] && printf true || printf false
    printf ',"python_path":'; json_string "$PYTHON"; printf ',"status":'
    [[ -f "$STATE/runtime.json" && -f "$PYTHON" ]] && json_string unverified || json_string missing
    printf '},"home_candidates":['
    FIRST=1; ENV_HOME=${DSH_HOME:-}; DEFAULT_HOME=${HOME:-}
    if [[ -n "${ENV_HOME//[$' \t\r\n']/}" ]] && ! has_controls "$ENV_HOME"; then
        [[ "$ENV_HOME" = /* ]] || ENV_HOME="$PROJECT_ROOT/$ENV_HOME"
        ENV_HOME=$(normalize_path "$ENV_HOME")
        report_candidate DSH_HOME "$ENV_HOME"; FIRST=0
    fi
    if [[ "$DEFAULT_HOME" = /* ]] && ! has_controls "$DEFAULT_HOME"; then
        DEFAULT_HOME=$(normalize_path "${DEFAULT_HOME%/}/.dsh")
        [[ $FIRST = 1 ]] || printf ','
        report_candidate default "$DEFAULT_HOME"
    fi
    printf ']}\n'
    exit 0
fi

[[ "$SUPPORTED" = true ]] || fail unsupported_platform 'This pinned DSH runtime requires macOS 14+ or Linux glibc 2.28+ on arm64/x86_64.'
check_state_paths
# Cleanup only a lock this invocation created. Failed setup is not automatically
# retried or erased: the marker distinguishes it from a compatible ready runtime.
cleanup() { [[ $LOCK_HELD = 0 ]] || rmdir -- "$STATE/.bootstrap-lock" 2>/dev/null || :; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sanitize_environment() {
    local key
    while IFS= read -r key; do
        case "$key" in UV_*|PIP_*|CONDA*|_CE_CONDA|_CE_M|VIRTUAL_ENV*|PYTHON*|__PYVENV_LAUNCHER__|NETRC) unset "$key" ;; esac
    done < <(compgen -e)
    export UV_NO_CONFIG=1 UV_NO_PROGRESS=1 UV_KEYRING_PROVIDER=disabled
    export UV_CACHE_DIR="$STATE/cache" UV_PYTHON_INSTALL_DIR="$STATE/python"
    export UV_PYTHON_CACHE_DIR="$STATE/python-downloads" UV_PYTHON_BIN_DIR="$STATE/python-bin"
    export UV_TOOL_DIR="$STATE/tools" UV_TOOL_BIN_DIR="$STATE/tool-bin" UV_CREDENTIALS_DIR="$STATE/credentials"
    export TMPDIR="$STATE/tmp" TMP="$STATE/tmp" TEMP="$STATE/tmp" PYTHONDONTWRITEBYTECODE=1
    # HTTP(S)_PROXY, ALL_PROXY, NO_PROXY and normal SSL/CURL certificate settings
    # are preserved. Never print environment values or raw uv/curl diagnostics.
}
resolve_runtime_path() {
    local target="$1" parent link hops=0
    while :; do
        parent=$(cd -P -- "${target%/*}" 2>/dev/null && pwd -P) || fail unsafe_path 'A runtime link is dangling or cyclic.'
        target="$parent/${target##*/}"
        [[ -L "$target" ]] || break
        ((hops+=1)); ((hops <= 40)) || fail unsafe_path 'A runtime link has a cyclic or excessive target chain.'
        link=$(readlink -- "$target" 2>/dev/null) || fail unsafe_path 'Cannot resolve a runtime link.'
        has_controls "$link" && fail unsafe_path 'Control characters in runtime link targets are not supported.'
        [[ "$link" = /* ]] && target="$link" || target="$parent/$link"
    done
    [[ -e "$target" ]] || fail unsafe_path 'A runtime link is dangling or cyclic.'
    if [[ -d "$target" ]]; then
        target=$(cd -P -- "$target" 2>/dev/null && pwd -P) || fail unsafe_path 'Cannot resolve a runtime directory link.'
    fi
    [[ "$target" = "$STATE" || "$target" = "$STATE/"* ]] || fail unsafe_path 'A runtime link escapes the project toolchain directory.'
    RESOLVED_RUNTIME_PATH="$target"
}
check_runtime_links() (
    # Native preflight, BEFORE Python/site startup. Walk import/startup roots,
    # not the cache. Follow internal directory aliases once so they cannot hide
    # another escaping link; normal files need no subprocess or canonicalization.
    local directory entry key visited=$'\n' index=0
    local -a pending
    pending=("$@")
    shopt -s nullglob dotglob
    while (( index < ${#pending[@]} )); do
        directory="${pending[index]}"; ((index+=1))
        key="${directory#"$STATE"}"
        [[ "$visited" != *$'\n'"$key"$'\n'* ]] || continue
        visited="$visited$key"$'\n'
        [[ -d "$directory" && -r "$directory" && -x "$directory" ]] || fail unsafe_path 'Cannot inspect a runtime startup directory.'
        for entry in "$directory"/*; do
            has_controls "$entry" && fail unsafe_path 'Control characters in runtime paths are not supported.'
            if [[ -L "$entry" ]]; then
                resolve_runtime_path "$entry"
                entry="$RESOLVED_RUNTIME_PATH"
            fi
            [[ ! -d "$entry" ]] || pending[${#pending[@]}]="$entry"
        done
    done
)
safe_python() {
    [[ -f "$PYTHON" && -x "$PYTHON" ]] || fail runtime_missing 'Run setup after approving the project-only toolchain installation.'
    resolve_runtime_path "$PYTHON"
    [[ "$RESOLVED_RUNTIME_PATH" = "$STATE/python/"* && -f "$RESOLVED_RUNTIME_PATH" && -x "$RESOLVED_RUNTIME_PATH" ]] || fail unsafe_path 'The venv interpreter must belong to this project managed Python directory.'
}

if [[ "$ACTION" = exec || -e "$STATE/runtime.json" ]]; then
    owned_state || fail unowned_runtime 'Runtime ownership is missing; existing files will not be overwritten.'
    [[ -f "$STATE/runtime.json" ]] || fail runtime_missing 'No ready runtime manifest exists. Approve setup first.'
    [[ ! -e "$STATE/.bootstrap-lock" ]] || fail runtime_busy 'Another setup is active or an interrupted setup lock needs inspection.'
    check_runtime_links "$STATE/python" "$STATE/venv" || exit 1
    safe_python
    sanitize_environment
    if [[ "$ACTION" = exec ]]; then
        [[ ${#PYTHON_ARGS[@]} -gt 0 ]] || usage
        "$PYTHON" -I -B "$SCRIPT_DIR/bootstrap_support.py" validate --quiet --project-root "$PROJECT_ROOT" || exit 1
        exec "$PYTHON" -I -B "${PYTHON_ARGS[@]}"
    else
        "$PYTHON" -I -B "$SCRIPT_DIR/bootstrap_support.py" validate --project-root "$PROJECT_ROOT"
        exit $?
    fi
fi

# Do not claim arbitrary preexisting Python/cache/bin trees as this skill's own.
if [[ -d "$STATE" ]]; then
    owned_state && fail incomplete_runtime 'An incomplete owned setup exists. Inspect it and explicitly remove only its technical files before retrying; preserve binding.json.'
    # Binding, dev-tests and CI fixtures may coexist in the state root. Claim
    # only bootstrap-owned target paths, never the entire state directory.
    for NAME in bin python python-downloads python-bin tools tool-bin cache credentials tmp staging venv .bootstrap-owner runtime.json; do
        [[ ! -e "$STATE/$NAME" && ! -L "$STATE/$NAME" ]] || fail unowned_runtime 'A toolchain target already exists. Inspect it; setup will not replace it.'
    done
fi
umask 077
mkdir -p -- "$STATE" || fail filesystem 'Cannot create the project runtime directory.'
mkdir -- "$STATE/.bootstrap-lock" 2>/dev/null || fail runtime_busy 'Another setup is active or a setup lock needs inspection.'
LOCK_HELD=1
(set -C; printf '%s\n' "$MARKER" > "$STATE/.bootstrap-owner") 2>/dev/null || fail unowned_runtime 'Cannot claim runtime ownership without replacing existing files.'
for NAME in bin python python-downloads python-bin tools tool-bin cache credentials tmp staging; do
    mkdir -- "$STATE/$NAME" || fail filesystem 'Cannot create a private toolchain directory.'
done
sanitize_environment

load_asset() {
    local line prefix key middle asset between digest rest found=0 version_ok=0
    ASSET=''; EXPECTED=''
    [[ -f "$SCRIPT_DIR/../assets/uv-release.json" ]] || fail release_metadata 'Pinned uv release metadata is missing.'
    while IFS= read -r line; do
        [[ "$line" = *'"version": "0.12.24"'* ]] && version_ok=1
        case "$line" in *\""$PLATFORM"\"*)
            IFS='"' read -r prefix key middle asset between digest rest <<< "$line"
            ASSET="$asset"; EXPECTED="$digest"; ((found+=1)) ;;
        esac
    done < "$SCRIPT_DIR/../assets/uv-release.json"
    [[ $version_ok = 1 && $found = 1 && "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] || fail release_metadata 'Invalid pinned uv release metadata.'
    case "$PLATFORM:$ASSET" in
        macos-arm64:uv-aarch64-apple-darwin.tar.gz|macos-x86_64:uv-x86_64-apple-darwin.tar.gz|linux-aarch64:uv-aarch64-unknown-linux-gnu.tar.gz|linux-x86_64:uv-x86_64-unknown-linux-gnu.tar.gz) ;;
        *) fail release_metadata 'The uv asset does not match this platform.' ;;
    esac
}
sha256_file() {
    local output
    if type -P sha256sum >/dev/null 2>&1; then
        output=$(sha256sum -- "$1" 2>/dev/null) || return 1
    elif type -P shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 -- "$1" 2>/dev/null) || return 1
    else
        fail hash_tool 'A SHA-256 tool (sha256sum or shasum) is required when uv is absent.'
    fi
    printf '%s' "${output%% *}"
}
install_local_uv() {
    local operation archive extract root names types entry name_count=0 type_count=0 uv_count=0 root_count=0 uvx_count=0 uvw_count=0 actual
    load_asset
    for TOOL in curl tar mktemp cp chmod; do
        type -P "$TOOL" >/dev/null 2>&1 || fail bootstrap_tool 'curl, tar, mktemp, cp, chmod and a SHA-256 tool are needed to bootstrap uv.'
    done
    operation=$(mktemp -d "$STATE/staging/uv.XXXXXXXX" 2>/dev/null) || fail filesystem 'Cannot create an isolated download operation.'
    mkdir -- "$operation/download" "$operation/extract" || fail filesystem 'Cannot create isolated download and extraction directories.'
    archive="$operation/download/$ASSET"; extract="$operation/extract"
    curl --disable --fail --silent --location --proto '=https' --proto-redir '=https' --connect-timeout 30 --max-time 600 \
        --output "$archive" "https://github.com/astral-sh/uv/releases/download/0.12.24/$ASSET" 2>/dev/null \
        || fail uv_download 'Pinned uv download failed; check connectivity/proxy/certificates in your terminal. Diagnostics are withheld.'
    actual=$(sha256_file "$archive") || fail uv_digest 'Cannot calculate the uv archive digest.'
    [[ "$actual" = "$EXPECTED" ]] || fail uv_digest 'The uv archive SHA-256 does not match the pinned official release.'
    root=${ASSET%.tar.gz}
    names=$(tar -tzf "$archive" 2>/dev/null) || fail uv_archive 'Cannot inspect the verified uv archive.'
    types=$(tar -tvzf "$archive" 2>/dev/null) || fail uv_archive 'Cannot inspect uv archive entry types.'
    # Whitelist the entire official archive, not just the path we want to copy.
    # Refuse links, special files, traversal, duplicates and unexpected entries.
    while IFS= read -r entry; do
        ((name_count+=1))
        case "$entry" in
            "$root"|"$root/") ((root_count+=1)) ;;
            "$root/uv") ((uv_count+=1)) ;;
            "$root/uvx") ((uvx_count+=1)) ;;
            "$root/uvw") ((uvw_count+=1)) ;;
            *) fail uv_archive 'The uv archive contains an unexpected or unsafe entry.' ;;
        esac
    done <<< "$names"
    while IFS= read -r entry; do
        ((type_count+=1))
        case "${entry:0:1}" in -|d) ;; *) fail uv_archive 'Links or special entries are not allowed in the uv archive.' ;; esac
    done <<< "$types"
    [[ $name_count = "$type_count" && $uv_count = 1 && $root_count -le 1 && $uvx_count -le 1 && $uvw_count -le 1 ]] || fail uv_archive 'Duplicate or malformed uv archive entries were found.'
    # Extract only one validated regular entry to a literal new file. No general
    # tar extraction can write archive-selected filenames or traverse directories.
    (set -C; tar -xOzf "$archive" "$root/uv" > "$extract/uv") 2>/dev/null || fail uv_archive 'Cannot extract the validated uv executable.'
    [[ -s "$extract/uv" && ! -L "$extract/uv" && ! -e "$STATE/bin/uv" ]] || fail uv_archive 'The uv executable is empty or would replace an existing file.'
    cp -p -- "$extract/uv" "$STATE/bin/uv" 2>/dev/null || fail filesystem 'Cannot copy uv into the private project bin directory.'
    chmod 700 -- "$STATE/bin/uv" 2>/dev/null || fail filesystem 'Cannot make the project uv executable runnable.'
    UV="$STATE/bin/uv"; UV_SOURCE=project; UV_SHA=$(sha256_file "$UV") || fail uv_digest 'Cannot verify the project uv executable.'
    # Known files only. Failed setups retain their private staging data for local
    # inspection, without ever executing from a download/extract directory.
    rm -- "$archive" "$extract/uv" 2>/dev/null || :
    rmdir -- "$operation/download" "$operation/extract" "$operation" 2>/dev/null || :
}
[[ -n "$UV" ]] || install_local_uv

uv_call() (
    # --no-config does not disable .netrc or user auth stores. Only uv receives
    # this private empty user-config home; SDK execution retains the real home.
    export HOME="$STATE/credentials" USERPROFILE="$STATE/credentials" NETRC="$STATE/credentials/no-netrc"
    export XDG_CONFIG_HOME="$STATE/credentials" XDG_DATA_HOME="$STATE/credentials"
    export APPDATA="$STATE/credentials" LOCALAPPDATA="$STATE/credentials"
    "$UV" --no-config --cache-dir "$STATE/cache" "$@"
)
uv_probe() { uv_call "$@" 2>/dev/null; }
VERSION_OUTPUT=$(uv_probe --version) || fail uv_version 'Cannot run the selected uv. No update or fallback was attempted.'
if [[ "$VERSION_OUTPUT" =~ ^uv[[:space:]]+([0-9]+)\.([0-9]+)\.([0-9]+)([[:space:]]|$) ]]; then
    UV_VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
    (( BASH_REMATCH[1] > 0 || BASH_REMATCH[2] > 9 || (BASH_REMATCH[2] == 9 && BASH_REMATCH[3] >= 20) )) \
        || fail uv_version 'uv 0.9.20 or newer with the required flags is needed. Update it yourself if desired; this skill never updates existing uv.'
else
    fail uv_version 'The selected uv version is not recognized. Existing uv will not be replaced.'
fi
for COMMAND in python venv pip; do
    case "$COMMAND" in
        python) HELP=$(uv_probe python install --help) || fail uv_capability 'uv python install help failed.'; FLAGS=(--install-dir --no-bin --no-registry) ;;
        venv) HELP=$(uv_probe venv --help) || fail uv_capability 'uv venv help failed.'; FLAGS=(--no-project --managed-python --no-python-downloads) ;;
        pip) HELP=$(uv_probe pip install --help) || fail uv_capability 'uv pip install help failed.'; FLAGS=(--python --no-python-downloads --only-binary --link-mode --prerelease --default-index --keyring-provider) ;;
    esac
    for FLAG in "${FLAGS[@]}"; do
        [[ "$HELP" = *"$FLAG"* ]] || fail uv_capability 'The selected uv lacks a required isolation flag. No update or alternate uv was attempted.'
    done
done
uv_step() { uv_call "$@" >/dev/null 2>&1; }
uv_step python install --install-dir "$STATE/python" --no-bin --no-registry 3.12 \
    || fail python_install 'Project-only managed Python installation failed; no global environment was changed. Inspect connectivity/platform support locally.'
check_state_paths
check_runtime_links "$STATE/python" || exit 1
[[ ! -e "$STATE/venv" && ! -L "$STATE/venv" ]] || fail existing_venv 'An existing environment will not be replaced.'
uv_step venv --no-project --managed-python --no-python-downloads --python 3.12 "$STATE/venv" \
    || fail venv_create 'Project venv creation failed; incomplete owned files are left for inspection.'
check_state_paths
check_runtime_links "$STATE/python" "$STATE/venv" || exit 1
safe_python
"$PYTHON" -I -B "$SCRIPT_DIR/bootstrap_support.py" check-python --project-root "$PROJECT_ROOT" || exit 1
uv_step pip install --python "$PYTHON" --no-python-downloads --link-mode copy --only-binary :all: --prerelease explicit \
    --default-index https://pypi.org/simple --keyring-provider disabled --no-sources \
    'deepseek-harness-sdk==0.1.5rc1' 'deepseek-harness-runtime-bin==0.1.5rc1' \
    || fail sdk_install 'Pinned SDK/runtime wheel installation failed; no source builds, global pip or existing environment fallback was attempted.'
check_state_paths
check_runtime_links "$STATE/python" "$STATE/venv" || exit 1
safe_python
"$PYTHON" -I -B "$SCRIPT_DIR/bootstrap_support.py" record --project-root "$PROJECT_ROOT" \
    --platform "$PLATFORM" --uv-path "$UV" --uv-version "$UV_VERSION" --uv-source "$UV_SOURCE" --uv-sha256 "$UV_SHA"
exit $?
