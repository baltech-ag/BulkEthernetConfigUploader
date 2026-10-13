# BulkEthernetConfigUploader

Loads one BALTECH reader configuration (`.bec`) onto many Ethernet readers in parallel.
The readers are listed in a CSV file (IP address, optionally with port). Each reader is
updated over an encrypted BRP-over-TCP session that is authenticated with the transport
certificate package in `certs/`.

Status: work in progress (milestone 2: core and command line).

## Command line

```
becu --csv readers.csv --config project.bec [options]
becu --address 192.168.1.10:2939 --config project.bec
becu --csv readers.csv --list-columns
```

Run `becu --help` for all options. The exit code is 0 when every reader was updated,
1 when at least one reader ended with an error, 2 for a usage error.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest
uv run becu --help
```

See `+plans/` for the architecture and `certs/README.md` for the certificate package.
