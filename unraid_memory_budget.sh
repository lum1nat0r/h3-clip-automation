#!/bin/bash
# Run on the Unraid HOST as root, not inside a container.
# Creates only the named H3 memory cgroup. Never stops/restarts containers.
# Usage: bash unraid_memory_budget.sh prepare|verify COMFY_CONTAINER OLLAMA_CONTAINER
set -euo pipefail
mode=${1:-help}
comfy=${2:-}
ollama=${3:-}
budget=25769803776 # 24 GiB shared by BOTH containers
ollama_budget=17179869184 # 16 GiB individual ceiling
parent=/h3-automatik
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
[[ $mode == prepare || $mode == verify ]] || die 'Usage: bash unraid_memory_budget.sh prepare|verify COMFY_CONTAINER OLLAMA_CONTAINER'
[[ $EUID -eq 0 ]] || die 'Run as root on the Unraid host.'
for name in "$comfy" "$ollama"; do
    [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die 'Pass the two real container names.'
    docker inspect --format '{{.Id}}' "$name" >/dev/null
done
[[ $comfy != "$ollama" ]] || die 'Two different containers are required.'
driver=$(docker info --format '{{.CgroupDriver}}')
version=$(docker info --format '{{.CgroupVersion}}')
[[ $driver == cgroupfs ]] || die "Detected cgroup driver $driver; this Unraid helper supports cgroupfs only. Do not change the Docker driver."
if [[ $version == 2 ]]; then
    base=/sys/fs/cgroup
    [[ -f $base/cgroup.subtree_control ]] || die 'Host cgroup hierarchy is not accessible.'
    [[ " $(cat "$base/cgroup.subtree_control") " == *' memory '* ]] || die 'Root memory controller is not enabled; host configuration must be checked.'
    group=$base$parent
    limit=$group/memory.max
    swap=$group/memory.swap.max
    expected_swap=0
elif [[ $version == 1 ]]; then
    base=/sys/fs/cgroup/memory
    [[ -f $base/memory.limit_in_bytes ]] || die 'Host memory cgroup is not accessible.'
    group=$base$parent
    limit=$group/memory.limit_in_bytes
    swap=$group/memory.memsw.limit_in_bytes
    expected_swap=$budget
else
    die "Unsupported cgroup version: $version"
fi

if [[ $mode == prepare ]]; then
    for name in "$comfy" "$ollama"; do
        [[ $(docker inspect --format '{{.State.Running}}' "$name") == false ]] || die "Stop $name first. No process will be killed by this helper."
    done
    # An existing group may contain other containers: never lower its budget
    # or change its settings while it has children/processes.
    if [[ -d $group ]]; then
        [[ -z $(find "$group" -mindepth 1 -type d -print -quit) ]] || die 'Group has children. Check that all members are stopped before preparing it.'
        [[ ! -f $group/cgroup.procs || -z $(cat "$group/cgroup.procs") ]] || die 'Group contains processes.'
    fi
    mkdir -p "$group"
    if [[ $version == 1 ]]; then
        printf '1\n' > "$group/memory.use_hierarchy"
    fi
    printf '%s\n' "$budget" > "$limit"
    if [[ -f $swap ]]; then
        printf '%s\n' "$expected_swap" > "$swap"
    else
        swap_total=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
        [[ $swap_total == 0 ]] || die 'Swap limit unavailable while host swap exists; no verified protection.'
    fi
    if [[ $version == 2 ]]; then
        printf '+memory\n' > "$group/cgroup.subtree_control"
    fi
    printf 'Prepared shared H3 budget: 24 GiB, cgroup v%s. Not persistent across reboot.\n' "$version"
    printf 'Now add the documented Extra Parameters to BOTH Unraid templates, start both containers, then run verify.\n'
    exit 0
fi

[[ -f $limit && $(cat "$limit") == "$budget" ]] || die 'Shared 24-GiB limit is missing or different.'
if [[ -f $swap ]]; then
    [[ $(cat "$swap") == "$expected_swap" ]] || die 'Shared swap ceiling is not enforced.'
else
    [[ $(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) == 0 ]] || die 'Swap cannot be verified.'
fi
if [[ $version == 1 ]]; then
    [[ $(cat "$group/memory.use_hierarchy") == 1 ]] || die 'Hierarchical accounting is not enabled.'
fi
for name in "$comfy" "$ollama"; do
    expected=$budget
    [[ $name != "$ollama" ]] || expected=$ollama_budget
    read -r actual actual_swap actual_parent < <(docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.CgroupParent}}' "$name")
    [[ $actual == "$expected" ]] || die "$name has a different RAM limit than the prepared template."
    if [[ $actual_swap != "$expected" ]]; then
        [[ $version == 2 && $actual_swap == -1 && $(docker info --format '{{.SwapLimit}}') == false && $(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) == 0 && $(cat "$swap") == 0 ]] || die "$name swap protection cannot be verified."
    fi
    [[ $actual_parent == "$parent" ]] || die "$name is not configured under $parent."
    pid=$(docker inspect --format '{{.State.Pid}}' "$name")
    [[ $pid =~ ^[1-9][0-9]*$ && -r /proc/$pid/cgroup ]] || die "$name must be running for membership verification."
    if [[ $version == 2 ]]; then
        actual_path=$(awk -F: '$1 == "0" {print $3}' "/proc/$pid/cgroup")
    else
        actual_path=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {print $3}' "/proc/$pid/cgroup")
    fi
    [[ $actual_path == "$parent/"* ]] || die "$name is not actually in the shared memory hierarchy."
    if [[ $version == 2 ]]; then
        [[ $(cat "$base$actual_path/memory.max") == "$expected" ]] || die "$name live kernel RAM limit differs."
    fi
    printf 'Verified %s: RAM=%s bytes, Docker MemorySwap=%s, group=%s\n' "$name" "$actual" "$actual_swap" "$actual_path"
done
printf 'PASS: Both containers share a verified 24-GiB RAM ceiling. No swap available to this group.\n'
printf 'Other containers, CPU load and GPU/driver faults remain outside this budget.\n'
