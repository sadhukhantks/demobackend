# Runbook: Diagnose Intermittent Slowdown on ERP EC2 Instance

You are an operations agent with shell access to a production server. Follow this
runbook top to bottom. **Read the Ground Rules first — they are binding.**

---

## STATUS — Root cause found 2026-08-13. Read this before running anything.

A full pass of Phases 1–4 was completed on 2026-08-13. **The cause is identified and
Phase 5a monitoring is installed and running.** Do not re-run Phases 1–4 from scratch.

**What to do on the next invocation:** skip to **Section 10 — Follow-Up Procedure**, at the
bottom of this file.

**Root cause:** `src/mobileapp/src/attendance/face_cache.py:117` — `load()` sits inside a
module named `face_cache`, next to a working `_cache` dict and `get()`, and **ignores both**,
re-running `_load()` on every request. Its own docstring admits the cost: "~1-3 s" per call.
Callers: `attendance.py:162` and `attendance.py:405`.

Each `/check-face` or `/attendance` request opens a **new un-pooled MySQL connection**
(`src/mobileapp/db.py:52` = raw `mysql.connector.connect()`) to a **remote** DB, runs a
5-table JOIN over **all ~1,947 branch employees**, does 1,947 × `json.loads`, builds an
ndarray, then runs a dlib encode. Measured: **1,361 reloads in 24h**, 403 + 455 of them in
the two morning-window hours. `/check-face` is the #1 endpoint at 1,000 calls/day.

**The multiplier:** the Flask mobileapp is mounted into the **same uvicorn process as the
entire ERP**, at both `/` and `/api`, via `WSGIMiddleware` (`src/main.py:359-361`), which
dispatches to the anyio threadpool (default **40 threads**). Forty concurrent face requests
× ~28 MB transient each crosses the 1024 MiB cap in minutes — which is why 2026-07-20 shows
three OOM kills in 98 minutes. It also explains why users report *the ERP* is slow rather
than *attendance* is slow: procurement, sales and jute share the hogged process.

**The windows are shift punch-in and shift change — human-scheduled, not cron.**

---

## 0. Ground Rules

1. **Read-only by default.** Phases 1–4 must not change system state. Do not
   restart containers, kill processes, edit configs, or clear logs.
2. **Ask before any write.** Phase 5 installs software. Stop and get explicit
   human approval before starting it.
3. **Never restart the ERP containers to "test" something.** This is production.
   A restart destroys the evidence you are trying to collect.
4. **If a command fails, record the error and continue.** Do not improvise
   workarounds that escalate privileges or install packages outside Phase 5.
5. **Report findings as you go.** Do not batch everything to the end.

---

## 1. Context — What Is Already Known

Do not re-investigate these. They are settled.

**The environment** (verified 2026-08-13)
- Instance: `i-084fcfe389929d9b2`, type **t3a.medium** (2 vCPU, **4 GiB RAM**)
- Workload: ERP frontend + backend, both in Docker
- Root volume: **gp2** EBS
- Amazon Linux 2, kernel 5.10.234, Docker 25.0.8, **cgroup v1**, passwordless sudo
- It is **ECS**, not docker-compose: tasks `vowerp3be-task:119`, `my-nextjs-task:135`
- **`docker inspect` reports `Memory=0` on both app containers — this is misleading.**
  The real cap is the **ECS task-level memcg**: `/sys/fs/cgroup/memory/ecs/<taskid>/`.
  `vowerp3be-task` = **1024 MiB**, `my-nextjs-task` = 1536 MiB.
- uvicorn runs `--host 0.0.0.0 --port 8000` with **no `--workers`** → one process for the
  whole ERP
- **The DB is NOT on this box** — `DATABASE_HOST=3.7.255.145:3306`, a separate EC2, still
  uninstrumented
- Host is never short: 3.8 GiB total, ~2.1 GiB available. **Swap 0.** PSI not compiled into
  this kernel (`/proc/pressure/*` absent)
- Container netns hides DB connections from host `ss` — host showing 0 established to 3306 is
  normal, not a finding

**⚠ TIMEZONE — the single easiest way to miss the correlation**

The box runs **UTC**; users are **IST (+5:30)**. The reported windows translate to:

| Reported (IST) | On the box (UTC) | What it actually is |
|---|---|---|
| 06:30 – 09:00 | **01:00 – 03:30** | shift punch-in |
| 14:30 – 15:30 | **09:00 – 10:00** | shift change |

Apply this shift to every `dmesg -T`, `docker logs` and `/var/log/messages` timestamp or the
evidence looks uncorrelated.

**The symptom**
- Users report the ERP is slow in two daily windows (see table above)
- CPU utilisation rises in exactly those windows, peaking at ~26.8%

**Already ruled out**
| Hypothesis | Verdict | Evidence |
|---|---|---|
| CPU credit throttling | **Ruled out** | T3 defaults to unlimited mode; usage peaks 2.69 credits/5min vs 2.0 earned, only briefly; idle burn is ~10% of earn rate |
| CPU saturation | **Ruled out** | 26.8% peak on 2 vCPU = ~half of one core busy |
| EBS / disk bottleneck | **Ruled out (baseline)** | Idle 99.99%, queue length 0.002, latency <1ms, burst balance 100%, throughput in KiB/s |
| Swap thrashing (B) | **Ruled out** 2026-08-13 | `SwapTotal: 0` — the kernel cannot page out at all. `si`/`so`/`wa` all zero. |
| Host memory exhaustion | **Ruled out** 2026-08-13 | Host holds ~2.1 GiB available throughout. Every kill is `CONSTRAINT_MEMCG`, i.e. the task's own cap. |
| Docker cgroup **CPU** limits | **Ruled out** 2026-08-13 | `CpuQuota=0 NanoCpus=0` on both app containers. No quota exists to throttle against. |
| Scheduled batch work (cron) | **Ruled out** 2026-08-13 | `/etc/cron.d` stock only; both crontabs empty; 2 systemd timers (tmpfiles-clean, readahead); no in-container cron. The windows are **human**-scheduled — shift timings. |

**CONFIRMED: memory exhaustion, variant (A) — OOM kills, at the container cap, not the host.**

Evidence:
- **18 OOM kills** 2026-07-11 → 2026-08-05. Every one `task=uvicorn`,
  `constraint=CONSTRAINT_MEMCG`, anon-rss ≈ **1,044,000 kB identically**.
- 2026-08-03 09:23 UTC and 2026-08-05 09:41 UTC = 14:53 / 15:11 IST — **inside the afternoon
  window**.
- Live ratchet caught during the session: uvicorn rss **809 → 837 MB in one 10 s step, never
  released**. Monotonic — allocator arenas are not returned to the OS.
- The 26.8% CPU peak is *explained*, not contradicted: 455 loads/hr × 1–3 s ≈ 25–38% of one
  core ≈ 13–19% of two. **Not iowait** — one hot path saturating while the host average hides it.

---

## 2. Connect to the Server

**Known-good command** (verified 2026-08-13 — the key lives in `/c/code`):

```bash
cd /c/code && ssh -i vowerp3ui.pem -o StrictHostKeyChecking=no -o BatchMode=yes \
  ec2-user@ec2-13-233-237-141.ap-south-1.compute.amazonaws.com 'bash -s' <<'EOF'
... commands ...
EOF
```

Notes from the last run:
- `sudo` is passwordless, and **`docker` does need `sudo`** — prefix every `docker` command.
- Keep each SSH block under ~2 minutes or the tool backgrounds it. For longer sampling loops,
  run them with `run_in_background: true` and read the output file.
- **The harness permission classifier blocks prod writes** — the Phase 5a install was denied
  twice before the user approved it, and an `nsenter`-containing probe was denied outright.
  Expect to need explicit human approval, and avoid `nsenter`.

SSM Session Manager (`aws ssm start-session --target i-084fcfe389929d9b2 --region ap-south-1`)
was not needed; SSH worked directly.

### Environment detection

Already done — see the verified environment block in Section 1. Only re-run this if the
instance has been rebuilt:

```bash
. /etc/os-release && echo "OS: $NAME $VERSION_ID"
uname -r
sudo docker --version
stat -fc %T /sys/fs/cgroup/ 2>/dev/null   # "cgroup2fs" = v2, "tmpfs" = v1
date -u                                    # confirm the box is still UTC
```

---

## 3. Phase 1 — Immediate Evidence (highest yield)

Run these first. **If Step 1.1 returns hits inside the incident windows, you can
stop investigating and go straight to Section 8.**

### 1.1 OOM kills — the decisive test

```bash
sudo dmesg -T 2>/dev/null | grep -i -E 'out of memory|oom-kill|killed process' | tail -50
```

Also check the persistent journal, since `dmesg` is a ring buffer and may have
already rotated past the incident:

```bash
sudo journalctl -k --since "3 days ago" --no-pager | grep -i -E 'oom|killed process' | tail -50
sudo grep -ri -E 'oom|killed process' /var/log/messages /var/log/syslog 2>/dev/null | tail -50
```

> **Interpretation:** any hit timestamped near 06:30–09:00 or 14:30–15:30
> confirms hypothesis (A). Record which process was killed and its RSS at time
> of death.

### 1.2 Container restart history

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'
```

Then for each ERP container:

```bash
docker inspect <container> --format \
  'Name={{.Name}} Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}}'
```

> **Interpretation:** `OOMKilled=true` or `ExitCode=137` is a memory kill.
> A non-zero `RestartCount` with a recent `StartedAt` means it died today.

### 1.3 Current memory state

```bash
free -h
cat /proc/meminfo | grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree|Committed_AS'
swapon --show
```

> **Interpretation:** `MemAvailable` under ~400 MB means no headroom.
> `SwapTotal: 0` is important — **with no swap configured, the kernel cannot
> page out, so it goes straight to OOM killing.** That would make (A) the only
> possible variant and rules out (B).

### 1.4 Live pressure snapshot

```bash
vmstat 1 10
```

> **Interpretation:** watch three columns —
> - `si` / `so` non-zero → actively swapping (hypothesis B)
> - `wa` above ~10 → waiting on disk
> - `free` shrinking steadily → a leak or growing workload
>
> Note: if run outside the incident window this will look healthy. That is
> expected and not evidence of anything.

### 1.5 Pressure Stall Information (if kernel ≥ 4.20)

```bash
cat /proc/pressure/memory 2>/dev/null
cat /proc/pressure/io 2>/dev/null
cat /proc/pressure/cpu 2>/dev/null
```

> **Interpretation:** `some avg60` above 10% for memory means real stalling.
> This is the single most direct measure of the symptom.

---

## 4. Phase 2 — Memory Attribution

Identify *what* is consuming the 4 GiB.

```bash
docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'
```

Per-container limits — an unlimited container can consume the whole host:

```bash
for c in $(docker ps --format '{{.Names}}'); do
  echo "--- $c"
  docker inspect "$c" --format \
    'Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} NanoCpus={{.HostConfig.NanoCpus}} CpuQuota={{.HostConfig.CpuQuota}} CpuPeriod={{.HostConfig.CpuPeriod}} Restart={{.HostConfig.RestartPolicy.Name}}'
done
```

> `Memory=0` means **no limit set** — that container can OOM the entire host.

Top consumers host-wide:

```bash
ps -eo pid,ppid,rss,pmem,pcpu,etime,comm,args --sort=-rss | head -20
```

Convert RSS to MB mentally (`rss` is in KB). Note anything above ~800 MB.

---

## 5. Phase 3 — Container CPU Throttling

Even at 26% host CPU, a capped container can be starved.

**cgroup v2:**
```bash
for c in $(docker ps -q); do
  name=$(docker inspect "$c" --format '{{.Name}}')
  echo "--- $name"
  sudo cat /sys/fs/cgroup/system.slice/docker-${c}*.scope/cpu.stat 2>/dev/null || \
  sudo cat /sys/fs/cgroup/docker/${c}*/cpu.stat 2>/dev/null
done
```

**cgroup v1:**
```bash
for c in $(docker ps -q); do
  echo "--- $c"
  sudo cat /sys/fs/cgroup/cpu/docker/${c}*/cpu.stat 2>/dev/null
done
```

> **Interpretation:** `nr_throttled` climbing and `throttled_usec` (or
> `throttled_time`) large means the container is being held back by its own
> quota. Sample twice 60s apart — if `nr_throttled` increases between samples,
> throttling is happening *now*.

---

## 6. Phase 4 — Application & Scheduled Work

Two fixed daily windows is the signature of scheduled work. Find it.

```bash
# System-wide cron
sudo ls -la /etc/cron.d/ /etc/cron.daily/ 2>/dev/null
sudo cat /etc/crontab 2>/dev/null
for u in root ec2-user ubuntu; do echo "--- $u"; sudo crontab -u $u -l 2>/dev/null; done

# systemd timers
systemctl list-timers --all --no-pager

# Cron *inside* the containers — often where ERP batch jobs actually live
for c in $(docker ps --format '{{.Names}}'); do
  echo "--- $c"
  docker exec "$c" crontab -l 2>/dev/null
  docker exec "$c" ls /etc/cron.d/ 2>/dev/null
done
```

Container logs for the incident windows — adjust the date:

```bash
docker logs --since '2026-08-13T06:00:00' --until '2026-08-13T09:30:00' \
  --timestamps <backend_container> 2>&1 | tail -300
```

Look for: slow query warnings, request durations, timeouts, connection pool
exhaustion, worker restarts, stack traces clustered at the window boundaries.

**Locate the database** — this determines whether DB load lands on this box:

```bash
docker ps --format '{{.Names}}\t{{.Image}}'   # is there a postgres/mysql/mariadb container?
sudo ss -tulpn | grep -E ':(5432|3306|1521|27017)'
docker exec <backend> env 2>/dev/null | grep -i -E 'db_host|database_url|db_server' 
```

> If the DB is on this instance, it is competing for the same 4 GiB and is the
> prime suspect. If it is on RDS, check RDS CloudWatch metrics separately.

---

## 7. Phase 5 — Instrumentation

> ### ✅ 5a IS ALREADY INSTALLED AND RUNNING (since 2026-08-13 03:25 UTC)
>
> Do **not** reinstall it. The deployed version differs from the template below — it also
> logs per-task memcg `usage/max/**failcnt**`, uvicorn rss/threads/uptime, and per-minute
> `faceloads_60s / reqs_60s / err401_60s / err5xx_60s`.
>
> - Script: `/usr/local/bin/perfsample.sh`
> - Cron: `/etc/cron.d/perfsample` — every minute; prune `-mtime +2` at 03:00 UTC
>   (user reduced retention from 7 days to 2)
> - Logs: `/var/log/perfsample/sample-<UTC-date>.log`
>
> **`failcnt` is the number to watch — it increments on every memcg allocation failure, so it
> goes non-zero *before* the kill.**
>
> Baseline at install: `rss=837MB memcg usage=832 max=865 limit=1024 failcnt=0`, uptime 18.4h.
>
> Removal when finished:
> ```bash
> sudo rm -f /etc/cron.d/perfsample /usr/local/bin/perfsample.sh
> sudo rm -rf /var/log/perfsample
> ```

**Stop and ask the human before installing anything further.** These steps install software
and create persistent files.

### 5a. Lightweight sampler — TEMPLATE ONLY, already deployed (see box above)

```bash
sudo mkdir -p /var/log/perfsample
sudo tee /usr/local/bin/perfsample.sh >/dev/null <<'SCRIPT'
#!/bin/bash
LOG=/var/log/perfsample/sample-$(date +%F).log
{
  echo "===== $(date -Is) ====="
  free -m
  echo "--- pressure"
  cat /proc/pressure/memory 2>/dev/null
  cat /proc/pressure/io 2>/dev/null
  echo "--- vmstat"
  vmstat 1 3 | tail -2
  echo "--- top rss"
  ps -eo rss,pmem,pcpu,comm --sort=-rss | head -8
  echo "--- containers"
  docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} ({{.MemPerc}})' 2>/dev/null
} >> "$LOG" 2>&1
SCRIPT
sudo chmod +x /usr/local/bin/perfsample.sh

# every minute
echo '* * * * * root /usr/local/bin/perfsample.sh' | sudo tee /etc/cron.d/perfsample
# prune after 7 days
echo '0 3 * * * root find /var/log/perfsample -mtime +7 -delete' | sudo tee -a /etc/cron.d/perfsample
```

Verify after two minutes:
```bash
sudo tail -40 /var/log/perfsample/sample-$(date +%F).log
```

**Removal when finished:**
```bash
sudo rm -f /etc/cron.d/perfsample /usr/local/bin/perfsample.sh
sudo rm -rf /var/log/perfsample
```

### 5b. CloudWatch Agent (for memory metrics in the console)

Requires the instance IAM role to have `CloudWatchAgentServerPolicy` attached.
Verify that first — the install will silently fail to publish without it.

```bash
# Amazon Linux
sudo yum install -y amazon-cloudwatch-agent
# Ubuntu
wget https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
```

```bash
sudo tee /opt/aws/amazon-cloudwatch-agent/etc/config.json >/dev/null <<'JSON'
{
  "metrics": {
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "mem":  { "measurement": ["mem_used_percent","mem_available","mem_total"], "metrics_collection_interval": 60 },
      "swap": { "measurement": ["swap_used_percent"], "metrics_collection_interval": 60 },
      "disk": { "measurement": ["used_percent"], "resources": ["/"], "metrics_collection_interval": 60 }
    }
  }
}
JSON

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -m ec2 -a status
```

Metrics appear under the **CWAgent** namespace within ~5 minutes.

### 5c. Enable detailed (1-minute) EC2 monitoring

5-minute averages smooth away short stalls that users absolutely feel.

```bash
aws ec2 monitor-instances --instance-ids i-084fcfe389929d9b2 --region <REGION>
```

---

## 8. Decision Table

Map the evidence to a conclusion:

| Evidence | Conclusion | Action |
|---|---|---|
| OOM kills in logs at window times; `OOMKilled=true` or exit 137 | **Memory exhaustion (A)** | Set per-container memory limits so one service cannot kill the host; then resize to a 8 GiB instance (t3a.large) or tune app worker counts |
| `si`/`so` non-zero, `wa` high, disk ops spike during window | **Swap thrashing (B)** | Same as above — the fix is more RAM or lower memory use, not more swap |
| `SwapTotal: 0` and `MemAvailable` near zero | **Hard OOM path** | No swap means no soft-fail; kernel kills immediately. Confirms (A) |
| `nr_throttled` rising on a container | **cgroup CPU cap** | Raise or remove the container's `--cpus` / `CpuQuota` |
| All host metrics healthy; backend logs show slow queries | **Application / DB layer** | Profile the slow queries; check DB indexes and connection pool sizing |
| Cron or timer firing at 06:30 and 14:30 | **Scheduled batch job** | Reschedule off-hours, or throttle its concurrency |
| Everything clean during a *confirmed* incident window | Escalate | Look upstream: load balancer, network, external API the ERP depends on |

Note the hypotheses are not mutually exclusive — a batch job at 06:30 that
consumes memory and triggers an OOM kill satisfies three rows at once.

---

## 9. Report Format

Produce a single summary in this shape:

```
## Environment
OS, kernel, docker version, cgroup version, container inventory, DB location

## Findings
### Phase 1 — Immediate Evidence
[verbatim relevant output + one-line interpretation each]
### Phase 2 — Memory Attribution
### Phase 3 — CPU Throttling
### Phase 4 — Application Layer

## Conclusion
Which decision-table row(s) matched, and the evidence for each.

## Confidence
High / Medium / Low — and what would raise it.

## Recommended Actions
Ordered, each marked [immediate] / [short-term] / [requires downtime]

## Not Yet Determined
Anything that needs an observation during a live incident window.
```

**If no incident occurred during this session**, say so explicitly rather than
concluding the system is healthy. Recommend installing the Phase 5a sampler and
re-running Phases 1–4 during the next 06:30 or 14:30 window.

---

## 10. Follow-Up Procedure (CURRENT TASK)

State as of 2026-08-13: cause identified, sampler running, **no code change applied** — the
user chose to watch the sampler before fixing. Phases 1–4 are settled; do not redo them.

### Step 1 — Harvest the sampler

```bash
cd /c/code && ssh -i vowerp3ui.pem -o StrictHostKeyChecking=no -o BatchMode=yes \
  ec2-user@ec2-13-233-237-141.ap-south-1.compute.amazonaws.com 'bash -s' <<'EOF'
echo "--- files"; ls -la /var/log/perfsample/
echo "--- any allocation failures yet? (non-zero = pre-kill pressure)"
grep -h 'memcg' /var/log/perfsample/*.log | grep -v 'failcnt=0' | tail -20
echo "--- rss trend, one line per 10 min"
grep -h -A1 '^=====' /var/log/perfsample/*.log | grep -E '^=====|uvicorn pid' | paste - - | awk 'NR%10==1'
echo "--- did uvicorn restart? (uptime_s resetting = it was killed)"
grep -h 'uvicorn pid' /var/log/perfsample/*.log | awk '{print $2, $NF}' | uniq -f1 | tail -20
echo "--- busiest minutes by face loads"
grep -h 'faceloads_60s' /var/log/perfsample/*.log | sort -t= -k2 -rn | head -15
echo "--- fresh OOM kills since 2026-08-13"
sudo dmesg -T | grep -i -E 'oom-kill|killed process' | tail -10
EOF
```

### Step 2 — Interpret

| Observation | Meaning |
|---|---|
| `failcnt` non-zero | The task hit its 1024 MiB cap. Kill is imminent or already happened. |
| `uptime_s` resets to a small number | uvicorn was killed and ECS restarted it. Cross-check `dmesg -T`. |
| rss climbs monotonically, never drops | The allocator ratchet — confirms the diagnosis. |
| `faceloads_60s` spikes in 01:00–03:30 / 09:00–10:00 UTC | Confirms the windows are punch-driven. |
| High `err401_60s` | Retry storm — each 401 doubles the face load in the worst window. |
| Windows busy but rss flat and `failcnt=0` | The cache path is **not** the whole story → investigate the DB box at `3.7.255.145`. |

### Step 3 — Apply the fix (only when the user says go)

Agreed, in order. **None of these are applied yet.**

1. **[immediate, no deploy]** ECS task memory 1024 → 2048 MiB; frontend task 1536 → 1024
   (it uses only 181 MiB). Buys headroom; **does not fix the slowness**.
2. **[the actual fix]** `src/mobileapp/src/attendance/face_cache.py` — key `_cache` by
   `(db_name, branch_id)` with a short TTL; keep `invalidate()` on face registration.
   ~15 lines, both call sites unchanged. Removes 1,361 daily round trips, the ratchet, and
   the 1–3 s per-request cost at once.
3. `src/mobileapp/db.py` — pool connections via `mysql.connector.pooling.MySQLConnectionPool`,
   one pool per DB name (the name varies per subdomain).
4. Investigate the **304 × `401 Unauthorized`**/24h on `/check-face` and `/attendance`.

### Step 4 — Separate issues found, not part of this fix

- Live prod 500s: `Unknown column 'indent_bal_qty' in 'field list'` — schema drift vs the
  deployed code.
- Loaded guns on a 1 GiB cap, **not** implicated in these windows (absent from the top-25
  request mix): `src/hrms/reportQueries.py` has **no `LIMIT` anywhere** plus unbounded date
  ranges over `daily_attendance`; `GET /doff-transactions`
  (`src/mobileapp/src/doff/doff.py:173`) has every filter optional → full-table scan.

### Step 5 — Tear down when done

```bash
sudo rm -f /etc/cron.d/perfsample /usr/local/bin/perfsample.sh
sudo rm -rf /var/log/perfsample
```

### Still open

No live incident was observed on 2026-08-13 — that morning window was quiet (load 0.02–0.19).
Confidence is **high** on the OOM mechanism, **medium** that it is the whole story for the
morning window. If slowness persists after the cache fix, the next place to look is the **DB
box at `3.7.255.145`**, which no one has examined.