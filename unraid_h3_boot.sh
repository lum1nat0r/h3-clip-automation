#!/bin/bash
# Unraid cgroup-v2 startup guard. No model loading or rendering.
# Install before emhttp in /boot/config/go; ordinary autostart and Docker
# restart policies for these two containers must be disabled.
set -euo pipefail
mode=${1:-check-kernel}
base=/sys/fs/cgroup
group=$base/h3-automatik
budget=25769803776
# Edit these names to match your own containers before installing.
comfy=YOUR_COMFYUI_CONTAINER
ollama=YOUR_OLLAMA_CONTAINER
die() { logger -t h3-memory "STOP: $*"; printf 'STOP: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run on the Unraid host as root.'
[[ $mode == install || $mode == start || $mode == check-kernel ]] || die 'Expected install, start or check-kernel.'
check_kernel() {
    [[ -f $group/memory.max && $(cat "$group/memory.max") == "$budget" ]] || die 'Shared 24-GiB ceiling missing.'
    [[ -f $group/memory.swap.max && $(cat "$group/memory.swap.max") == 0 ]] || die 'Shared no-swap ceiling missing.'
    [[ " $(cat "$group/cgroup.subtree_control") " == *' memory '* ]] || die 'Child memory accounting missing.'
}
if [[ $mode == install ]]; then
    [[ -f $base/cgroup.controllers ]] || die 'This boot guard requires verified cgroup v2.'
    [[ " $(cat "$base/cgroup.controllers") " == *' memory '* ]] || die 'Host memory controller missing.'
    printf '+memory\n' > "$base/cgroup.subtree_control"
    if [[ -d $group ]]; then
        # An existing populated group is accepted only if already protected.
        if [[ -n $(find "$group" -mindepth 1 -type d -print -quit) || -n $(cat "$group/cgroup.procs") ]]; then
            check_kernel
        fi
    fi
    mkdir -p "$group"
    printf '%s\n' "$budget" > "$group/memory.max"
    printf '0\n' > "$group/memory.swap.max"
    printf '+memory\n' > "$group/cgroup.subtree_control"
    # Keep the shared parent from being removed as an empty ancestor when
    # Docker cleans up the last stopped/failed container's cgroup.
    mkdir -p "$group/h3-budget-anchor"
    check_kernel
    hook=/usr/local/emhttp/plugins/dynamix/event/docker_started/h3_memory_start
    [[ -d ${hook%/*} ]] || die 'Unraid docker_started event directory missing.'
    cat > "$hook" <<'HOOK'
#!/bin/bash
/bin/bash /boot/config/h3/unraid_h3_boot.sh start 2>&1 | /usr/bin/logger -t h3-memory
exit "${PIPESTATUS[0]}"
HOOK
    chmod 700 "$hook"
    logger -t h3-memory 'Installed: shared 24-GiB ceiling and guarded Docker startup.'
    printf 'PASS: Shared limit prepared; guarded docker_started hook installed.\n'
    exit 0
fi
check_kernel
[[ $mode != check-kernel ]] || { printf 'PASS: Shared 24-GiB ceiling and zero swap.\n'; exit 0; }
[[ $comfy != YOUR_COMFYUI_CONTAINER && $ollama != YOUR_OLLAMA_CONTAINER ]] || die 'Configure the two container names in this script first.'
[[ $(docker info --format '{{.CgroupDriver}}/{{.CgroupVersion}}') == cgroupfs/2 ]] || die 'Unexpected Docker cgroup configuration.'
for name in "$comfy" "$ollama"; do
    expected=$budget
    [[ $name != "$ollama" ]] || expected=17179869184
    read -r actual actual_swap actual_parent restart < <(docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.CgroupParent}} {{.HostConfig.RestartPolicy.Name}}' "$name")
    [[ $actual == "$expected" && $actual_parent == /h3-automatik && $restart == no ]] || die "$name settings changed; refusing automatic start."
    if [[ $actual_swap != "$expected" ]]; then
        # Some Docker builds report SwapLimit=false and rewrite MemorySwap to
        # -1. Accept only with no host swap AND the already checked parent=0.
        [[ $actual_swap == -1 && $(docker info --format '{{.SwapLimit}}') == false && $(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) == 0 ]] || die "$name swap protection cannot be verified."
    fi
done
for setting in OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1; do
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$ollama" | grep -qx "$setting" || die "Missing $setting."
done
for name in "$comfy" "$ollama"; do
    docker start "$name" >/dev/null
done
/bin/bash /boot/config/h3/unraid_memory_budget.sh verify "$comfy" "$ollama"
logger -t h3-memory 'PASS: Both services started with verified shared and individual limits.'
