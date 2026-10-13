# BulkEthernetConfigUploader

Loads one BALTECH reader configuration (`.bec`) onto many Ethernet readers in parallel.
The readers are listed in a CSV file (IP address, optionally with port). Each reader is
updated over an encrypted BRP-over-TCP session that is authenticated with the transport
certificate package in `certs/`.

## Installation (Windows)

Run `BulkEthernetConfigUploader-<version>-setup.exe`. It installs for the current user
without administrator rights into `%LOCALAPPDATA%\Programs\BulkEthernetConfigUploader` and
adds a Start menu entry. The program folder also holds `becu-cli.exe` for scripts, the
certificate package in `certs\`, and after the first run `reports\` and `logs\`.

## Start from the sources (Windows)

Install [uv](https://docs.astral.sh/uv/) once (installer from that page or `winget install
astral-sh.uv`). Put the sources into a short path such as `C:\BECU` (the Python launcher
fails in folder paths longer than about 150 characters) and double-click `Start.cmd`. On
the first start uv downloads Python 3.13 and the packages, once per PC, and creates the
project environment `.venv` in the program folder; this needs internet access and about a
minute. Later starts take a few seconds. uv uses the Python 3.13 it manages itself; a
Python installed from python.org or the Microsoft Store is used only if uv cannot download.

In the window:

1. Select the CSV file. A header line is detected and skipped automatically. Select the
   column with the addresses (`a.b.c.d` or `a.b.c.d:port`). If the addresses carry no
   port, you can select a port column; without one, port 2939 is used.
2. Select the `.bec` configuration file.
3. The certificate package from `certs\` is preselected; "Change..." selects another one.
4. Options: "Max. parallel readers" limits how many readers are in progress at the same
   time ("all" = no limit). "Start up to N readers per second" (default 10) limits the
   connection burst at the start; 1000 starts all readers nearly at once. "Automatic
   retries for temporary errors" repeats a reader, 5 seconds apart, when it is unreachable
   or busy, the connection is lost during the upload, or it does not answer after the
   reboot; a rejected certificate, access denied, a refused configuration or a wrong
   configuration after the reboot are not retried.
5. "Command line for automation" shows the command line call that performs the same
   update with the current settings; "Copy" puts it on the clipboard. Use it in a script
   (see Command line below).
6. "Estimated duration" is a rough figure for the whole update from the reader count, the
   start rate, the parallel limit and the configuration size; retries and errors are not
   included. Start. The counters show pending, handshaking, updating, rebooting, updated
   and errors. Readers that end with an error are listed; "Retry errors" runs all of them
   again.

Every run writes a report CSV to `reports\` and a log to `logs\becu.log` in the program
folder.

## Command line

Installed program: `becu-cli.exe` in the program folder. From the sources:
`.venv\Scripts\python.exe -m becu.cli` in the program folder, or `uv run python -m becu.cli`.

```
becu-cli.exe --csv readers.csv --config project.bec [options]
becu-cli.exe --address 192.168.1.10:2939 --config project.bec
becu-cli.exe --csv readers.csv --list-columns
```

Run with `--help` for all options. Columns are given as 0-based indices (`--ip-column 1`
is the second column); `--list-columns` shows them. The exit code is 0 when every reader
was updated, 1 when at least one reader ended with an error, 2 for a usage error. The GUI
field "Command line for automation" produces the exact call, for the installed program or
for the sources.

## Development

```
uv sync
uv run pytest
uv run inv --list
uv run inv installer        # builds dist/BulkEthernetConfigUploader-<version>-setup.exe
```

`inv installer` bundles the GUI and the CLI with PyInstaller into `dist/BulkEthernetConfigUploader/`
(certificate package included) and wraps that folder with Inno Setup into the per-user installer.
Inno Setup must be installed on the build PC: `winget install -e --id JRSoftware.InnoSetup`.

See `certs/README.md` for the certificate package.
Timing facts for the target readers (Kofax Ethernet card reader, firmware 1096 2.40.03): PKI handshake
about 21 s, upload about 16 ms per configuration byte, reboot about 5 s, one TCP session
per reader at a time. The duration estimate in the window and in the CLI summary uses these
figures.
