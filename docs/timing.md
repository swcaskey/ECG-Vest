# Two-board timing and bench acceptance

## Configuration

References: [MAX30003 Rev 3](01_MAX30003_Datasheet.pdf), pp19, 26, 28-31,
33-37 and 41-43; [WING schematic](02_MAX30003WING_Guide_and_Schematic.pdf), p5.
Nano ESP32 uses the pinned core and tool versions in `toolchain.json`.

| Physical board | CS / INTB | Current electrode connection |
| --- | --- | --- |
| 1 | D10 / D2 | III = LL - LA; sleeve unused |
| 2 | D9 / D3 | I = LA - RA; sleeve supplies RL bias |

Shared SPI is D13 SCLK, D11 MOSI, D12 MISO, mode 0, MSB first, 1 MHz.
Both boards share 3V3 and circuit ground. Only write-only SYNCH selects both chips;
reads select one. Independent onboard FCLK oscillators remain unchanged.
Do not join the two VCM outputs or connect VCM to circuit ground.

| Register | Value |
| --- | --- |
| CNFG_GEN 0x10 | 0x080004: ECG on; internal bias/lead-off off |
| CNFG_EMUX 0x14 | 0x3B0000 internal isolated test; 0 external |
| CNFG_CAL 0x12 | 0x704800 bipolar 1 Hz test; 0 external |
| CNFG_ECG 0x15 | 0x805000: 128 sps, gain20, HP/LP enabled |
| EN_INT 0x02 | 0x800001: FIFO-threshold EINT only, active-low CMOS |
| EN_INT2 0x03 | 0: unused |
| MNGR_INT 0x04 | 0x380004: eight unread samples assert EINT |

## Sample association and clock fitting

EINT asserts when eight unread FIFO entries are available and stays asserted
until reads reduce that count below eight. Its falling edge therefore identifies
the eighth unread entry: `first_unread_sequence + 7`. The ISR records common
`esp_timer_get_time()` time and the protected FIFO-read count; no SPI, allocation
or serial work occurs in the ISR. This replaces the older SAMP-first-edge guess.

Main verifies the interrupt and threshold batch before accepting the anchor.
Edges during FIFO reads, duplicate/missing edges and queue losses invalidate
timing. Backlog draining does not invent anchors. Measured ECG continues through
timing-only faults; FIFO, PLL, transport and data faults restart both boards
in a new epoch. Invalid timing clears calculated II. USB reconnect requires
a new mode selection.

An associated A record contains a one-based FIFO sample position, not an IRQ
callback count. For position n the host estimates:

    t_i(n) = fit_i(n + 1) - 4906 * fitted_sample_period_i / 256

4906 master-clock ticks is the configured LPF delay (Table 4, p19), nominally
149.719 ms. Matching filters remove their common nominal delay from relative
alignment; this is not an absolute electrode-event timestamp or a complete
frequency-dependent HPF delay model.

Centered robust fitting tracks each independent sample clock in common MCU time.
Readiness requires enough anchors/span, a plausible rate and bounded residual.
The firmware association flag describes a known FIFO index, not a precision
certificate. Legacy recordings with association 0 remain unvalidated; an optional
exploratory override never changes their recorded association.

II is computed on a common 128 Hz grid only where both streams bracket a point.
Interpolation does not cross missing/settling positions, epochs or resets and
does not extrapolate. Raw values are neither normalized nor polarity-corrected
before addition. Linear interpolation error increases with frequency: its maximum
sine midpoint attenuation is about 0.75% at 5 Hz and 23% at 28.35 Hz. Timing precision
and amplitude reconstruction are separate acceptance measurements.

## E2 records and recording provenance

ASCII newline-terminated records, 115200 baud. s=session, e=epoch, b=physical board.

    E2,C,s,e,b,mode,chip_hex,launch_us,ratio,latency_ticks,association_valid
    E2,R,s,e,mode
    E2,D,s,e,b,first_sequence,value:tag;value:tag;...
    E2,A,s,e,b,timing_position,esp_timestamp_us
    E2,S,s,e,b,status_hex,irq_count,irq_lost,tx_lost
    E2,E,s,e,b,event_code,esp_timestamp_us
    E2,T,s,e,b,irq_count,esp_timestamp_us,interval_us,fifo_read_count,irq_lost

Associated FIFO anchors use ratio 1 and `timing_position = sample_index + 1`.
Legacy association 0 A records retain their SAMP-counter interpretation and ratio.
S/T IRQ callback counts are diagnostics, not A sample positions. T is bounded
context around a timing fault and never repairs or substitutes an anchor.

D preserves signed18-bit ECG voltage and ETAG. Tags0/2 are valid voltage;
1/3 consume time positions but display as gaps. Empty, overflow and reserved
tags are never voltage samples. C/C/R requires both matching configurations
and successful identity/register readback. Parser errors, losses, stale clocks
or data and incomplete recording endings remain explicit capture failures.

CSV retains exact wire bytes plus options metadata. New captures save the
physical-to-logical lead mapping and bias board. Replay restores those values;
recordings lacking mapping show neutral board labels unless explicitly overridden.
Historical files are never silently assigned today's swapped labels.

## Bench acceptance: 2026-11-16

Format, Check Syntax, all 72 unit tests and the pinned Nano ESP32 Build passed.
The FIFO-anchor firmware was uploaded with permission. The working plug layout
above was retained; generator B supplied 5 Hz while A supplied 3 Hz, then 11 Hz.
Both used 20 mVpp, +650 mV offset and High Z, with no person connected.

| Capture | Duration | Valid samples, B1 / B2 | Result |
| --- | --- | --- | --- |
| `fifo-internal-final-20261116T224813Z.csv` | 20 s | 2568 / 2560 | Both internal periods 128 samples |
| `fifo-external-3hz-final-20261116T224912Z.csv` | 30 s | 3848 / 3840 | Worst steady 2 s-window relative delay 0.467 ms |
| `fifo-external-11hz-long-20261116T225100Z.csv` | 600 s | 76800 / 76808 | All 299 steady windows below 1 ms; worst 0.533 ms |
| `fifo-external-11hz-reopen-20261116T230131Z.csv` | 20 s | 2568 / 2560 | Fresh start healthy; worst steady delay 0.508 ms |

All four captures and shared-task replays had no missing positions, acquisition
faults, parser errors or incomplete endings. Calculated II worked without the
exploratory override. The eight-sample difference at a recording boundary is
ordinary staggered batching, not a missing-data event. Raw CSVs and analysis
JSON/PNGs are saved in ignored `recordings/`.

Timing figures compare the sign-corrected shared generator component after the
first 2 s of filter startup. They measure this setup's relative waveform delay,
including analog-path differences; they are not absolute timestamp calibration
or proof across the entire filter passband. Earlier SAMP recordings retain
their original unvalidated association and any recorded faults.

Human-connected testing requires a separately reviewed electrical safety setup;
direct USB is not patient isolation. Deliberate hardware fault injection and
body-connected performance were not part of this bench acceptance.
