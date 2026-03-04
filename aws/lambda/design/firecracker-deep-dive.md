# Firecracker Deep Dive — MicroVM Architecture and Security

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Firecracker architecture, jailer, seccomp, cgroups, namespaces, snapshots, Lambda's use of Firecracker, multi-tenant isolation

---

## Table of Contents

1. [What Is Firecracker?](#1-what-is-firecracker)
2. [Architecture Overview](#2-architecture-overview)
3. [The MicroVM Model](#3-the-microvm-model)
4. [Device Model — Minimal by Design](#4-device-model--minimal-by-design)
5. [KVM Integration](#5-kvm-integration)
6. [The Jailer — Defense in Depth](#6-the-jailer--defense-in-depth)
7. [Seccomp Filters](#7-seccomp-filters)
8. [Cgroups and Resource Control](#8-cgroups-and-resource-control)
9. [Namespace Isolation](#9-namespace-isolation)
10. [Snapshots and Restore](#10-snapshots-and-restore)
11. [How Lambda Uses Firecracker](#11-how-lambda-uses-firecracker)
12. [Multi-Tenant Security Model](#12-multi-tenant-security-model)
13. [Performance Characteristics](#13-performance-characteristics)
14. [Comparison: Firecracker vs Containers vs Traditional VMs](#14-comparison-firecracker-vs-containers-vs-traditional-vms)
15. [Production Host Hardening](#15-production-host-hardening)
16. [Design Decisions and Trade-offs](#16-design-decisions-and-trade-offs)
17. [Interview Angles](#17-interview-angles)

---

## 1. What Is Firecracker?

Firecracker is an **open-source Virtual Machine Monitor (VMM)** purpose-built for creating and managing secure, multi-tenant serverless workloads. It creates lightweight virtual machines called **microVMs** using Linux KVM.

**Key facts:**
- Written in **Rust** (memory safety)
- Originated from Google's **crosvm** (Chrome OS VMM) but has diverged significantly
- Open-sourced by AWS in November 2018 (at re:Invent)
- Used in production by **AWS Lambda** and **AWS Fargate**
- Licensed under Apache 2.0

**Design philosophy:**
> Each Firecracker process encapsulates exactly one microVM. Guest code is considered malicious from inception.

---

## 2. Architecture Overview

### 2.1 Process Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HOST KERNEL (Linux + KVM)                     │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                   Firecracker Process                         │   │
│  │                   (one per microVM)                           │   │
│  │                                                               │   │
│  │  ┌────────────┐  ┌────────────┐  ┌─────────┐  ┌─────────┐  │   │
│  │  │ API Thread │  │ VMM Thread │  │ vCPU    │  │ vCPU    │  │   │
│  │  │            │  │            │  │ Thread 0│  │ Thread 1│  │   │
│  │  │ HTTP server│  │ Device     │  │         │  │         │  │   │
│  │  │ /config    │  │ emulation: │  │ KVM_RUN │  │ KVM_RUN │  │   │
│  │  │ /actions   │  │ - virtio   │  │ loop    │  │ loop    │  │   │
│  │  │ /machine   │  │   net      │  │         │  │         │  │   │
│  │  │            │  │ - virtio   │  │ Guest   │  │ Guest   │  │   │
│  │  │            │  │   block    │  │ code    │  │ code    │  │   │
│  │  │            │  │ - serial   │  │ runs    │  │ runs    │  │   │
│  │  │            │  │ - i8042    │  │ here    │  │ here    │  │   │
│  │  └────────────┘  └────────────┘  └─────────┘  └─────────┘  │   │
│  │                                                               │   │
│  │  Seccomp filters applied per thread                           │   │
│  │  Chroot + namespace isolation via Jailer                      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Thread Model

| Thread | Count | Role | Executes Guest Code? |
|--------|-------|------|---------------------|
| API Thread | 1 | HTTP server for control plane | No |
| VMM Thread | 1 | Device emulation (virtio) | No |
| vCPU Threads | 1–32 | KVM_RUN loop executing guest code | Yes |

**Critical design**: The API thread **never** executes guest code. This separates the control plane from the data plane within a single process.

---

## 3. The MicroVM Model

### 3.1 What Makes It "Micro"?

Compared to a full virtual machine (QEMU/KVM):

| Aspect | QEMU/KVM (Full VM) | Firecracker (MicroVM) |
|--------|--------------------|-----------------------|
| Device model | ~100+ emulated devices | **5 devices** |
| Memory overhead | ~100–300 MB | **< 5 MB** |
| Boot time | 5–30 seconds | **< 125 ms** |
| Code size | ~1.4M lines (QEMU) | ~50K lines (Firecracker) |
| Language | C | **Rust** |
| Attack surface | Large (many devices, protocols) | Minimal |
| Features | Live migration, GPU passthrough, USB, etc. | None of these |

### 3.2 MicroVM Configuration

A microVM is configured through Firecracker's REST API before boot:

```json
{
    "boot-source": {
        "kernel_image_path": "vmlinux",
        "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"
    },
    "drives": [
        {
            "drive_id": "rootfs",
            "path_on_host": "/path/to/rootfs.ext4",
            "is_root_device": true,
            "is_read_only": false
        }
    ],
    "machine-config": {
        "vcpu_count": 2,
        "mem_size_mib": 256
    },
    "network-interfaces": [
        {
            "iface_id": "eth0",
            "guest_mac": "AA:FC:00:00:00:01",
            "host_dev_name": "tap0"
        }
    ]
}
```

### 3.3 Guest Kernel

- Firecracker boots a **Linux kernel** provided by the operator
- Recommended: AWS-optimized guest kernel (stripped of unnecessary drivers)
- Boot arguments: `console=ttyS0 reboot=k panic=1 pci=off`
  - `pci=off`: No PCI bus (virtio-mmio instead)
  - `panic=1`: Reboot 1 second after kernel panic
  - `reboot=k`: Use keyboard controller for reboot signal

---

## 4. Device Model — Minimal by Design

### 4.1 The Five Devices

Firecracker exposes **only five** emulated devices:

| Device | Type | Purpose | Backend |
|--------|------|---------|---------|
| **virtio-net** | VirtIO | Network I/O | TAP device on host |
| **virtio-block** | VirtIO | Block storage | File on host |
| **virtio-vsock** | VirtIO | Host-guest communication | Unix domain socket |
| **Serial console** | 8250 UART | Logging / debugging | Firecracker stdout |
| **i8042 keyboard** | PS/2 | Reboot signaling only | KVM |

### 4.2 What's Deliberately Missing

| Missing Device | Present in QEMU | Why Excluded |
|----------------|-----------------|--------------|
| USB controller | Yes | Attack surface, not needed for serverless |
| GPU/display | Yes | No graphical output needed |
| Sound card | Yes | Not needed |
| IDE/SCSI | Yes | VirtIO-block is sufficient |
| ACPI | Partial | Minimal power management |
| PCI bus | Yes | VirtIO-mmio used instead (simpler) |
| Floppy drive | Yes | Obviously not needed |
| vfio/passthrough | Yes | Adds complexity and attack surface |

**Design principle**: Every additional device is an additional attack surface. Firecracker includes the absolute minimum.

### 4.3 I/O Rate Limiting

Each device supports token-bucket rate limiting with two independent buckets:

```
┌─────────────────────────────────────┐
│          Rate Limiter               │
│                                      │
│  ┌───────────────────────────┐      │
│  │ Operations/sec bucket     │      │
│  │ - size (burst)            │      │
│  │ - refill_time             │      │
│  │ - one_time_burst          │      │
│  └───────────────────────────┘      │
│                                      │
│  ┌───────────────────────────┐      │
│  │ Bandwidth bucket          │      │
│  │ - size (bytes/refill)     │      │
│  │ - refill_time             │      │
│  │ - one_time_burst          │      │
│  └───────────────────────────┘      │
│                                      │
│  Both must have tokens for I/O      │
│  to proceed                          │
└─────────────────────────────────────┘
```

**Use case in Lambda**: Rate limiters prevent a noisy-neighbor microVM from consuming disproportionate network or disk bandwidth on a shared host.

### 4.4 MMDS (MicroVM Metadata Service)

- Similar to EC2's Instance Metadata Service (IMDS)
- Host configures metadata via API, guest accesses via `http://169.254.169.254/` [INFERRED: or configurable IP]
- Used by Lambda to pass function configuration, credentials, etc. to the execution environment [INFERRED]
- Accessible via `/mmds` endpoint on the Firecracker API

---

## 5. KVM Integration

### 5.1 How Firecracker Uses KVM

```
┌──────────────────────────────────────────────────────┐
│                   USER SPACE                          │
│                                                       │
│  Firecracker (VMM)                                    │
│  ┌─────────────┐                                     │
│  │ vCPU Thread │                                     │
│  │             │◄──── VM Exit (I/O, interrupt, etc.) │
│  │  ┌────────┐ │                                     │
│  │  │KVM_RUN │ │────► VM Entry (resume guest)        │
│  │  │ ioctl  │ │                                     │
│  │  └────────┘ │                                     │
│  └──────┬──────┘                                     │
│         │                                             │
│         │ ioctl(KVM_RUN)                              │
│         ▼                                             │
├─────────────────────────────────────────────────────│
│                   KERNEL SPACE                        │
│                                                       │
│  KVM Module                                           │
│  ┌─────────────────────────────────────────────┐     │
│  │  Hardware virtualization (Intel VT-x / AMD-V)│     │
│  │  - Guest mode execution                       │     │
│  │  - Memory virtualization (EPT/NPT)           │     │
│  │  - Interrupt injection                        │     │
│  │  - VM Exit handling                           │     │
│  └─────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────┘
```

**The KVM_RUN loop:**
1. vCPU thread calls `ioctl(KVM_RUN)` — enters guest mode
2. Guest code executes directly on the CPU (hardware-accelerated)
3. VM Exit occurs (I/O request, interrupt, exception, etc.)
4. KVM returns control to Firecracker's vCPU thread
5. Firecracker handles the exit reason (e.g., forward I/O to VMM thread)
6. Loop back to step 1

### 5.2 CPU Templates

Firecracker controls which CPU features are exposed to the guest:
- **Static templates**: Pre-defined CPU feature sets
- **Custom templates**: User-defined via API
- Purpose: Ensure consistent CPU feature exposure for snapshot portability across different host CPU models

### 5.3 Clock Source

- Guest uses **kvm-clock** as the sole clock source
- No TSC passthrough by default (avoids timing side-channel attacks)
- ARM: Physical counter (CNPTCT) reset on VM boot (Linux 6.4+ with `KVM_CAP_COUNTER_OFFSET`)

---

## 6. The Jailer — Defense in Depth

### 6.1 What Is the Jailer?

The jailer is a companion program that sets up a restricted environment **before** launching Firecracker. It operates on the principle of defense in depth:

> "The jailer provides a second line of defense in case the virtualization barrier is ever compromised."

### 6.2 Jailer Execution Sequence

```
┌────────────────────────────────────────────────────────────────────┐
│                    Jailer Execution Flow                            │
│                                                                     │
│  1. VALIDATE                                                        │
│     ├── Verify all paths and VM ID                                  │
│     └── Close all FDs except stdin/stdout/stderr                    │
│                                                                     │
│  2. PURGE ENVIRONMENT                                               │
│     └── Remove all inherited environment variables                  │
│                                                                     │
│  3. CREATE JAIL DIRECTORY                                           │
│     └── <chroot_base>/<exec_file_name>/<id>/root/                  │
│                                                                     │
│  4. COPY FIRECRACKER BINARY                                        │
│     └── Into jail (prevents memory sharing with other instances)    │
│                                                                     │
│  5. SET UP CGROUPS                                                  │
│     ├── Create cgroup hierarchy: <base>/<parent>/<id>              │
│     ├── Write PID to tasks file                                     │
│     └── Apply controller values (cpu, memory, etc.)                │
│                                                                     │
│  6. CREATE DEVICE NODES                                             │
│     ├── /dev/kvm (major 10, minor 232)                             │
│     └── /dev/net/tun (major 10, minor 200)                         │
│                                                                     │
│  7. SET UP NAMESPACES                                               │
│     ├── Mount namespace (unshare + pivot_root + chroot)            │
│     ├── Network namespace (setns to pre-created netns)             │
│     └── PID namespace (optional, via clone + CLONE_NEWPID)         │
│                                                                     │
│  8. SET RESOURCE LIMITS                                             │
│     ├── fsize: max file size                                        │
│     └── no-file: max file descriptors (default 2048)               │
│                                                                     │
│  9. DROP PRIVILEGES                                                 │
│     ├── Set UID to unprivileged user                               │
│     └── Set GID to unprivileged group                              │
│                                                                     │
│  10. EXEC FIRECRACKER                                               │
│      └── exec("./firecracker", "--id=<id>", ...)                   │
│                                                                     │
└────────────────────────────────────────────────────────────────────┘
```

### 6.3 Jail File System Layout

```
/srv/jailer/firecracker/<vm-id>/root/
├── firecracker          (copied binary, owned by uid:gid)
├── dev/
│   ├── kvm              (character device, for KVM access)
│   └── net/
│       └── tun          (character device, for TAP networking)
├── [kernel image]       (hard-linked by operator)
├── [rootfs image]       (hard-linked by operator)
└── run/
    └── firecracker.socket   (API socket, created by Firecracker)
```

**Why copy the binary?** Prevents different microVM instances from sharing memory pages of the Firecracker binary, which could be a side-channel vector.

### 6.4 Privilege Model

```
Jailer starts as root (to set up cgroups, namespaces, mounts)
    │
    ▼
Sets up all privileged resources
    │
    ▼
Drops to unprivileged UID:GID
    │
    ▼
exec() into Firecracker (runs unprivileged)
    │
    ▼
Firecracker only sees: /dev/kvm, /dev/net/tun, rootfs, kernel
(everything else is invisible due to chroot)
```

### 6.5 Jailer Invocation Example

```bash
jailer \
    --id "vm-001" \
    --exec-file /usr/bin/firecracker \
    --uid 1000 \
    --gid 1000 \
    --cgroup-version 2 \
    --cgroup "cpu.max=100000 1000000" \
    --cgroup "memory.max=268435456" \
    --netns /var/run/netns/vm-001 \
    --resource-limit "no-file=4096" \
    --new-pid-ns \
    --daemonize
```

---

## 7. Seccomp Filters

### 7.1 What Are Seccomp Filters?

Seccomp (Secure Computing Mode) restricts which **system calls** a process can make. Firecracker uses seccomp-BPF (Berkeley Packet Filter) to allowlist only the system calls it needs.

### 7.2 How Firecracker Uses Seccomp

```
┌────────────────────────────────────────────────────────┐
│             Seccomp Filter Architecture                  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Thread-specific filters loaded BEFORE guest     │   │
│  │  code execution begins                            │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  API Thread Filter:                                      │
│  ┌────────────────────────────────────────────┐         │
│  │ Allow: epoll_*, accept, read, write, ...   │         │
│  │ Deny:  execve, fork, clone, ptrace, ...    │         │
│  └────────────────────────────────────────────┘         │
│                                                          │
│  VMM Thread Filter:                                      │
│  ┌────────────────────────────────────────────┐         │
│  │ Allow: ioctl(KVM_*), read, write,          │         │
│  │        epoll_*, mmap, ...                   │         │
│  │ Deny:  execve, fork, clone, ptrace, ...    │         │
│  └────────────────────────────────────────────┘         │
│                                                          │
│  vCPU Thread Filter:                                     │
│  ┌────────────────────────────────────────────┐         │
│  │ Allow: ioctl(KVM_RUN), read, write,        │         │
│  │        signal-related, ...                  │         │
│  │ Deny:  everything else                      │         │
│  └────────────────────────────────────────────┘         │
│                                                          │
│  Default: MOST RESTRICTIVE (recommended for production) │
└────────────────────────────────────────────────────────┘
```

### 7.3 Defense Purpose

Even if a guest escapes the VM (via a KVM vulnerability), the seccomp filter ensures the Firecracker process can only make a minimal set of system calls. This severely limits what an attacker can do:

| Without Seccomp | With Seccomp |
|-----------------|--------------|
| Could fork/exec new processes | Blocked |
| Could open arbitrary files | Blocked (limited to already-open FDs) |
| Could create network connections | Blocked (only pre-created TAP) |
| Could call ptrace to debug other processes | Blocked |
| Could load kernel modules | Blocked |

---

## 8. Cgroups and Resource Control

### 8.1 How Lambda Uses Cgroups [INFERRED]

```
cgroup hierarchy (per host):
├── /lambda/
│   ├── /vm-001/           (microVM 1 — Function A)
│   │   ├── cpu.max        100000 1000000  (10% of one core)
│   │   ├── memory.max     134217728       (128 MB)
│   │   ├── memory.swap.max 0              (no swap)
│   │   └── tasks          [Firecracker PID]
│   │
│   ├── /vm-002/           (microVM 2 — Function B)
│   │   ├── cpu.max        200000 1000000  (20% of one core)
│   │   ├── memory.max     268435456       (256 MB)
│   │   ├── memory.swap.max 0
│   │   └── tasks          [Firecracker PID]
│   │
│   └── /vm-003/           ...
```

### 8.2 Cgroup Controllers Used

| Controller | Purpose | Configuration |
|------------|---------|---------------|
| **cpu** | CPU time allocation | `cpu.shares`, `cpu.cfs_period_us`, `cpu.cfs_quota_us` |
| **cpuset** | Pin to specific CPU cores | Prevents cross-node migration |
| **memory** | Memory limit | `memory.limit_in_bytes`, `memory.memsw.limit_in_bytes` |
| **blkio** | Block I/O throttling | `blkio.throttle.io_serviced`, `blkio.throttle.io_service_bytes` |
| **cpuacct** | CPU usage monitoring | `cpuacct.usage_percpu` |

### 8.3 Why Cgroups Are Critical for Multi-Tenancy

Without cgroups, a microVM could:
- Consume all CPU on the host (denial of service to other tenants)
- Allocate memory until the host OOMs
- Saturate disk I/O, starving other microVMs
- Use swap space, leaving memory traces on disk

Cgroups enforce **hard limits** per microVM, ensuring fair resource allocation even when guests are malicious.

---

## 9. Namespace Isolation

### 9.1 Namespaces Used by Jailer

| Namespace | What It Isolates | How Jailer Uses It |
|-----------|-----------------|-------------------|
| **Mount** | File system view | `unshare()` + `pivot_root()` + `chroot()` — only jail directory visible |
| **Network** | Network stack | `setns()` to pre-created network namespace — isolated TAP device |
| **PID** (optional) | Process ID space | `clone(CLONE_NEWPID)` — Firecracker is PID 1 in its namespace |

### 9.2 Mount Namespace — Filesystem Isolation

```
Host filesystem:
/
├── bin/
├── etc/
├── usr/
├── var/
├── srv/
│   └── jailer/
│       └── firecracker/
│           └── vm-001/
│               └── root/      ◄── This becomes the ENTIRE filesystem
│                   ├── firecracker
│                   ├── dev/kvm
│                   ├── dev/net/tun
│                   ├── vmlinux
│                   └── rootfs.ext4

Guest's view (after chroot):
/
├── firecracker
├── dev/
│   ├── kvm
│   └── net/tun
├── vmlinux
└── rootfs.ext4
```

The guest cannot access anything outside the jail directory — not the host OS, not other microVMs' files, nothing.

### 9.3 Network Namespace

Each microVM gets its own network namespace with:
- An isolated **TAP device** for guest networking
- No access to the host's network interfaces
- Traffic passes through the TAP device to the host, where iptables/routing rules control forwarding

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  microVM     │     │  Network     │     │  Host        │
│  (guest)     │     │  Namespace   │     │  (default ns)│
│              │     │              │     │              │
│  eth0 ──────────► tap0 ──────────────► iptables ──► Internet
│  (virtio-net)│     │  (isolated)  │     │  / routing   │
└──────────────┘     └──────────────┘     └──────────────┘
```

### 9.4 PID Namespace

When `--new-pid-ns` is used:
- Firecracker becomes **PID 1** in its own PID namespace
- Cannot see or signal other processes on the host
- If Firecracker crashes, all its child processes are cleaned up automatically (init behavior)
- Prevents a compromised Firecracker from enumerating or attacking other processes

---

## 10. Snapshots and Restore

### 10.1 Snapshot Components

A Firecracker snapshot consists of three parts:

| Component | Contents | Size |
|-----------|----------|------|
| **Guest memory file** | Full or differential memory state | Proportional to memory size |
| **MicroVM state file** | Emulated hardware state + KVM state | Small (KBs) |
| **Disk files** | Managed externally (not part of snapshot) | User-managed |

### 10.2 Snapshot Types

**Full snapshot:**
- Contains the complete guest memory (all pages)
- Immediately restorable without dependencies
- Written synchronously during creation

**Diff snapshot:**
- Contains only memory pages modified since the last snapshot
- Smaller than full snapshots
- Must be merged with base snapshot for restoration
- Merge tool: `snapshot-editor edit-memory rebase --memory-path base --diff-path layer`

### 10.3 Snapshot/Restore Workflow

```
CREATION:
┌──────────────┐    Pause     ┌──────────────┐   CreateSnapshot   ┌────────────────┐
│  Running     │ ──────────► │  Paused      │ ──────────────────► │ Snapshot files  │
│  microVM     │              │  microVM     │                     │ on disk         │
└──────────────┘              └──────┬───────┘                     └────────────────┘
                                     │
                                  Resume
                                     │
                              ┌──────▼───────┐
                              │  Running     │  (original continues)
                              │  microVM     │
                              └──────────────┘

RESTORATION (different Firecracker process):
┌────────────────┐   LoadSnapshot   ┌──────────────┐   Resume    ┌──────────────┐
│ Snapshot files  │ ──────────────► │  Paused      │ ─────────► │  Running     │
│ on disk         │                  │  microVM     │             │  microVM     │
└────────────────┘                  └──────────────┘             └──────────────┘
```

### 10.4 Memory Loading — MAP_PRIVATE Optimization

Firecracker uses `MAP_PRIVATE` + `mmap` for memory restoration:

```
Traditional approach:
  Load entire memory file into RAM → Slow startup

Firecracker approach:
  mmap(MAP_PRIVATE) the memory file → Near-instant startup
  Pages loaded on demand (page fault → read from file)
  Copy-on-write for modifications
```

**Advantage**: Very fast snapshot loading times
**Trade-off**: The memory file must remain accessible for the entire lifetime of the restored microVM

### 10.5 How Lambda Uses Snapshots (SnapStart) [INFERRED]

```
PUBLISH TIME:
1. Lambda creates a new microVM
2. Runs function Init code (class loading, dependency injection, etc.)
3. Pauses the microVM
4. Creates a Firecracker snapshot (memory + state)
5. Encrypts and stores snapshot in internal storage

INVOKE TIME:
1. Load encrypted snapshot
2. Restore microVM from snapshot (MAP_PRIVATE for fast load)
3. Resume execution — Init phase is SKIPPED
4. Function handler runs immediately
5. Result: Cold start reduced from seconds to ~200 ms
```

### 10.6 Snapshot Security Considerations

**The uniqueness problem**: Restoring the same snapshot multiple times creates copies that share identical state:

| Shared State | Risk |
|-------------|------|
| Random number generator seeds | Predictable "random" numbers |
| TLS session IDs | Session reuse / hijacking |
| UUIDs generated during Init | Duplicate unique identifiers |
| Database connection IDs | Connection conflicts |
| In-memory caches | Stale data |

**Lambda's mitigation** (SnapStart hooks):
- `beforeCheckpoint()`: Called before snapshot — flush connections, clear caches
- `afterRestore()`: Called after restore — re-seed RNG, establish new connections
- Lambda provides a `RuntimeHooks` interface for Java to register these hooks

**Firecracker's position**: "Resuming identical snapshots multiple times creates security risks. Users must implement mechanisms ensuring uniqueness persists across restoration events."

### 10.7 Snapshot Limitations

| Limitation | Details |
|------------|---------|
| Network connections | Dropped on restore (vsock connections close, listen sockets survive) |
| Disk flush | Not explicitly flushed during snapshot |
| MMDS data | Not persisted in snapshot (config is, data store is not) |
| Metrics/logs config | Not saved |
| Cgroups v1 | High restoration latency (v2 recommended) |
| ARM | Cannot restore between different GIC versions |
| Huge pages | Dirty page tracking negates huge page benefits |

---

## 11. How Lambda Uses Firecracker

### 11.1 Lambda's Firecracker Deployment [INFERRED]

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Worker Host (Bare Metal)                         │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Host OS (Amazon Linux, minimal)                              │  │
│  │  KVM module loaded                                            │  │
│  │  Cgroups v2 hierarchy                                         │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ Jailer +     │  │ Jailer +     │  │ Jailer +     │  ...         │
│  │ Firecracker  │  │ Firecracker  │  │ Firecracker  │              │
│  │              │  │              │  │              │              │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │              │
│  │ │ microVM  │ │  │ │ microVM  │ │  │ │ microVM  │ │              │
│  │ │          │ │  │ │          │ │  │ │          │ │              │
│  │ │ Python   │ │  │ │ Node.js  │ │  │ │ Java     │ │              │
│  │ │ 3.12     │ │  │ │ 20.x     │ │  │ │ 21       │ │              │
│  │ │ runtime  │ │  │ │ runtime  │ │  │ │ runtime  │ │              │
│  │ │          │ │  │ │          │ │  │ │          │ │              │
│  │ │ Customer │ │  │ │ Customer │ │  │ │ Customer │ │              │
│  │ │ Function │ │  │ │ Function │ │  │ │ Function │ │              │
│  │ │ Code     │ │  │ │ Code     │ │  │ │ Code     │ │              │
│  │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │              │
│  │              │  │              │  │              │              │
│  │ Tenant A    │  │ Tenant B    │  │ Tenant C    │              │
│  └──────────────┘  └──────────────┘  └──────────────┘              │
│                                                                      │
│  150+ microVMs per host (depending on memory configuration)         │
│  ~5 MB VMM overhead per microVM                                     │
│  150+ microVMs/sec creation rate                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.2 Lambda Execution Environment ↔ Firecracker Mapping

| Lambda Concept | Firecracker Component |
|----------------|----------------------|
| Execution environment | One microVM |
| Function memory (128 MB – 10 GB) | Guest memory allocation |
| Function vCPUs (proportional to memory) | Guest vCPU count |
| /tmp storage (512 MB – 10 GB) | virtio-block device |
| Network connectivity | virtio-net + TAP device |
| Function code + runtime | Guest rootfs image |
| Freeze (between invocations) | Pause microVM (halt vCPUs) [INFERRED] |
| Thaw (new invocation) | Resume microVM [INFERRED] |
| SnapStart | Firecracker snapshot/restore |
| Cold start Init phase | Guest boot + runtime initialization |

### 11.3 Why Lambda Chose Firecracker Over Alternatives

| Alternative | Why Not Chosen |
|-------------|---------------|
| **Containers (Docker/runc)** | Shared kernel = weaker isolation. A kernel vulnerability affects all containers. Insufficient for multi-tenant serverless. |
| **Full VMs (QEMU/KVM)** | Too heavy: 100+ MB overhead, 5-30s boot time. Incompatible with Lambda's sub-second cold start requirement. |
| **gVisor** | Userspace kernel reimplementation — good isolation but performance overhead for syscall-heavy workloads. |
| **Kata Containers** | Uses QEMU (too heavy) or Cloud Hypervisor (close to Firecracker but less optimized for serverless). |

**Firecracker hits the sweet spot**: VM-level isolation with container-like performance.

---

## 12. Multi-Tenant Security Model

### 12.1 Defense-in-Depth Layers

```
Layer 0: Hardware
├── Intel VT-x / AMD-V (hardware virtualization)
├── EPT/NPT (hardware memory isolation)
├── CPU microcode updates
└── SMT disabled [recommended for production]

Layer 1: KVM (Kernel)
├── Guest ↔ Host memory isolation via hardware page tables
├── VM Exits trap all privileged operations
├── Separate address spaces per VM
└── No shared memory between VMs

Layer 2: Firecracker (VMM)
├── Minimal device model (5 devices — tiny attack surface)
├── Rust (memory safety — no buffer overflows)
├── Thread-per-vCPU with per-thread seccomp filters
├── Rate limiting on all I/O devices
└── CPU templates to control feature exposure

Layer 3: Jailer
├── Chroot — isolated filesystem view
├── Network namespace — isolated network
├── PID namespace — isolated process view
├── Cgroups — resource limits (CPU, memory, I/O)
├── Unprivileged user — no root capabilities
├── Seccomp — restricted system calls
└── Unique UID/GID per microVM

Layer 4: Lambda Service [INFERRED]
├── No two functions from the same account on the same microVM
├── Code-affinity routing (reuse warm sandbox for same function)
├── Time-bounded sandbox reuse (eviction policies)
└── Encrypted function code at rest and in transit
```

### 12.2 Threat Model

**What Firecracker defends against:**

| Threat | Defense |
|--------|---------|
| Guest escapes VM via KVM vulnerability | Jailer (chroot, seccomp, namespaces) limits blast radius |
| Guest exploits device emulation bug | Only 5 devices, written in Rust (memory safe) |
| Guest consumes all host CPU/memory | Cgroups enforce hard limits |
| Guest floods network | Rate limiters on virtio-net |
| Guest reads other guest's memory | Hardware page tables (EPT/NPT) + separate address spaces |
| Guest reads host filesystem | Chroot — only jail directory visible |
| Guest enumerates host processes | PID namespace |
| Guest accesses host network | Network namespace |
| Side-channel attacks (Spectre, etc.) | SMT disabled, CPU microcode updates, KSM disabled |

**What Firecracker does NOT defend against:**

| Threat | Why |
|--------|-----|
| Host kernel compromise | KVM runs in kernel — a kernel bug affects everything |
| Hardware backdoors | Below Firecracker's layer |
| Physical access attacks | Out of scope |
| Supply chain attacks on Firecracker binary | Trust boundary — users must verify |

### 12.3 Why One MicroVM Per Tenant?

```
Alternative: Multiple functions in one microVM
  Tenant A Function 1 ─┐
  Tenant A Function 2 ──┤── Single microVM
  Tenant B Function 1 ──┘   (DANGEROUS: shared kernel, shared memory)

Firecracker model: One microVM per tenant function
  Tenant A Function 1 ──── microVM 1
  Tenant A Function 2 ──── microVM 2  (separate, even for same tenant)
  Tenant B Function 1 ──── microVM 3
```

Each microVM has its own:
- Kernel (guest kernel instance)
- Memory space (hardware-enforced)
- Filesystem (chroot)
- Network stack (netns)
- Process space (PID ns)
- Resource limits (cgroups)

Even if a vulnerability is found, the blast radius is **one microVM** = one function invocation.

---

## 13. Performance Characteristics

### 13.1 Key Numbers

| Metric | Value | Context |
|--------|-------|---------|
| **Boot time** | < 125 ms | From API call to user code executing |
| **Memory overhead** | < 5 MB per microVM | VMM process overhead (not guest memory) |
| **Creation rate** | 150+ microVMs/sec/host | On a typical bare-metal instance |
| **Creation rate** | ~5 microVMs/sec/core | Per-core metric |
| **Max vCPUs** | 32 per microVM | One thread per vCPU |
| **Minimum config** | 1 vCPU, 128 MB RAM | Smallest possible microVM |
| **Device emulation** | VirtIO (para-virtualized) | Near-native I/O performance |

### 13.2 Why So Fast?

| Factor | Traditional VM | Firecracker |
|--------|---------------|-------------|
| BIOS/UEFI | Full BIOS emulation (slow) | No BIOS — direct kernel boot |
| Device probing | Probes 100+ devices | Only 5 devices to probe |
| PCI enumeration | Full PCI bus scan | No PCI (`pci=off`) — VirtIO-MMIO |
| Kernel | Full Linux kernel | Stripped guest kernel (fewer modules) |
| Init system | systemd, services, etc. | Minimal init → Lambda runtime |

### 13.3 Memory Efficiency on Lambda Hosts [INFERRED]

```
Bare metal host: 384 GB RAM (example)
├── Host OS + KVM overhead:     ~2 GB
├── Lambda control processes:   ~2 GB
└── Available for microVMs:     ~380 GB

Per microVM (128 MB function):
├── Guest memory:               128 MB
├── Firecracker VMM overhead:   ~5 MB
└── Total:                      ~133 MB

Maximum microVMs (128 MB each): ~380,000 MB / 133 MB ≈ 2,857 microVMs
(Practical limit much lower due to CPU, networking, and other factors)

Per microVM (1 GB function):
├── Guest memory:               1,024 MB
├── Firecracker VMM overhead:   ~5 MB
└── Total:                      ~1,029 MB

Maximum microVMs (1 GB each): ~380,000 MB / 1,029 MB ≈ 369 microVMs
```

---

## 14. Comparison: Firecracker vs Containers vs Traditional VMs

### 14.1 Full Comparison Matrix

| Feature | Docker Container | Firecracker MicroVM | Traditional VM (QEMU/KVM) |
|---------|-----------------|---------------------|--------------------------|
| **Isolation boundary** | Linux namespaces + cgroups | KVM + namespaces + cgroups + seccomp | KVM |
| **Kernel** | Shared with host | Separate guest kernel | Separate guest kernel |
| **Boot time** | < 1 second | < 125 ms | 5–30 seconds |
| **Memory overhead** | < 1 MB | < 5 MB | 100–300 MB |
| **Security** | Moderate (shared kernel) | High (hardware + software isolation) | High (hardware isolation) |
| **Devices** | Host devices (passthrough) | 5 emulated (minimal) | 100+ emulated |
| **Snapshots** | Container checkpointing (CRIU) | Native (fast, MAP_PRIVATE) | QEMU snapshots (slow) |
| **Language** | Go | Rust | C |
| **Multi-tenancy** | Risky (kernel vuln = game over) | Safe (VM boundary) | Safe but heavy |
| **Density** | Very high | High | Low |
| **I/O** | Native (no virtualization) | Near-native (VirtIO) | VirtIO or emulated |
| **Use case** | Dev, single-tenant | Multi-tenant serverless | General purpose, long-running |

### 14.2 The Isolation Spectrum

```
Weaker Isolation ◄──────────────────────────────────────────► Stronger Isolation
Higher Density                                                   Lower Density

  Process    Container    gVisor      Firecracker    Full VM
  (fork)     (Docker)     (ptrace/    (microVM)      (QEMU)
                          KVM)
  │           │            │            │              │
  │ Shared    │ Shared     │ Userspace  │ Separate     │ Separate
  │ kernel    │ kernel     │ kernel     │ guest        │ guest
  │ Same      │ Namespace  │ + KVM      │ kernel       │ kernel
  │ address   │ isolation  │            │ + jailer     │ Full
  │ space     │            │            │ + seccomp    │ device
  │           │            │            │ Minimal      │ model
  │           │            │            │ devices      │

  Lambda chose Firecracker: best trade-off between
  security (VM-level) and performance (container-like)
```

---

## 15. Production Host Hardening

### 15.1 AWS Recommendations for Firecracker Hosts

| Category | Recommendation | Why |
|----------|---------------|-----|
| **Seccomp** | Use default (most restrictive) filters | Limits system call exposure |
| **SMT** | Disable hyperthreading | Mitigates Spectre, MDS side-channels |
| **KSM** | Disable Kernel Samepage Merging | Prevents memory deduplication side-channels |
| **Swap** | Disable swap entirely | Prevents guest memory remnants on disk |
| **Kernel** | `quiet loglevel=1` on host kernel | Reduces serial console noise |
| **Microcode** | Latest CPU microcode | Patches hardware vulnerabilities |
| **Cgroups** | v2 preferred | Faster snapshot restore, better hierarchy |
| **Memory** | DDR4 with ECC + TRR | Mitigates Rowhammer attacks |
| **Serial** | Disable in guest: `8250.nr_uarts=0` | Reduces guest-to-host data channel |
| **Overcommit** | Carefully managed | Prevents OOM situations |

### 15.2 Network Flood Mitigation

```
Per-microVM defenses:
1. VirtIO-net rate limiter (Firecracker built-in)
2. tc qdisc on TAP device (traffic control)
3. iptables in network namespace
4. connlimit per microVM
```

### 15.3 Monitoring

Firecracker emits:
- **Logs**: Line-buffered to named pipes (configurable)
- **Metrics**: Emitted at startup, every 60 seconds, and on panic
- Production builds suppress serial console output (prevents host access to guest data)

---

## 16. Design Decisions and Trade-offs

### 16.1 Why Rust?

| Factor | Rationale |
|--------|-----------|
| Memory safety | No buffer overflows, use-after-free, or null pointer dereferences — critical for a VMM |
| Performance | Comparable to C, no garbage collector pauses |
| Concurrency safety | Ownership model prevents data races — important for multi-threaded VMM |
| Ecosystem | Growing Rust ecosystem for systems programming |

A VMM written in C (like QEMU) has had hundreds of CVEs related to memory safety. Rust eliminates entire categories of vulnerabilities.

### 16.2 Why Only 5 Devices?

```
Attack surface analysis:

QEMU device emulation CVEs (partial list):
- CVE-2020-1711: iSCSI heap buffer overflow
- CVE-2019-6778: SLiRP heap buffer overflow
- CVE-2018-16872: USB redirect vulnerability
- CVE-2017-2615: VGA display buffer overflow
- ... hundreds more

Firecracker: 5 devices, ~50K lines of Rust code
  → Dramatically smaller attack surface
  → Each device is simple and well-audited
```

### 16.3 Why VirtIO-MMIO Instead of PCI?

| Aspect | PCI | VirtIO-MMIO |
|--------|-----|-------------|
| Device discovery | PCI bus scan (slow) | Memory-mapped (instant) |
| Code complexity | PCI controller emulation | No PCI needed |
| Boot impact | Must probe bus | `pci=off` — skip entirely |
| Compatibility | Universal | Requires VirtIO-MMIO drivers |

Since Firecracker controls the guest kernel, it can mandate VirtIO-MMIO support and skip PCI entirely. This saves ~100 ms in boot time [INFERRED].

### 16.4 Why Copy the Binary Into the Jail?

"Prevents different microVM instances from sharing memory pages of the Firecracker binary."

Without copying, the kernel's page cache would share the binary's read-only pages across all Firecracker processes. This creates a potential side-channel: one microVM could observe cache eviction patterns caused by another microVM loading the same binary pages.

### 16.5 The Snapshot Uniqueness Trade-off

| Approach | Advantage | Disadvantage |
|----------|-----------|--------------|
| Full cold start every time | Guaranteed unique state | Slow (seconds) |
| Snapshot restore | Fast (~200 ms) | Must handle uniqueness concerns |
| Snapshot + hooks | Fast + handles uniqueness | Developer must implement hooks correctly |

Lambda chose snapshot + hooks (SnapStart), accepting the complexity of uniqueness hooks for the performance benefit.

---

## 17. Interview Angles

### 17.1 Likely Questions

**Q: "Why does Lambda use Firecracker instead of containers?"**

Containers share the host kernel. A kernel vulnerability (e.g., CVE in a syscall handler) would compromise ALL containers on the host — unacceptable for multi-tenant serverless where arbitrary customer code runs. Firecracker provides VM-level isolation (each guest has its own kernel) while maintaining container-like performance: < 5 MB overhead, < 125 ms boot time, 150+ microVMs/sec creation rate. It's the best trade-off between security and performance for serverless workloads.

**Q: "Walk me through the security layers protecting a Lambda function."**

From outside in:
1. **Hardware**: Intel VT-x/AMD-V provides CPU isolation; EPT/NPT provides memory isolation
2. **KVM**: Traps privileged instructions, enforces separate address spaces per VM
3. **Firecracker**: Only 5 devices (tiny attack surface), written in Rust (memory safe), per-thread seccomp filters
4. **Jailer**: Chroot (filesystem isolation), network namespace (network isolation), PID namespace (process isolation), cgroups (resource limits), unprivileged user
5. **Lambda service**: Function-to-microVM mapping, code encryption, credential rotation

Even if a guest exploits a KVM bug to escape the VM, the jailer's seccomp filters limit system calls, chroot limits filesystem access, and the unprivileged UID limits capabilities.

**Q: "How did the 2019 VPC improvement use Firecracker/Hyperplane?"**

Before 2019, each Lambda execution environment created a dedicated ENI in the customer's VPC (10-30 second cold start). After 2019, Lambda introduced Hyperplane ENIs — shared network interfaces that support 65,000 connections each. Instead of creating ENIs at invocation time, they're created at deploy time and shared across all functions with the same subnet/security-group combination. Firecracker's VPC-to-VPC NAT tunnels traffic from the Lambda-managed VPC to the customer VPC through these shared ENIs.

**Q: "What's the SnapStart security concern?"**

When a Firecracker snapshot is restored, the guest has the exact same state as when it was snapshotted — including random number generator seeds, TLS session IDs, and in-memory tokens. If the same snapshot is restored to multiple microVMs simultaneously, they all have identical "unique" values. Lambda mitigates this with `beforeCheckpoint()` and `afterRestore()` hooks that let developers flush connections and re-seed RNGs.

**Q: "How does Firecracker achieve < 125 ms boot time?"**

Four key optimizations:
1. **No BIOS/UEFI**: Direct kernel boot, skipping firmware entirely
2. **No PCI bus**: Uses VirtIO-MMIO (`pci=off`), eliminating bus enumeration
3. **5 devices**: No device probing beyond the 5 virtio devices
4. **Stripped guest kernel**: AWS provides optimized kernels with unnecessary drivers removed

**Q: "How many Lambda functions can run on a single host?"**

Depends on function memory configuration. Each microVM costs ~5 MB overhead plus the allocated memory. On a 384 GB host with ~380 GB available: at 128 MB/function, ~2,800 microVMs theoretically fit (practical limit is lower due to CPU/networking). At 1 GB/function, ~370 microVMs. Firecracker can create 150+ microVMs/second, so scaling up on a host is fast.

### 17.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Firecracker VMM overhead | < 5 MB per microVM |
| Boot time | < 125 ms |
| MicroVM creation rate | 150+ per second per host |
| Creation rate per core | ~5 per second |
| Max vCPUs per microVM | 32 |
| Emulated devices | 5 (virtio-net, virtio-block, virtio-vsock, serial, i8042) |
| Connections per Hyperplane ENI | 65,000 |
| Language | Rust |
| Open-sourced | November 2018 (re:Invent) |
| Based on | Google crosvm (diverged) |
| Uses | Linux KVM |
| Seccomp | Per-thread, most restrictive by default |
| Jailer | Chroot + PID ns + net ns + mount ns + cgroups + UID drop |
| Snapshot types | Full and differential |
| Snapshot restore | MAP_PRIVATE mmap (near-instant) |

### 17.3 Red Flags in Interviews

| Red Flag | Why It's Wrong |
|----------|---------------|
| "Firecracker is a container runtime" | It's a VMM — each microVM has its own kernel |
| "Firecracker uses QEMU" | It replaced QEMU — completely different codebase |
| "Lambda functions share a kernel" | Each execution environment has its own guest kernel in a microVM |
| "Firecracker overhead is ~100 MB" | It's < 5 MB — that's the whole point |
| "Cold starts are slow because of Firecracker boot" | Firecracker boots in < 125 ms — cold starts come from Init code |
| "Snapshots are automatically safe to restore" | Must handle uniqueness (RNG seeds, connections) |
| "Seccomp filters are applied to the guest" | Applied to the Firecracker process (host-side), not the guest kernel |

---

*Cross-references:*
- [Execution Environment Lifecycle](execution-environment-lifecycle.md) — How Init/Invoke/Shutdown map to microVM operations, SnapStart details
- [Worker Fleet and Placement](worker-fleet-and-placement.md) — How Firecracker microVMs are placed and managed on worker hosts
- [Invocation Models](invocation-models.md) — How invocations reach execution environments
- [VPC Networking](vpc-networking.md) — Hyperplane ENIs and network namespace integration
