# ─── DarkNodes VPS Image ─────────────────────────────────────────────────────
# Base: cruizba/ubuntu-dind (https://github.com/cruizba/ubuntu-dind)
# The community reference for Ubuntu + systemd + Docker-in-Docker (272 ★).
# Originally derived from nestybox/sysbox examples (4K ★), adapted for
# standard --privileged Docker without requiring the sysbox runtime.
#
# Bot-specific additions layered on top:
#   • openssh-server   — SSH access on port 22 (exposed via host port binding)
#   • fuse-overlayfs   — storage driver for nested Docker without root overlay
#   • tmate            — terminal sharing sessions
#   • sshx             — web-based terminal sessions
#
# Runtime requirements:
#   --privileged  --cgroupns=host
#   --tmpfs /run:exec,mode=755,size=256m
#   --tmpfs /run/lock:size=64m
#   --tmpfs /tmp:exec,size=512m
#   Named volumes for /var/lib/docker  /home  /root  /opt  (persistence)
# ─────────────────────────────────────────────────────────────────────────────

ARG UBUNTU_VERSION="24.04"
FROM ubuntu:${UBUNTU_VERSION}

ARG UBUNTU_VERSION
# Docker is installed from the official Docker apt repository below.
# Pinning to a specific engine version is optional; omitting the pin takes the
# latest stable release which is fine for VPS containers.
ENV DEBIAN_FRONTEND=noninteractive \
    container=docker

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — systemd + base system packages
# Identical to cruizba/ubuntu-dind structure; extended with openssh-server and
# fuse-overlayfs for VPS access and nested-Docker storage.
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        systemd \
        systemd-sysv \
        libsystemd0 \
        ca-certificates \
        dbus \
        iptables \
        iproute2 \
        kmod \
        locales \
        sudo \
        udev \
        curl \
        wget \
        xz-utils \
        openssh-server \
        fuse3 \
        fuse-overlayfs && \
    \
    # Prevent journald from reading kernel messages from /dev/kmsg
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf && \
    \
    # Housekeeping
    apt-get clean -y && \
    rm -rf \
       /var/cache/debconf/* \
       /var/lib/apt/lists/* \
       /var/log/* \
       /tmp/* \
       /var/tmp/* \
       /usr/share/doc/* \
       /usr/share/man/* \
       /usr/share/local/* && \
    \
    # Create default 'admin/admin' user
    useradd --create-home --shell /bin/bash admin && \
    echo "admin:admin" | chpasswd && \
    usermod -aG sudo admin && \
    printf 'admin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/admin && \
    chmod 440 /etc/sudoers.d/admin

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — mask systemd units unnecessary inside a container
# Source: cruizba/ubuntu-dind + additional units from nestybox recommendations
# ─────────────────────────────────────────────────────────────────────────────
RUN systemctl mask \
        systemd-udevd.service \
        systemd-udevd-kernel.socket \
        systemd-udevd-control.socket \
        systemd-modules-load.service \
        sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
        systemd-networkd-wait-online.service \
        systemd-logind.service \
        getty.service \
        getty.target \
        2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 3 — iptables backend (follows cruizba/ubuntu-dind logic)
#  - Ubuntu 22.04/24.04: legacy backend
#  - Ubuntu 26.04+: nft backend (iptables-legacy can't init nat table in 1.8.11)
# ─────────────────────────────────────────────────────────────────────────────
RUN case "${UBUNTU_VERSION}" in \
        20.04) ;; \
        22.04|24.04) \
            update-alternatives --set iptables  /usr/sbin/iptables-legacy  2>/dev/null || true; \
            update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null || true ;; \
        *) \
            update-alternatives --set iptables  /usr/sbin/iptables-nft  2>/dev/null || true; \
            update-alternatives --set ip6tables /usr/sbin/ip6tables-nft 2>/dev/null || true ;; \
    esac

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 4 — Docker Engine + Buildx + Compose (all from the official Docker
# apt repository in a single apt-get install call).
#
# Why apt instead of get.docker.com + separate GitHub binaries?
#   • One key fetch + one apt-get update replaces three slow network round-trips.
#   • Buildx and Compose are shipped as official apt packages (docker-buildx-plugin,
#     docker-compose-plugin) — no separate GitHub binary downloads needed.
#   • The Docker apt CDN is heavily cached and fast globally.
# ─────────────────────────────────────────────────────────────────────────────
RUN set -eux; \
    # Add Docker's official GPG key (retries for flaky networks on remote nodes)
    install -m 0755 -d /etc/apt/keyrings && \
    curl --retry 5 --retry-delay 10 --retry-max-time 120 --connect-timeout 30 \
        -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    \
    # Add the Docker apt repository
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
      > /etc/apt/sources.list.d/docker.list && \
    \
    # Install Docker Engine, CLI, containerd, Buildx, and Compose in one pass
    apt-get update && \
    apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin && \
    apt-get clean -y && \
    rm -rf /var/lib/apt/lists/* && \
    \
    # Add admin to docker group and verify
    usermod -a -G docker admin && \
    docker --version && \
    docker buildx version && \
    docker compose version && \
    \
    # Compat symlink so `docker-compose` (v1 style) still works
    ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 7 — Docker daemon config
# Default to overlay2 (native kernel driver, starts DinD in ~3-5 s).
# configure_vps detects if overlay2 fails within 20 s and rewrites this file
# to fuse-overlayfs automatically before restarting dockerd.
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /etc/docker && \
    printf '{\n  "storage-driver": "overlay2",\n  "dns": ["8.8.8.8", "1.1.1.1"],\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n' \
    > /etc/docker/daemon.json

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 8 — SSH setup
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /etc/ssh/sshd_config.d && \
    ssh-keygen -A && \
    echo "root:root" | chpasswd && \
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/'          /etc/ssh/sshd_config && \
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \
    systemctl enable ssh && \
    systemctl enable docker

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 9 — tmate (terminal sharing; static binary from GitHub releases)
# Non-fatal: if the download fails (slow/blocked network), the build continues
# and the bot falls back gracefully (checks with || echo MISSING at runtime).
# ─────────────────────────────────────────────────────────────────────────────
RUN arch="$(uname -m)" && \
    if [ "$arch" = "x86_64" ];    then ta="amd64"   ; \
    elif [ "$arch" = "aarch64" ]; then ta="arm64v8" ; \
    elif [ "$arch" = "armv7l" ];  then ta="arm32v7" ; \
    else echo "tmate: unsupported arch $arch — skipping" >&2; exit 0; fi && \
    ( \
        curl --retry 5 --retry-delay 10 --retry-max-time 300 --connect-timeout 30 -fsSL \
            "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${ta}.tar.xz" \
            -o /tmp/tmate.tar.xz && \
        tar -xf /tmp/tmate.tar.xz -C /tmp && \
        install -m 755 "/tmp/tmate-2.4.0-static-linux-${ta}/tmate" /usr/local/bin/tmate && \
        rm -rf /tmp/tmate* && \
        tmate -V \
    ) || echo "tmate install skipped (download failed — will be unavailable at runtime)"

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 10 — sshx (web-based terminal sessions)
# Pinned to a specific GitHub release binary instead of the live curl|sh pipe.
# Non-fatal: if the download fails the build continues; bot checks at runtime.
# ─────────────────────────────────────────────────────────────────────────────
RUN arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  sshx_arch="x86_64-unknown-linux-musl"  ;; \
        aarch64) sshx_arch="aarch64-unknown-linux-musl" ;; \
        armv7l)  sshx_arch="armv7-unknown-linux-musleabihf" ;; \
        *) echo "sshx: unsupported arch $arch — skipping" >&2; exit 0 ;; \
    esac && \
    ( \
        curl --retry 3 --retry-delay 10 --retry-max-time 120 --connect-timeout 30 -fsSL \
            "https://github.com/ekzhang/sshx/releases/download/v0.2.2/sshx-v0.2.2-${sshx_arch}.tar.gz" \
            -o /tmp/sshx.tar.gz && \
        tar -xzf /tmp/sshx.tar.gz -C /tmp && \
        install -m 755 /tmp/sshx /usr/local/bin/sshx && \
        rm -f /tmp/sshx.tar.gz /tmp/sshx && \
        sshx --version 2>/dev/null || true \
    ) || echo "sshx install skipped (download failed — will be unavailable at runtime)"

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 11 — Pre-bake static runtime config
#
# Everything here is identical on every container instance, so doing it once
# at build time means configure_vps only has to handle the three dynamic
# values (root password, hostname, DinD wait).  This shaves ~30-40 s off
# every deployment without touching container-specific state.
# ─────────────────────────────────────────────────────────────────────────────
RUN set -eux; \
    \
    # ── Locale + timezone ────────────────────────────────────────────────────
    locale-gen en_US.UTF-8 && \
    update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 && \
    ln -snf /usr/share/zoneinfo/UTC /etc/localtime && \
    echo UTC > /etc/timezone && \
    \
    # ── /etc/environment — survives all shells and systemd units ─────────────
    printf 'LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\nLANGUAGE=en_US:en\nDEBIAN_FRONTEND=noninteractive\nTZ=UTC\nNEEDRESTART_MODE=a\nNEEDRESTART_SUSPEND=1\nDEBIAN_PRIORITY=critical\nPIP_BREAK_SYSTEM_PACKAGES=1\n' \
        > /etc/environment && \
    \
    # ── apt: retries + non-interactive dpkg ──────────────────────────────────
    printf 'APT::Acquire::Retries "5";\nAPT::Acquire::http::Timeout "30";\nAPT::Acquire::https::Timeout "30";\n' \
        > /etc/apt/apt.conf.d/80retries && \
    printf 'DPkg::Options:: "--force-confdef";\nDPkg::Options:: "--force-confold";\n' \
        > /etc/apt/apt.conf.d/90dpkg && \
    \
    # ── Enable universe + multiverse (DEB822 format for Ubuntu 24.04+) ───────
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i 's/^Components: main.*/Components: main restricted universe multiverse/' \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's/^# \(deb.*universe\)/\1/'   /etc/apt/sources.list; \
        sed -i 's/^# \(deb.*multiverse\)/\1/' /etc/apt/sources.list; \
    fi && \
    \
    # ── needrestart — no interactive "restart services?" prompts ─────────────
    mkdir -p /etc/needrestart/conf.d && \
    printf '$nrconf{restart}     = '"'"'a'"'"';\n$nrconf{kernelhints} = 0;\n$nrconf{ucodehints}  = 0;\n' \
        > /etc/needrestart/conf.d/50-darknodes.conf && \
    \
    # ── pip: system-wide installs without --break-system-packages ────────────
    mkdir -p /etc/pip /root/.config/pip /home/admin/.config/pip && \
    printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf && \
    cp /etc/pip.conf /root/.config/pip/pip.conf && \
    cp /etc/pip.conf /home/admin/.config/pip/pip.conf && \
    chown -R admin:admin /home/admin/.config/pip && \
    \
    # ── python + pip bare aliases (Ubuntu 24.04 removed them) ────────────────
    if ! command -v python >/dev/null 2>&1; then \
        update-alternatives --install /usr/bin/python python /usr/bin/python3 10 2>/dev/null || true; \
        ln -sf /usr/bin/python3 /usr/local/bin/python 2>/dev/null || true; \
    fi && \
    if ! command -v pip >/dev/null 2>&1; then \
        ln -sf /usr/bin/pip3 /usr/local/bin/pip 2>/dev/null || true; \
    fi && \
    \
    # ── npm global prefix ─────────────────────────────────────────────────────
    npm config set prefix /usr/local 2>/dev/null || true && \
    \
    # ── root sudoers (admin already done in Layer 1) ─────────────────────────
    echo "root  ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/root && \
    chmod 440 /etc/sudoers.d/root && \
    \
    # ── System-wide PATH profile — Go, Cargo, pip globals ────────────────────
    printf '#!/bin/sh\nexport DEBIAN_FRONTEND=noninteractive\nexport NEEDRESTART_MODE=a\nexport NEEDRESTART_SUSPEND=1\nexport PIP_BREAK_SYSTEM_PACKAGES=1\nexport GOPATH="${GOPATH:-/root/go}"\nif command -v go >/dev/null 2>&1; then\n    export PATH="$GOPATH/bin:$(go env GOROOT 2>/dev/null || echo /usr/local/go)/bin:$PATH"\nfi\n[ -d /root/.cargo/bin ] && export PATH="/root/.cargo/bin:$PATH"\n' \
        > /etc/profile.d/00-darknodes-path.sh && \
    chmod +x /etc/profile.d/00-darknodes-path.sh && \
    \
    # ── sysctl — real-VPS kernel parameters ──────────────────────────────────
    printf 'net.ipv4.ip_forward            = 1\nnet.ipv4.tcp_fin_timeout       = 30\nnet.ipv4.tcp_keepalive_time    = 300\nnet.core.somaxconn             = 65535\nnet.core.netdev_max_backlog    = 5000\nfs.file-max                    = 1000000\nfs.inotify.max_user_watches    = 524288\nvm.swappiness                  = 10\nvm.overcommit_memory           = 1\nkernel.dmesg_restrict          = 0\n' \
        > /etc/sysctl.d/99-darknodes.conf && \
    \
    # ── ulimits — raise file-descriptor limits for all users ─────────────────
    printf '*    soft  nofile   65536\n*    hard  nofile   65536\n*    soft  nproc    65536\n*    hard  nproc    65536\nroot soft  nofile   65536\nroot hard  nofile   65536\n' \
        > /etc/security/limits.d/99-darknodes.conf && \
    \
    # ── SSH: keepalive + MOTD settings ───────────────────────────────────────
    grep -q "^ClientAliveInterval" /etc/ssh/sshd_config || echo "ClientAliveInterval 60"  >> /etc/ssh/sshd_config && \
    grep -q "^ClientAliveCountMax" /etc/ssh/sshd_config || echo "ClientAliveCountMax 10"  >> /etc/ssh/sshd_config && \
    sed -i 's/^#*PrintLastLog.*/PrintLastLog no/' /etc/ssh/sshd_config 2>/dev/null || true && \
    grep -q "^PrintMotd" /etc/ssh/sshd_config || echo "PrintMotd yes" >> /etc/ssh/sshd_config && \
    \
    # ── Disable noisy Ubuntu update-motd.d scripts ───────────────────────────
    chmod -x /etc/update-motd.d/* 2>/dev/null || true && \
    rm -f /etc/motd && \
    \
    # ── MOTD script (reads /etc/darknodes-brand at login time) ───────────────
    printf '#!/bin/bash\n_brand=$(cat /etc/darknodes-brand 2>/dev/null || echo "DarkNodes")\n_hn=$(hostname 2>/dev/null || echo "DarkNodes-VPS")\n_os=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo "Ubuntu")\n_kern=$(uname -r 2>/dev/null || echo "unknown")\n_uptime=$(uptime -p 2>/dev/null || echo "unknown")\n_load=$(uptime 2>/dev/null | awk -F'"'"'load average:'"'"' '"'"'{print $2}'"'"' | xargs || echo "unknown")\n_mem_total=$(free -m 2>/dev/null | awk '"'"'/^Mem:/{print $2}'"'"')\n_mem_used=$(free -m 2>/dev/null | awk '"'"'/^Mem:/{print $3}'"'"')\n_disk=$(df -h / 2>/dev/null | awk '"'"'NR==2{print $3"/"$2" ("$5" used)"}'"'"' || echo "unknown")\n_docker=$(docker ps --format "{{.Names}}" 2>/dev/null | wc -l || echo "0")\necho ""\necho "  Welcome to your $_brand VPS  |  $_hn"\necho "  OS: $_os  |  Kernel: $_kern"\necho "  Uptime: $_uptime  |  Load: $_load"\necho "  Memory: $_mem_used / $_mem_total MiB  |  Disk: $_disk  |  Docker: $_docker running"\necho ""\n' \
        > /etc/profile.d/darknodes-motd.sh && \
    chmod +x /etc/profile.d/darknodes-motd.sh

# ─────────────────────────────────────────────────────────────────────────────
# Correct stop signal for systemd containers (as per cruizba/ubuntu-dind)
STOPSIGNAL SIGRTMIN+3

# Named volume for Docker daemon data (avoids storage-driver issues)
VOLUME /var/lib/docker

# systemd as PID 1 — the only correct init for a systemd container
ENTRYPOINT ["/sbin/init", "--log-level=err"]
