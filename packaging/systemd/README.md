# User systemd Timer for `sys-investigate`

Installs a low-overhead oneshot sampler (`Nice=10`, `IOSchedulingClass=best-effort`, `MemoryMax=128M`) that appends JSONL telemetry to `~/.local/state/sysadmin-tools/investigate.jsonl` every 5 minutes using `--no-statvfs` so mounted storage stalls never block sampling.

## Prerequisites

Install standalone CLI copies into `~/.local/bin`:

```bash
./scripts/install.sh
```

## Install & Enable

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/sys-investigate-sampler.service ~/.config/systemd/user/
cp packaging/systemd/sys-investigate-sampler.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sys-investigate-sampler.timer
```

## Inspect

```bash
systemctl --user status sys-investigate-sampler.timer sys-investigate-sampler.service
systemctl --user list-timers sys-investigate-sampler.timer
journalctl --user -u sys-investigate-sampler.service -n 50 --no-pager
tail -n 5 ~/.local/state/sysadmin-tools/investigate.jsonl
```

## Disable & Remove

```bash
systemctl --user disable --now sys-investigate-sampler.timer
rm -f ~/.config/systemd/user/sys-investigate-sampler.service ~/.config/systemd/user/sys-investigate-sampler.timer
systemctl --user daemon-reload
```
