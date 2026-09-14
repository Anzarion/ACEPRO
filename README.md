# ACEPRO — Voron fork

A Klipper driver for the **Anycubic ACE Pro (Gen 1)** on a **Voron 2.4 R2 350 mm**.

This is a personal fork of [Kobra-S1/ACEPRO](https://github.com/Kobra-S1/ACEPRO),
maintained for exactly one machine. It is **not** a general-purpose distribution:
support for the Kobra 3 / Kobra S1 / KS1M printers, the KlipperScreen panel and
the standalone web dashboard have all been removed, because none of them run
here. If you have one of those printers, use upstream — it is better maintained
for your case than this fork will ever be.

## Relationship to upstream

The fork is deliberately decoupled. Changes are **imported selectively** from
upstream when they fix a real problem on this machine; there is no attempt to
stay mergeable. One feature went the other way: the `cont_assist_time` tangle
detector was contributed upstream and merged as `5c31769` (PR #18), so upstream's
later rework of it builds on code from this fork — which is why that rework
could be imported cleanly.

`upstream` remains configured as a remote for exactly that purpose:

```bash
git remote -v          # origin = this fork, upstream = Kobra-S1/ACEPRO
git fetch upstream
git log --oneline voron..upstream/dev     # what is available to import
```

## Hardware

| | |
|---|---|
| Printer | Voron 2.4 R2, 350 mm |
| Changer | 1× ACE Pro Gen 1 (JSON protocol, 115200 baud) |
| Bowden | 2100 mm ACE park position → toolhead sensor |
| Toolhead sensor | `filament_toolhead` (pre-extruder, `filament_switch_sensor`) |
| Post-extruder sensor | `filament_nozzle` |
| Return module | `filament_runout_rdm` (`filament_tracker`, encoder + switch) |
| Screen | helixscreen |
| Spool tracking | FilaMan via Moonraker, Spoolman-compatible |

## What this fork adds

Features that exist here and not upstream, or that behave differently:

- **Post-extruder sensor.** `filament_runout_sensor_name_toolhead` is the
  toolhead sensor and `..._nozzle` is a third, post-extruder sensor. Upstream
  uses `..._nozzle` for the toolhead sensor — the same key, opposite meaning.
  Anything imported from upstream has to be checked against this.
- **`ace_debug`.** Routes diagnostic output (GET_INFO, monitor status, the
  `ACE_DEBUG` command) to the console instead of only `klippy.log`.
- **Depleted-spool recovery.** When a spool runs out *at the ACE*, its feed
  gears have nothing left to grip, so no retract can clear the bowden.
  `flush_forward_until_clear()` pushes the orphaned filament out through the
  nozzle over the bucket instead — at print end and, more importantly, on a
  mid-print toolchange, which would otherwise abort the print.
- **FilaMan consumption tracking.** T-macros are registered dynamically in
  `manager.register_tool_macros()` so `_SET_SPOOL_BY_TOOL` runs *after* the
  physical toolchange. The static macros upstream ships have the opposite
  order, which loses ~27 mm per toolchange to the high-water-mark tracker.
  `CLEAR_ACTIVE_SPOOL` is called after unloads.
- **`custom_name` / OrcaSlicer lane sync.**
- **`filament_tracker`** module (adopted from the Kobra-S1 Klipper fork), used
  for the return-module sensor.
- **`smart_unload` rework:** move to the bucket before retracting, disable feed
  assist *before* `CUT_TIP` rather than after, nozzle wipe after unloading.

## Install

Nothing is copied — the driver is symlinked into Klipper, so `git pull` plus a
Klipper restart is the entire update procedure:

```
~/klipper/klippy/extras/ace              -> ~/ACEPRO/extras/ace
~/klipper/klippy/extras/filament_tracker.py
~/klipper/klippy/extras/temperature_ace.py
~/klipper/klippy/extras/virtual_pins.py
~/moonraker/moonraker/components/ace_status.py
                                         -> ~/ACEPRO/ace_status_integration/moonraker/ace_status.py
```

Deploy:

```bash
ssh tobi@voron.local 'cd ~/ACEPRO && git pull'
# then RESTART in Klipper
```

## Configuration

`config/voron/` is a **record of what runs on the machine**, not a template.
The printer reads its own hand-maintained copies in `~/printer_data/config/`;
the files here are kept byte-identical to those so the repo can be read instead
of the machine. When they diverge, the machine is right — re-sync the repo, not
the printer.

| File | |
|---|---|
| `config/voron/acepro.cfg` | entry point, sensor declarations |
| `config/voron/acepro_setting.cfg` | the `[ace]` section |
| `config/voron/acepro_macros.cfg` | ACE macros |
| `config/voron/acepro_printer_macros.cfg` | pause/resume integration |
| `config/spoolman_logic.cfg` | FilaMan/Spoolman, RFID→ID mapping |

`config/spoolman_logic.cfg` deliberately contains **no** `[gcode_macro T0..T7]`
— the driver registers them (see above). If they ever reappear, the tracking fix
is silently dead: `register_tool_macros()` skips auto-registration per tool when
a macro of that name already exists.

## Development

```powershell
python -m pytest tests/ -q
```

Tests must run through PowerShell on Windows. Commit messages are in English.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — module layout and data flow
- [PROTOCOL.md](PROTOCOL.md) — ACE1/ACE2 wire protocol
- [CONNECTION_SUPERVISION.md](CONNECTION_SUPERVISION.md) — connection health handling

## License

See [LICENSE](LICENSE). Upstream project by Kobra-S1; this fork by Anzarion.
