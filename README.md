# TT-BURNIN

Tenstorrent Burnin (TT-Burnin) is a command line utility to run a high power consumption workload on TT devices.

## Official Repository

[https://github.com/tenstorrent/tt-burnin/](https://github.com/tenstorrent/tt-burnin/)

## Getting started
Build and editing instruction are as follows -

### Building from Git

After cloning the repo, install and source rust for the luwen library
```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```
Upgrade pip to the latest and install tt-burnin
```
pip3 install --upgrade pip
pip3 install .
```
### Optional - for TT-Tools developers

Generate and source a python3 environment
```
python3 -m venv .venv
source .venv/bin/activate
pip3 install --upgrade pip
```
For users who would like to edit the code without re-building, install burnin in editable mode.
```
pip3 install --editable .
```

# Usage

Command line arguments
```
usage: tt-burnin [-h] [-v] [--reset_file reset_config.json] [--no-reset]
                 [--no-check] [--idle] [--ramp-step CORES]
                 [--ramp-interval SECONDS] [--max-cores CORES]
                 [--duration SECONDS] [--aiclk-limit MHZ] [--tdp-limit WATTS]
                 [--board-power-limit WATTS]
                 [--enable-gddr] [--enable-l2cpu]
                 [--max-board-power WATTS]
                 [--max-total-board-power WATTS]
```

## Getting Help!

Running tt-burnin with the ```-h, --help``` flag should bring up something that looks like this

```
usage: tt-burnin [-h] [-v] [--reset_file reset_config.json] [--no-reset]
                 [--no-check] [--idle] [--ramp-step CORES]
                 [--ramp-interval SECONDS] [--max-cores CORES]
                 [--duration SECONDS] [--aiclk-limit MHZ] [--tdp-limit WATTS]
                 [--board-power-limit WATTS]
                 [--enable-gddr] [--enable-l2cpu]
                 [--max-board-power WATTS]
                 [--max-total-board-power WATTS]

Tenstorrent Burnin (TT-Burnin) is a command line utility to run a high power consumption workload on TT devices.

optional arguments:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --reset_file reset_config.json
                        Provide a custom reset json file for the host.Generate a default reset json file with the -g option with tt-smi.
  --ramp-step CORES     Release this many Tensix cores per ramp step (default: 1)
  --ramp-interval SECONDS
                        Wait this long after each ramp step (default: 1.0)
  --max-cores CORES     Run on at most this many Tensix cores per device
  --duration SECONDS    Stop automatically this long after the ramp completes
  --aiclk-limit MHZ     Temporarily limit Blackhole AICLK; restored on exit
  --tdp-limit WATTS     Temporarily set Blackhole ASIC TDP; restored on exit
  --board-power-limit WATTS
                        Apply and verify the firmware board-power policy after reset
  --enable-gddr         Wake Blackhole GDDR after the core ramp
  --enable-l2cpu        Enable Blackhole L2CPU clocks after the core ramp
  --max-board-power WATTS
                        Stop if any local board reaches this measured input power
  --max-total-board-power WATTS
                        Stop if the local boards reach this combined input power
```

## Running tt-burnin

After building run `tt-burnin` to run the program. 

TT-Burnin performs the following steps when running:
1. Reset the boards on the host to get them into a known good state
2. Start boards sequentially and release Tensix cores in small batches
3. Output a realtime telemetry command line widget to monitor the devices
4. After user hits "enter" to stop the workload, another reset is performed to bring the boards back to known good state

### Safer staged tests

Core activation is staged by default: one core is released per second, and boards
are started sequentially. A bounded smoke test can explicitly limit the core count
and duration:

```
tt-burnin --max-cores 1 --ramp-step 1 --ramp-interval 5 --duration 3
```

On Blackhole, TT-Burnin keeps GDDR/MRISC and L2CPU powered down because the BHPV
workload runs on Tensix with local L1/NOC traffic. Only Tensix and max AICLK are
requested, avoiding the unrelated power step caused by the legacy `high` profile.
For finer startup control, `--aiclk-limit` applies the firmware's temporary,
device-validated host ceiling and restores the previous ceiling during cleanup:

```
tt-burnin --aiclk-limit 900 --max-cores 1 --duration 3
```

`--tdp-limit` similarly applies a temporary firmware-validated ASIC limit and
restores the previous runtime value during cleanup. It is a reactive control and
does not prevent short power excursions.

`--board-power-limit` is applied after TT-Burnin's initial device reset and
verified through firmware telemetry before any workload core is released. This
prevents the reset from silently discarding a policy configured before TT-Burnin
started:

```
tt-burnin --board-power-limit 300 --aiclk-limit 1350 --duration 180
```

Blackhole runs also require firmware runtime-power status to report policy-ready,
strict enforcement, and a fresh input-power sample before TT-Burnin enables a
workload. A latched firmware containment trip aborts the workload immediately.
The end-to-end acceptance harness deliberately trips a low limit, proves that the
boot ID and every board identity survived, resets by stable PCI BDF, and requires
the 300 W policy to return ready/strict/fresh before it passes:

```
tt-burnin-containment-test --run
```

It persists progress in `/var/tmp/tt-burnin-containment-acceptance.json`. After
an interrupted run or suspected reboot, check that record without starting a
workload using `tt-burnin-containment-test --check-previous`.

For board-power testing beyond the BHPV workload itself, `--enable-gddr` and
`--enable-l2cpu` add those otherwise-unused domains only after every selected
Tensix core has completed its staged start.

The optional telemetry cutoffs take values chosen for the actual cards and host,
so TT-Burnin does not invent a universal wattage limit:

```
tt-burnin --max-board-power <per-card-watts> \
  --max-total-board-power <host-total-watts>
```

These cutoffs are reactive software checks. They stop the workload and return the
devices to low power after telemetry reaches a cutoff, but they cannot prevent a
short transient or substitute for adequate board and host power delivery. Use
`--ramp-step 0` only when the legacy simultaneous core release is intentional.
Pressing Enter also stops immediately between ramp samples; it is not deferred
until every core has started.

At exit, TT-Burnin reports both the peak sampled board input power across startup
and the average/range sampled during the sustained post-ramp dwell.

A full run of burnin should look like - 

```
$ tt-burnin

 Detected Chips: 3
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Pci Dev ID ┃ Board Type ┃ Device Series ┃ Board Number    ┃ Coordinates  ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ 0          │ grayskull  │ e75           │ 100007311523010 │ N/A          │
│ 1          │ wormhole   │ n300 L        │ 10001451170801d │ [0, 0, 0, 0] │
│ N/A        │ wormhole   │ n300 R        │ 10001451170801d │ [1, 0, 0, 0] │
└────────────┴────────────┴───────────────┴─────────────────┴──────────────┘
 Resetting devices on host... 
 Re-initializing boards after reset.... 
 Detected Chips: 3

 Starting TT-Burnin workload on all boards. WARNING: Opening SMI might cause unexpected behavior 
                                                                                                                                                               
┏━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ ID ┃ Core Voltage (V) ┃ Core Current (A) ┃ AICLK (MHz) ┃ Power (W)     ┃ Core Temp (°C) ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ 0  │ 0.74 / 0.84      │  73.0 / 170.0    │  653 / 1000 │  54.0 /  56.0 │ 41.3 / 75.0    │
│ 1  │ 0.75 / 0.95      │ 110.0 / 160.0    │  872 / 1000 │  84.0 /  85.0 │ 37.9 / 75.0    │
│ 2  │ 0.75 / 0.95      │ 110.0 / 160.0    │  885 / 1000 │  85.0 /  85.0 │ 33.4 / 75.0    │
└────┴──────────────────┴──────────────────┴─────────────┴───────────────┴────────────────┘
 Press Enter to STOP TT-Burnin on all boards...

 Stopping TT-Burnin workload on all boards. 

 Resetting devices on host... 
 Re-initializing boards after reset.... 
 Detected Chips: 3
```

## Supported products

tt-burnin can be used with Wormhole and Blackhole products. The last version that supported Grayskull products was [v0.2.5](https://github.com/tenstorrent/tt-burnin/releases/tag/v0.2.5).

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
