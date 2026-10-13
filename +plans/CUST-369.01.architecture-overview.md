# BulkEthernetConfigUploader: architecture overview (CUST-369)

Status: research + architecture, decisions integrated (section 0).
No product code yet. Probe scripts were run against a test reader (read-only probes, then two
BEC uploads; the original configuration was restored and verified).

## 0. Decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Config file | One legacy `.bec` for all readers. ConfigID is read from the file (display, skip, verify), not entered by the user. |
| D2 | Pre-scan / verify without PKI | Keep, with one checkbox to disable both (default on). FW 2.40.03 never gates the info commands, see 2.6. |
| D3 | Certificate | The transport package `certs/PKI_AUTH_BALTECH_FOR_KOFAX_TRANSPORT.zip` is committed to this private repository and ships in the deliverable; excluded from the MIT license (D6). |
| D4 | UI | wxPython. |
| D5 | Deliverable | No executables in the repository. Two ways to run: from the sources with `Start.cmd` (uv installed on the PC; the first start downloads Python 3.13 and the packages, `.venv` in the program folder) and as installed program from the per-user installer that `invoke installer` builds (PyInstaller folder with GUI and CLI, Inno Setup, no administrator rights). No code signing. |
| D6 | License | MIT for the source. The certificate zip is excluded from the license and ships under a confidentiality notice, see 3.7. |
| D7 | Parallelism | One worker per reader with a ramped start. UI: two numbers only, "Max. parallel readers" (default: all) and "Start rate" (default 10/s). A pool is the same thing with a lower maximum, no mode switch. |
| D8 | Failures | Always report. Option "factory reset and retry on upload failure" (default off). |
| D9 | Tests | Spike on the test reader done (2.4). Mock reader fleet (1800 instances) is in scope, see 3.6. |
| D10 | SDK | baltech-sdk 3.19.4, `Brp(Tcp("ip:port"), crypto=PKI(zip))`. Port parsing verified in the C source, on the device (2939) and with a local listener (12345); see 2.1. |
| D11 | Target and language | Windows x64 customer PC (mirror built for cp313 win_amd64). No VLAN limits assumed. UI and README in American English. |
| D12 | Timing numbers | The measured durations in section 1 and 2.4 are estimates only. The run does not use them: command timeouts are the SDK defaults, the reboot wait is a generous poll window. They appear in the code only as the constants of the duration estimate (`becu/core/estimate.py`) and as default delays of the mock fleet. |
| D13 | Progress display | No estimated time left during the run. Hard numbers only: pending, handshaking, updating, rebooting, updated, errors, plus total and elapsed time. Before the start, GUI and CLI show a rough estimate of the whole duration (reader count, start rate, parallel limit, configuration size) with its assumptions; retries and errors are excluded. Readers that already carry the target config count as updated with the per-reader status "already up to date". |
| D14 | Reader list | No table of all readers in the UI. The UI shows an errors list only (IP, port, serial, error, retries) with "Retry errors". The complete per-reader result (all rows, all columns) goes to the CSV report and the log. |
| D15 | Automation | The GUI shows the `python -m becu.cli` call equivalent to the current settings in a read-only field with a Copy button, so the customer can script the update. Paths are always quoted, columns are passed as 0-based indices, `--auto-confirm` replaces the confirmation prompt. `Start.cmd` reports its steps and closes two seconds after the GUI has started. |
| D16 | Stored settings | None. The GUI keeps nothing between sessions: every start shows the defaults (all readers in parallel, 10 starts per second, one retry, plain access on, no factory reset), the certificate from `certs/` preselected and no files from an earlier session. |

## 1. Context

Project CUST-369: Konica Minolta runs Kofax/Tungsten *Output
Manager* with up to **1800 Ethernet readers** per project. A server update factory-resets
every reader, which loses the project configuration. Baltech shall deliver a free tool
that runs in the readers' VLAN, takes a **list of IP addresses**, and reloads the project
configuration onto all readers **in a few minutes, in parallel**. The **source code goes to
the customer**; Baltech takes no further responsibility.

Facts established by research (SDK and firmware sources, project history) and by probes on
the test reader:

| Topic | Finding | Source |
|---|---|---|
| Reader | Kofax Ethernet reader on the BALTECH ID-engine XE platform, Kofax firmware **1096 v2.40.03** | firmware sources, probe |
| Protocol | BRP over TCP, reader is TCP server on port **2939**, raw BRP frames on the socket, **one connection at a time** (second connect: closed at first read or 1.5 s connect timeout) | brp_lib `tcpip_protocol.c`, probe |
| Config format | **BEC only**. Feature list has no `Bec2Upload`; no `fwupload/bf3_upload` in FW 2.40.03 | probe, firmware sources |
| Access | Level 0: info commands allowed, config write/reset need PKI at level 1 with the transport certificate; factory state trusts only the shared transport root | firmware sources, probe |
| PKI cost | Handshake **20.6-21.0 s measured** (4 samples); reader-side ECC, parallel across readers; reboot discards the session | probe |
| Upload | `CfgLoadPrepare(Default)` accepted; **~16-20 ms per payload byte** (121 B block ~2 s): 446 B config 6.8 s, 575 B config 9.2 s; switching between stock project configs needed no factory reset | probe |
| Reboot | BRP answers **4.85 s** after `CfgLoadFinish(WithReboot)`; TCP accept is immediate | probe |
| Total | **32.5 s and 34.8 s** per reader for small configs (connect + auth + upload + reboot + verify) | probe |
| Legal | Transport keys are not Kofax property; "not a production secret" (compiled into the IF Converter middleware) | CUST-352, IN-A-1074790423 |

## 2. Research results per requirement

### 2.1 "Latest Baltech SDK version 3, used until ToolSuite 5.05" — verified

| ToolSuite | baltech-sdk pin | Lock |
|---|---|---|
| 5.03.00 … 5.05.04 | `>=3.19.1,<4.0.0` | 3.19.4 |
| 5.06.00 | `>=4.0.0,<5.0.0` | 4.x |
| 5.06.02 / master | `>=5.2026.6.1,<6` + `brp-lib>=4.3.0` | 5.2026.x |

SDK 4.2026.02 **removed the PKI protocol and the Ethernet transport from brp_lib**.
The last release with both is **baltech-sdk 3.19.4** (PyPI,
brp_lib 3.19.04, `brp_lib.win32/win64.dll` bundled, `requires-python >=3.9,<4`, runs under
Python 3.13.16).

v3 Python API that we need (verified by introspection and on the device):

- `Brp(Tcp("ip:port"), crypto=PKI(Path(zip), session_timeout=0))`; `PKI` reads exactly
  the zip layout (`host_chain/*`, `reader_chain/*`, `keypair.key`, `settings.cfg`).
- `Sys_GetInfo`, `Sys_CfgGetId`, `Sys_GetBootStatus`, `Sys_GetFeatures`,
  `Sys_CfgLoadPrepare(AuthReq)`, `Sys_CfgLoadBlock(Version, Data)`,
  `Sys_CfgLoadFinish(FinalizeAction)`, `Sys_Reset`, `Sys_FactoryReset`.
- Port handling: the C function has no separate port parameter, but it splits
  `"a.b.c.d:port"` at the colon and passes the port to `getaddrinfo` (tcpip_protocol.c
  lines 87-102); without a port it uses 2939. Verified: `Tcp("127.0.0.1:12345")` connected
  to a local listener on 12345, `Tcp("127.0.0.1")` to 2939; both forms reached
  the test reader on port 2939. A known ToolSuite 5.05.x problem came from passing only the
  IP without the port; the claim that `brp_create_tcpip()` always connects to 2939 is
  imprecise. Fallback if
  ever needed: `Tcp(sock=socket)` with a socket connected by Python.
- TCP connect/round-trip timeout 1.5 s default, so dead IPs fail fast.
- DLL loaded with `ctypes.CDLL` (GIL released during calls); no mutable globals in the C
  library or the ctypes wrapper. One `Brp` stack per worker thread is safe.

Caveats: v3 is frozen; the Python `PKI` wrapper has no progress callback (the handshake
blocks inside `brp_open`); monitor HTML logs go to a fixed directory and stay off.
Precedent: the IF Converter middleware links brp_lib 3.19.4 for the same reason.
Rejected alternative: SDK 5 with a pure-Python TCP transport and PKI layer (would need a
port of internal crypto code with an open issue, not feasible in the time available).

### 2.2 CSV import with header detection and column selection — feasible

- Python stdlib `csv` + `csv.Sniffer` (delimiter `,`/`;`/tab, `has_header()`), UTF-8 with
  BOM and cp1252 fallback.
- Header detection is a heuristic (first row has no IPv4 in the column where the other
  rows have most). No override in the UI: a first row that is not an address is either a
  header (dropped) or an invalid row (skipped), so the outcome is the same. The UI shows
  "header row detected" / "no header row". The CLI keeps `--header/--no-header` and takes
  columns as 0-based indices (`--ip-column N`, `--port-column N`); the GUI names the columns
  of a file without header "Column 0..n-1", the same index.
- IP column dropdown; values `a.b.c.d` or `a.b.c.d:port`. If no value in the IP column
  carries a port, a second dropdown "Port column (optional, otherwise 2939)" appears. Port
  precedence: `IP:port` > port column > 2939 (changeable only via the CLI `--default-port`).
- Validation report before start: invalid rows, duplicates, empty cells, row count.

### 2.3 Encrypted connection with the transport certificate — verified on the device

- brp_lib v3's PKI layer consumes the zip directly; security level 1 from `settings.cfg`.
- Repo: `certs/README.md`, `certs/NOTICE.txt` and the zip itself (D3). `pki_package.py` checks the
  zip structure (host_chain, reader_chain, keypair.key, settings.cfg) when it loads the package.
- Runtime: the app loads `certs/*.zip` next to the sources; a custom path is possible in
  the CLI and in an advanced UI field. The UI shows CN and security level before start.

### 2.4 Python and many parallel connections — yes, measured

- One worker thread = one reader = one `Brp` stack; threads block in C socket calls with
  the GIL released. 1800 threads are fine on 64-bit Windows (stack is reserved, not
  committed; brp_lib needs a few KB per stack; the ephemeral port range has 16k ports).
- asyncio is not usable with the v3 C transport (blocking I/O); multiprocessing is not
  needed.
- Per-reader time, measured: connect 0.01 s + handshake 21 s + upload + reboot 4.9 s +
  verify 0.1 s. No second handshake is needed: verification uses `Sys.CfgGetId` at level 0
  after the reboot. Only the optional factory-reset-and-retry path (D8) pays a second
  handshake.
- Upload time is reader-side work (AES decrypt + flash write per block on the AVR): about
  1.8 s per 121-byte block, 16-20 ms per payload byte. The `.bec` file is XML with hex text,
  so the file is about 3.7× the payload (1646 B file = 446 B payload). Expected customer
  files of 2-5 KB:

| `.bec` file | Payload | Blocks | Upload | Per reader |
|---|---|---|---|---|
| 1.6 KB (measured) | 446 B | 5 | 6.8 s | 32.5 s |
| 2.1 KB (measured) | 575 B | 6 | 9.2 s | 34.8 s |
| 5 KB | ~1.35 KB | ~12 | ~22 s | ~48 s |
| 10 KB | ~2.7 KB | ~23 | ~43 s | ~69 s |

| Max. parallel readers | Start rate | 1800 readers, 35 s each | 1800 readers, 48 s each |
|---|---|---|---|
| 250 | none | ≈ **4.2 min** (+ retries) | ≈ 5.8 min |
| 600 | none | ≈ 1.8 min | ≈ 2.4 min |
| all (1800) | 50/s | 36 s ramp + 35 s ≈ **1.2 min** (+ retries) | ≈ 1.4 min |

- The ramp protects the network and the host (connect bursts, ARP). The reader side
  parallelizes perfectly because each reader does its own ECC and flash writes.
- Each reader accepts one session, so the tool never opens two sessions to one IP; the
  pre-scan runs on a plain connection that is closed before the PKI session opens. A
  reader that is busy (for example the Output Manager is still connected) fails fast and
  goes to the retry queue.

### 2.5 UI with progress, ETA, logging — feasible

- Layering: `core` library → `cli` (argparse, for tests and automation) → `gui` (wxPython).
  `Start.cmd` starts the GUI; the CLI is `python -m becu.cli` inside the environment.
- Parallelism controls (D7): "Max. parallel readers" (default all) and "Start up to N
  readers per second" (default 10, 1000 = nearly all at once). "Automatic retries for
  temporary errors" (default 1) with a tooltip that lists what is retried. No mode switch.
  `Start.cmd` launches the GUI with pythonw and closes its console window.
- Progress (D13, D14): one overall bar (updated + errors of total) and six counters:
  pending, handshaking, updating, rebooting, updated, errors; plus total and elapsed time.
  No estimated time left. Below it an errors list (IP, port, serial, error, retries) with
  the buttons "Retry errors", "Export report" and "Open log folder". No list of all
  readers; the CSV report holds every reader with state, config before/after and duration.
- Logging: `logging` hierarchy; `RotatingFileHandler` in `logs/` next to the app; a queue
  handler feeds the UI log panel (level selector DEBUG/INFO/WARNING/ERROR, filter by
  reader); per-run result report (CSV: IP, port, serial, firmware, config before/after,
  status, duration, error); brp_lib monitor logs off.

### 2.6 Level-0 access and the pre-scan (D2)

In FW 2.40.03 `dispatch/system/sdsys.c` checks access-condition bits only for
`Sys.Reset` (SYS_RESET), the IO-port commands, one HF command and the security reset.
`Sys.GetInfo`, `Sys.CfgGetId`, `Sys.GetBootStatus` and `Sys.GetFeatures` have no check,
so a configuration cannot lock them. The probe confirmed this on the reader with a project
configuration loaded, before and after the upload. The checkbox "Pre-scan and verify
without certificate" (default on) stays as a safety valve for other firmware; when it is
off, the tool skips the pre-scan and reports "not verified".

### 2.7 Config upload sequence (BEC) — verified on the device

`Sys.CfgLoadPrepare(AuthReq=Default)` → `Sys.CfgLoadBlock(Version=2 for the first block,
0 for the rest, Data)` per block → `Sys.CfgLoadFinish(FinalizeCfgLoadWithReboot)`.
On `ERR_INVALID_CMD` for Prepare fall back to blocks + `Sys.Reset`. On an error during
the blocks: `Sys.CfgLoadFinish(CancelCfgLoad)`. `.bec` = XML `<bec configId configName
customerKeySignature fileversion="2">` with `<block content="hex" version="…"/>` children
(as read by ToolSuite). D8: on upload failure with a configuration conflict
and the option enabled: `Sys.FactoryReset` → wait for reboot → new PKI session → upload
again, once.

## 3. Architecture

### 3.1 Repository layout (proposal)

```
BulkEthernetConfigUploader/
  pyproject.toml            uv, Python 3.13; deps: baltech-sdk>=3.19.4,<4, wxPython; dev: invoke, pytest, cryptography (mock)
  uv.lock
  Start.cmd                 uv sync with the installed uv, start GUI (see 3.4)
  tasks.py                  invoke: test, run, gui, installer
  installer/                PyInstaller spec, entry scripts, Inno Setup script (see 3.4)
  becu/                     Python package ("Bulk Ethernet Config Uploader")
    core/
      csv_import.py         sniff dialect/header, column model, IP[:port] parsing, validation
      bec_file.py           .bec parser -> blocks + config id/name
      pki_package.py        locate/validate zip, chain info
      reader_session.py     SDK v3 wrapper: connect plain / PKI, info, upload, reset, verify, timeouts
      job.py                per-reader state machine + result record
      scheduler.py          threads, ramp, cancel/stop, retries with backoff, state counters and events
      report.py             result CSV writer
      logsetup.py           file + queue handlers, levels
    cli.py                  python -m becu.cli
    gui/
      app.py, main_frame.py, error_list.py, log_panel.py
  mock/                     mock reader fleet for load tests (see 3.6), test PKI generator
  certs/                    README.md, NOTICE.txt, PKI_AUTH_BALTECH_FOR_KOFAX_TRANSPORT.zip
  tests/                    unit tests (csv, bec, scheduler with fake session, mock reader)
  +plans/
  LICENSE                   MIT
```

### 3.2 Per-reader state machine (`job.py`)

States match the UI counters (D13):

- PENDING: in the queue, not started.
- HANDSHAKING: plain TCP connect (1.5 s), pre-check (GetInfo, CfgGetId; if the target
  config is already loaded → UPDATED "already up to date"), connection closed, PKI
  session at level 1 (21 s).
- UPDATING: Prepare, n blocks, Finish with reboot.
- REBOOTING: poll plain connect from 4 s on, then verify with CfgGetId.
- UPDATED: target config verified (or "already up to date").
- ERROR(reason): unreachable, busy (connection refused/closed), PKI verify failed (reader
  locked to another CA), access denied, config conflict (security code/version), timeout
  mid-upload, verify mismatch.

Retries: configurable count and delay per error class; a retry puts the reader back to
PENDING. D8 factory-reset path for config conflicts.

### 3.3 Threads and events

UI thread; one scheduler thread that starts workers at the ramp rate up to the maximum;
one worker thread per reader. Workers emit events (state change, progress, log record)
into a `queue.Queue`; the GUI drains it via `wx.CallAfter` at 10 Hz. Stop = cooperative
flag; a running upload is cancelled with `CfgLoadFinish(Cancel)` before the last block, or
finished if the last block is already sent.

### 3.4 Deliverable (D5): sources with Start.cmd, and a per-user installer

The repository contains no binaries except the certificate package. From the sources, uv is
the prerequisite on the PC (installer from docs.astral.sh/uv or `winget install astral-sh.uv`).
`Start.cmd`:

1. Refuses folder paths above 150 characters (the venv launcher fails there) and a missing
   `uv` on the PATH, with a message that names the download page.
2. `uv sync --frozen` creates or repairs `.venv` in the program folder; uv prefers the
   Python 3.13 it manages (downloaded once per PC) and falls back to a system Python only
   when it cannot download. On failure the script deletes `.venv` and runs it once more.
3. Import check, then `pythonw -m becu.gui`; errors stay visible in the console and go to
   `logs\start.log`.

`invoke installer` builds the installer for end users: PyInstaller (`installer/becu.spec`)
bundles `BulkEthernetConfigUploader.exe` (windowed) and `becu-cli.exe` (console) with the
SDK DLLs and the certificate package into `dist/BulkEthernetConfigUploader/`; `project_root()`
resolves to the exe folder when frozen, so `certs/`, `logs/` and `reports/` sit next to the
program. Inno Setup (`installer/BulkEthernetConfigUploader.iss`, ISCC on the PATH or in its
default folder) wraps the folder into `dist/BulkEthernetConfigUploader-<version>-setup.exe`:
per-user installation under `%LOCALAPPDATA%\Programs`, no administrator rights, Start menu
entry, uninstaller, NOTICE shown before the installation. No code signing.

Handover: the repository and the installer.

### 3.5 Non-goals (first version)

SLP discovery, IP range scan, firmware upload, certificate provisioning (level 3), reader
lock/unlock, UDP introspection. The tool supports BEC files only (the target readers have
no other upload format).

### 3.6 Mock reader fleet for load tests (D9, in scope)

Goal: run the finished tool against 1800 simulated Kofax Ethernet readers on one PC.

- Python 3 asyncio TCP servers, one per reader, bound to loopback addresses
  127.0.x.y:2939 (Windows accepts the whole 127/8 range), listed in a generated CSV.
- Protocol: BRP command/response frames over TCP (BRP frame documentation and the SDK's
  C implementation as reference); single-connection behaviour like the real reader.
- Commands: Sys.GetInfo/CfgGetId/GetBootStatus/GetFeatures/CfgLoadPrepare/Block/Finish/
  Reset/FactoryReset; Pki.PfsGenKey/PfsAuthHostCert/PfsAuthRdrCert/Tunnel2 with
  `cryptography` (ECDH/ECDSA P-256, AES-128, HMAC) following the PKI protocol reference
  implementations of the reader side and the host side.
- Test PKI generated by the mock: root, software CA, host certificate, production CA,
  reader certificates (X.509 v3) and a `test_pki.zip` in the standard layout. The real
  transport zip and the production CA key are not used by the mock.
- Timing model from the measurements: handshake 21 s ± jitter, 16 ms per block byte,
  4.9 s reboot downtime; fault injection: unreachable, busy, locked (foreign CA), access
  denied, drop mid-upload, slow reader.
- Calibration: traces of handshake and upload against the test reader (Wireshark, port 2939).

### 3.7 License (D6)

`LICENSE` = MIT for everything in the repository. `certs/NOTICE.txt`: the certificate
package is not part of the licensed software, is provided for this installation only, and
must not be copied, published or redistributed. Because the zip is never in git and is
excluded from "the Software", MIT compatibility is not affected.

## 4. Milestones

1. **Spike (done):** plain and PKI sessions, two BEC uploads with reboot and
   verification on the test reader; numbers in section 1. Original configuration restored.
2. **Core + CLI (done):** package `becu` with `core/` (csv_import, bec_file,
   pki_package, session_api, reader_session on baltech-sdk 3.19.4, job, scheduler, report,
   logsetup) and `cli.py`; 58 unit tests with a fake reader fleet; `tasks.py` with
   `test` and `run`. Verified on the test reader: "already up to date" path, update to a
   second configuration (31 s), restore of the original (35 s), unreachable address with
   retry (exit code 1).
3. **GUI + deliverable (done):** wx main window (`becu/gui/`: inputs with
   automatic header detection and column selection, options, run panel with the six
   counters, errors list, log panel with level filter; no stored settings, every start
   begins with the defaults and the certificate from `certs/`; minimum window size derived
   from the layout and small enough for a 1366x768 screen), GUI smoke test with the
   fake fleet, GUI code path verified on the test reader. `Start.cmd` with the installed uv (first start downloads
   Python and packages, later 2 s, repair of a broken `.venv`), path-length check (launcher
   fails above ~150 characters). `invoke installer` builds the PyInstaller folder with GUI and CLI and the per-user
   installer (3.4).
4. **Mock fleet and load test:** 1800 mock readers, measure total runtime and retries.
5. **Later:** factory-reset-and-retry polish.

## 5. Open points

None. Target platform, VLAN assumption and language are decided (D11).

## 6. Risks

- SDK v3 is end-of-life; mitigated by the spike (works on the target firmware).
- 1800 simultaneous handshakes untested on real network hardware; the mock fleet tests
  the host side only. Keep maximum and ramp adjustable.
- Network effects at scale (ARP/IGMP issues and TCP-ACK problems behind firewalls seen in
  other installations).
- Short deadline: core, CLI and a plain GUI first; mock fleet and polish after.
