# Deployment — `kotak_zerodha_prasad_patankar`

Runs `main.py` directly from a git checkout under systemd. No pyinstaller
binary, no `monitor.sh`, **no cron**. Other algos already share this VPS, so
every unit here is name-scoped and cannot disturb them.

## Layout

```
/opt/algo/                                  <- sparse git clone (branch: prasad_changes)
  kotak_zerodha_prasad_patankar/
    main.py, utility/, data/, strategy/     <- tracked, pullable
    *.template                              <- tracked; copy these in step 5
    .env, user/credentials.py               <- LOCAL ONLY, gitignored
    user_selection.csv                      <- LOCAL ONLY, gitignored
    logs/                                   <- gitignored, created at runtime
/opt/iob-venv/                              <- venv, deliberately outside the tree
/usr/local/sbin/iob-watchdog.sh             <- root-owned, outside the writable tree
```

`.env`, `user/credentials.py` and `user_selection.csv` are **not in git**, so a
fresh clone does not contain them. Each ships a tracked `.template` to copy in
step 5. That split is intentional: the first two hold secrets, and the third is
rewritten at runtime by the dashboard (tracking it would make every `git pull`
conflict).

---

## Step 0 — Read this before you start

**Timezone.** There is no timezone handling anywhere in this codebase: 27 bare
`datetime.now()` calls in `main.py`, zero hits for `pytz` / `ZoneInfo` /
`timezone` / `utcnow`. Market hours are hardcoded naive local times
(09:15:30, 15:30:50, 23:16:00). On a UTC host every one is off by 5.5 hours,
and the `OnCalendar=` triggers in the timers are wrong too.

**Do not run the `copy to VPS/` scripts on this box.** They assume one
deployment per machine:

| Script | What it does to a shared VPS |
|---|---|
| `setup.sh` | `cat <<EOF \| crontab -` **replaces** the whole crontab — wipes every other algo's cron entries, silently |
| `stopeverything.sh` | `pkill -f monitor.sh` matches by pattern across all processes |
| `monitor.sh`, `cleanall.sh` | Hardcode `/home/myfiles/` |

systemd replaces all four (mapping at the bottom).

**Cloning this repo puts other users' credentials on this VPS.**
`credentials.py` has been committed under seven paths (Harshad, Mihir,
Rajendra, Chinmay, Anand, two Avadhoot variants), each holding live broker
keys, TOTP seeds and a plaintext Zerodha password. The clone in step 4 uses
`--depth 1 --filter=blob:none --sparse` so those blobs are never fetched — a
real reduction, but **not a security boundary**: it is a promisor remote, so
anything stays fetchable on demand, and the remote still holds all of it. The
only true fix is rotating those keys and purging history.

---

## Step 1 — Local: commit and push this user folder

The VPS clones from `origin`, so the folder has to be pushed first. Secrets are
gitignored and will not travel with it.

```bash
git add .gitignore kotak_zerodha_prasad_patankar
```

```bash
git status
```

Confirm `.env`, `user/credentials.py` and `user_selection.csv` are **absent**
from what is staged. Then:

```bash
git commit -m "Add prasad_patankar user + systemd deployment"
```

```bash
git push origin prasad_changes
```

Remote is `https://github.com/anandjoshi4u/Running-Systems.git` — not your
account. If it is private, put a **read-only deploy key** on the VPS. Given
what is in this repo's history, avoid a token that can push.

## Step 2 — VPS prerequisites

```bash
timedatectl
```

```bash
sudo timedatectl set-timezone Asia/Kolkata
```

```bash
sudo apt update && sudo apt install -y python3-venv git nginx
```

## Step 3 — Service user and directories

The algo runs as a dedicated unprivileged `iob` account rather than as root.
The reason is narrow but real: the Flask dashboard is reachable from the
internet through nginx and accepts POSTs that rewrite `user_selection.csv` and
toggle strategies. A flaw there should not be a root compromise on a box that
also hosts your other algos.

```bash
sudo useradd --system --home-dir /opt/algo --shell /usr/sbin/nologin iob
```

```bash
sudo mkdir -p /opt/algo /opt/iob-venv && sudo chown iob:iob /opt/algo /opt/iob-venv
```

### Let your own login account manage the files

Without this, `/opt/algo` is writable only by `iob` and root — so SFTP uploads
from MobaXterm fail, because you log in as neither. Add yourself to the `iob`
group:

```bash
sudo usermod -aG iob $USER
```

**Log out and back in** for the new group to take effect, then confirm:

```bash
id
```

`iob` must appear in the `groups=` list. If it does not, the SFTP writes in
step 5 will still be refused.

## Step 4 — Sparse clone

```bash
sudo -u iob -H git clone --depth 1 --single-branch --branch prasad_changes --filter=blob:none --sparse https://github.com/anandjoshi4u/Running-Systems.git /opt/algo
```

```bash
sudo -u iob -H git -C /opt/algo sparse-checkout set kotak_zerodha_prasad_patankar
```

Verify only your folder materialised:

```bash
ls /opt/algo
```

## Step 5 — Secrets and config (not in git)

These three files are **gitignored, so the clone does not contain them.** Each
has a committed `.template` — create all three, then fill them in:

```bash
cd /opt/algo/kotak_zerodha_prasad_patankar
```

```bash
sudo -u iob cp user_selection.csv.template user_selection.csv
```

```bash
sudo -u iob cp .env.template .env
```

```bash
sudo -u iob cp user/credentials.py.template user/credentials.py
```

### 5a. `user/credentials.py`

```python
CLIENT_NAME = "prasad_patankar"      # dashboard username + ledger filename

# ===== ANGEL — unused (angel_login is commented out at main.py:110) =====
# API_KEY, USER_ID and TOTP_KEY must stay DEFINED even though empty:
# common_functions.py:8 imports them by name at module load, so deleting them
# breaks the import before anything runs. secretKey is only picked up by the
# `import *` in config.py:4 and is safe to drop.
API_KEY = ""
secretKey = ""
TOTP_KEY = ""
USER_ID = ""

# ===== KOTAK NEO =====
consumer_key  = "xxxxxxxxxxxxxxxxxxxxxxxx"
totpkey       = "XXXXXXXXXXXXXXXX"        # base32 2FA seed, NOT a 6-digit code
mobile_number = "+91XXXXXXXXXX"           # country code required
ucc           = "XXXXX"

# ===== ZERODHA =====
ZERODHA_API_KEY    = "xxxxxxxxxxxxxxxx"
ZERODHA_API_SECRET = "xxxxxxxxxxxxxxxx"
ZERODHA_USER_ID    = "XX0000"
ZERODHA_PASSWORD   = "your-kite-password"
ZERODHA_TOTP_KEY   = "XXXXXXXXXXXXXXXX"   # base32 2FA seed

# ===== TELEGRAM =====
BOT_TOKEN = "0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
CHAT_ID   = "000000000"
```

Both `totpkey` and `ZERODHA_TOTP_KEY` are the **base32 secret** shown when you
set up 2FA (the string behind the QR code), not a rotating 6-digit code —
`pyotp` derives the code from it at login.

The Kotak **MPIN** and Angel **PIN** are deliberately *not* in this file. They
live in `user_selection.csv` (5c).

### 5b. `.env`

```
DASHBOARD_PASS=your-strong-password-here
```

`load_env_file()` at `main.py:65-73` splits on the first `=` and strips
whitespace — no quoting, no escaping, no `export`. Write the value bare;
quotes would become part of the password. Lines starting with `#` are skipped.

The dashboard username is `CLIENT_NAME` from 5a. If `DASHBOARD_PASS` is unset,
`dashboard.PASSWORD` is `None` (`main.py:80`) and nobody can log in.

### 5c. `user_selection.csv`

Set the two login IDs at the top:

```
SUBSCRIPTION_IDK,<Kotak MPIN + padding>,
APPROVAL_IDA,<Angel PIN + padding>,
```

`config.py:186-187` slices these — first **6** digits of `SUBSCRIPTION_IDK` are the
Kotak MPIN, first **4** of `APPROVAL_IDA` are the Angel PIN. Pad with extra
digits after them; the padding is ignored. `apply_login_config` raises
`ValueError` while either is blank, so the service cannot start until both are
set. That is deliberate.

> **`APPROVAL_IDA` is required even though Angel One is not used here.**
> `config.py:180-181` rejects anything shorter than 4 characters *before* it
> looks at whether Angel is enabled, and `angel_login` is commented out at
> `main.py:110`. The only consumer of the parsed value is
> `common_functions.py:32`, inside that dead function.
>
> So it is a mandatory field with no effect. Put any 4+ digit placeholder in
> it — `0000` is fine — or the service refuses to start with
> `ValueError: ANGEL_PIN must be at least 4 digits`. If you ever do enable
> Angel, replace it with the real PIN.

> **All `STRATEGY_*` flags ship as `FALSE` and every `*_LOT` is set to `1`**
> — deliberately minimal, to be raised to suit your capital. Turn strategies on
> one at a time and confirm fills before scaling.

`*_LOT` is a **multiplier on the exchange lot size**, not a share count:
`config.py` computes e.g. `ctx.ceb_lot = ctx.nifty_near_lot * CEB_LOT`, where
`nifty_near_lot` comes from the scrip master. So `1` means one full Nifty lot
(75 units at time of writing), which is the smallest tradeable position — not
one share.

Note a quirk when you edit these: `config.py:34` casts `'1'` to Python `True`
and `'0'` to `False` before the multiply. It is arithmetically correct
(`75 * True == 75`, an `int`), so nothing is broken — but do not be surprised
to see `True` if you ever print the raw config dict.

### Permissions

`/opt` is world-readable (755) by default and this box has other tenants, so
shut out everyone who is not `iob` or a member of the `iob` group (which now
includes you, from step 3):

```bash
sudo find /opt/algo/kotak_zerodha_prasad_patankar -type d -exec chmod 2770 {} +
```

Note this is applied to **every directory**, not just the top one. The clone
creates `user/`, `utility/`, `data/`, `strategy/` and `deploy/` at mode 755
owned by `iob`, so a non-recursive `chmod` leaves them read-only to you — and
`user/credentials.py`, the file you most need to edit, lives inside `user/`.

```bash
sudo chmod 660 /opt/algo/kotak_zerodha_prasad_patankar/.env /opt/algo/kotak_zerodha_prasad_patankar/user/credentials.py /opt/algo/kotak_zerodha_prasad_patankar/user_selection.csv
```

The leading `2` is **setgid**: every file created in that directory inherits
group `iob` regardless of who creates it. Without it, files you upload land
with your own primary group and the service loses access to them.

`660` rather than `600` is what lets you edit these over SFTP. Others still get
nothing.

### If you prefer not to upload at all

These files hold your broker keys and Zerodha password. Sending them over SFTP
leaves copies in MobaXterm's local session data. Editing in place avoids that,
and needs no group membership:

```bash
sudo -u iob nano /opt/algo/kotak_zerodha_prasad_patankar/.env
```

`.env` is one line and `credentials.py` about twenty, so this is often quicker
than an upload anyway.

### Why the whole folder must be writable

`dashboard.py:103-108` saves config as `user_selection.csv.tmp` then
`os.replace()`s it — that needs **directory** write, not just file write. Which
is also why the `.py` files are not kept root-owned: once the directory is
writable, anything in it can be replaced regardless of per-file ownership, so
file-level hardening there would be illusory. `ReadWritePaths=` in the unit
limits the blast radius to this one folder; the rest of the checkout, including
`.git`, stays read-only to the service.

## Step 6 — Virtualenv

```bash
sudo -u iob -H python3 -m venv /opt/iob-venv
```

```bash
sudo -u iob -H /opt/iob-venv/bin/pip install -r /opt/algo/kotak_zerodha_prasad_patankar/deploy/requirements.txt
```

`-H` sets `HOME` so pip's cache lands somewhere writable. The venv lives
outside the checkout so the working tree stays clean and re-cloning does not
force a rebuild.

This needs `git` on the box (step 2 installs it): the **Kotak Neo SDK is not on
PyPI**. `pip install neo-api-client` fails with *"No matching distribution
found"* — it only ships from GitHub, so `requirements.txt` pulls it over
`git+https` at a pinned tag. The other eight come from PyPI normally.

Two traps worth knowing about, because neither announces itself clearly:

- **The repo is `Kotak-neo-api-v2`, not `kotak-neo-api`.** The old one now
  404s. If you point a clone at it, git prompts for a GitHub username and
  password and then fails auth — GitHub answers 404 rather than 403 for repos
  it will not serve anonymously, so a *missing* repo looks exactly like a
  *private* one. That prompt means "wrong URL", not "you need credentials".
- **v1 imports fine and then fails at login.** This code calls
  `client.totp_login()` and `client.totp_validate()`; the v1 line exposes
  `login()` / `session_2fa()` instead. A v1 install passes any import-based
  check and then dies at 09:10 with an `AttributeError`. `smoketest.py`
  therefore asserts the SDK *method surface*, not just importability.

Smoke-test the imports. Note this imports the *submodules* only — importing
`main` would attempt a live broker login, so do not:

```bash
sudo -u iob -H /opt/iob-venv/bin/python /opt/algo/kotak_zerodha_prasad_patankar/deploy/smoketest.py
```

### Freeze the versions once it works

The PyPI entries are unpinned, so a fresh install resolves to whatever is
current. This code leans on pandas heavily in `reconcile.py`, `ledger.py` and
`orders.py`. Once the deployment is verified, lock it:

```bash
sudo -u iob -H bash -c '/opt/iob-venv/bin/pip freeze > /opt/iob-venv/requirements.lock.txt'
```

Install from the lock file thereafter, so an unrelated reinstall months from
now cannot shift order-handling behaviour underneath you.

Two deliberate details in that command:

- **The redirect runs inside `bash -c`.** Writing it as
  `sudo -u iob pip freeze > file` fails with `Permission denied`, because `>`
  is handled by *your* shell as your own user before `sudo` runs at all. Only
  the `pip` half would have been elevated.
- **It writes next to the venv, not into the checkout.** The lock describes
  `/opt/iob-venv`, and keeping it out of the git tree avoids an untracked file
  in every `git status`. It also sidesteps a dead end: the VPS should hold a
  read-only deploy key, so it cannot push the lock file back anyway.

To version it, print it and paste the contents into `deploy/` in your local
clone, then commit from there:

```bash
cat /opt/iob-venv/requirements.lock.txt
```

## Step 7 — systemd

```bash
cd /opt/algo/kotak_zerodha_prasad_patankar/deploy
```

```bash
sudo cp iob.service iob.timer iob-clean.service iob-clean.timer iob-watchdog.service iob-watchdog.timer /etc/systemd/system/
```

```bash
sudo install -o root -g root -m 755 iob-watchdog.sh /usr/local/sbin/iob-watchdog.sh
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now iob.timer iob-clean.timer iob-watchdog.timer
```

`iob.service` is deliberately **not** enabled — `iob.timer` starts it, which is
why the unit has no `[Install]` section.

| Unit | Schedule |
|---|---|
| `iob.timer` | Mon–Fri 09:10 → starts the algo |
| `iob-clean.timer` | Mon 09:05 → prunes logs |
| `iob-watchdog.timer` | every 5 min → restarts on stale log |

## Step 8 — nginx and TLS

```bash
sudo cp /opt/algo/kotak_zerodha_prasad_patankar/deploy/nginx-iob.conf /etc/nginx/sites-available/iob
```

Edit `server_name` in that file, then:

```bash
sudo ln -s /etc/nginx/sites-available/iob /etc/nginx/sites-enabled/
```

```bash
sudo nginx -t
```

```bash
sudo systemctl reload nginx
```

```bash
sudo certbot --nginx -d iob.yourdomain.com
```

The dashboard binds `127.0.0.1:7070` at `main.py:126` — loopback only, so it is
unreachable except through nginx. **Do not change it to `0.0.0.0`.** nginx
routes by `Host`, so this coexists with the existing site on port 80. Expose
only 80/443 in the firewall.

Proxy at the subdomain **root**, never a subpath — `dashboard.py` emits
root-relative URLs only (`redirect("/")`, `action="/toggle"`), so a `/iob/`
mount would post to `/toggle` and lose the prefix.

TLS is not optional here: the dashboard uses HTTP Basic auth on every request
including the 60-second meta-refresh, and that password gates lot sizes and
strategy toggles on a live account.

Port 7070 is hardcoded. A second IOB user on this box would collide — at that
point make it a `user_selection.csv` key rather than editing `main.py` per user.

## Step 9 — Verify

```bash
systemctl list-timers 'iob*'
```

First manual run, outside market hours:

```bash
sudo systemctl start iob
```

```bash
journalctl -u iob -f
```

Expect `Kotak login successful` and `Zerodha login successful`, then
`Waiting for market open..` if it is before 09:15:30.

### Confirm you can still manage the files

After that first run has created `logs/`, check that the setgid + `UMask=0002`
combination held. This matters because the unit also sets
`RestrictSUIDSGID=yes`, and it is worth confirming rather than assuming:

```bash
ls -ld /opt/algo/kotak_zerodha_prasad_patankar /opt/algo/kotak_zerodha_prasad_patankar/logs
```

Both should show group `iob` and a group-writable mode — `drwxrws---`. The `s`
is the setgid bit.

Then save any change from the dashboard UI and re-check the config file:

```bash
ls -l /opt/algo/kotak_zerodha_prasad_patankar/user_selection.csv
```

Expect `-rw-rw----  iob iob`. If it comes back `-rw-r-----`, the umask did not
apply and your next SFTP edit will be refused — fix with:

```bash
sudo chmod 660 /opt/algo/kotak_zerodha_prasad_patankar/user_selection.csv
```

---

## Day-2 operations

### Update

```bash
sudo -u iob -H git -C /opt/algo pull
```

**Run git as `iob`, not as yourself.** A plain `git pull` from your own account
fails with:

```
error: cannot open '.git/FETCH_HEAD': Permission denied
```

`/opt/algo` is created in step 3 at mode 755 owned by `iob`, and step 5's
`2770` applies only to the `kotak_zerodha_prasad_patankar` subfolder — so
`/opt/algo/.git` is group-readable but not group-writable, and `git pull` must
write `FETCH_HEAD` there. Being in the `iob` group is not enough.

Widening `.git` is the wrong fix anyway. Git 2.35.2+ refuses to operate on a
repo owned by another user (`detected dubious ownership`), so you would also
need a `safe.directory` exception — and any file the pull updated would land
owned by *you* inside a tree the service has to be able to write. Running the
pull as `iob` avoids all of it.

```bash
sudo systemctl restart iob
```

A pull alone changes nothing — Python has already imported its modules. Only
`user_selection.csv` hot-reloads (`config.py:207`, on mtime). Code changes need
the restart.

Restarting mid-session is survivable by design — `reconcile()` exists precisely
to rebuild state after a VPS failure — but it costs a fresh broker login and
scrip-master load. Prefer outside 09:15–15:30.

### Control

```bash
sudo systemctl stop iob
```

```bash
journalctl -u iob --since today
```

```bash
journalctl -t iob-watchdog --since today
```

### Dashboard

`https://iob.yourdomain.com`, log in with `CLIENT_NAME` / `DASHBOARD_PASS`.
Config edits are blocked 09:09–15:20 (`dashboard.py:35-36`).

---

## Lifecycle — read before changing `Restart=`

`main.py` is a **daily one-shot, not a daemon.** It ends at `main.py:1025` with
`os._exit(0)` at ~23:16, after the commodity session.

So `Restart=always` is wrong: every relaunch would find
`now >= phase1_exit_time`, perform a full Kotak + Zerodha login, load both
scrip masters, fall through both loops and exit within seconds — hammering
broker login endpoints all night. `Restart=on-failure` is required.

That depends on a patch already applied in this folder. The three login-failure
paths in `utility/common_functions.py` (lines 54, 90, 121) originally called
`sys.exit(0)`, which made a failed broker login indistinguishable from a clean
end-of-day. They now `sys.exit(1)`. **If those ever revert to 0, a flaky broker
at 09:10 means the algo silently skips the trading day and nothing alerts you.**

`Restart=on-failure` covers crashes but not hangs — a process stuck on a wedged
socket stays `active`. That is what `iob-watchdog.timer` is for: it ports
`monitor.sh`'s stale-log check (4 minutes), the one piece systemd does not
replace on its own.

## What replaced what

| Old | New |
|---|---|
| `IOB.bin` + pyinstaller | `/opt/iob-venv/bin/python main.py` |
| `monitor.sh` (start + restart loop) | `Restart=on-failure` + `iob.timer` |
| `monitor.sh` (stale-log check) | `iob-watchdog.timer` |
| `cleanall.sh` + cron | `iob-clean.timer` |
| `stopeverything.sh` (`pkill -f`) | `systemctl stop iob` |
| `setup.sh` (`crontab -`) | `systemctl enable` |
| `logs/output.log` | `journalctl -u iob` |
| manual file copy | `git -C /opt/algo pull` |
