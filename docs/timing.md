# Two-board timing and physical acceptance

## References and configuration

References included in this directory:

- [MAX30003 datasheet](01_MAX30003_Datasheet.pdf), Rev 3: p19 Table 4; pp24-29 register/interrupt
  maps; p31 SYNCH/FIFO_RST; pp32-37 front-end configuration; pp41-43 FIFO tags.
- [MAX30003WING guide and schematic](02_MAX30003WING_Guide_and_Schematic.pdf): p2 headers, p5 schematic.
- Nano ESP32 [datasheet](03_Nano_ESP32_Datasheet.pdf),
  [pinout](04_Nano_ESP32_Full_Pinout.pdf) and
  [schematic](05_Nano_ESP32_Schematic.pdf): D13/GPIO48,
  D11/GPIO38, D12/GPIO47, D10/GPIO21, D9/GPIO18, D2/GPIO5, D3/GPIO6;
  shared circuit ground and direct USB. The
  [ESP32-S3 datasheet](06_ESP32_S3_Datasheet.pdf) is background.
- The installed Arduino core is `2.0.18-arduino.5`, not a generic current ESP-IDF.
  Its esp_timer.h and esp32-hal-gpio.c support the used timer/interrupt APIs.

SPI remains mode 0, MSB first, 1 MHz. Both CS outputs are HIGH before SPI starts.
Only a write-only SYNCH selects both chips. Reads always select one board.

| Register | Value / interpretation |
| --- | --- |
| CNFG_GEN 0x10 | 0x080004, ECG on; FMSTR00; internal bias/lead-off off |
| CNFG_EMUX 0x14 | 0x3B0000 internal isolated calibration; 0 external |
| CNFG_CAL 0x12 | 0x704800 bipolar 1 Hz internal; 0 external |
| CNFG_ECG 0x15 | 0x805000, 128 sps, gain20, HP/LP enabled |
| EN_INT 0x02 | 0x000201: SAMP bit9 only, active-low CMOS on separate INTB wires (p28) |
| EN_INT2 0x03 | 0, unused |
| MNGR_INT 0x04 | 0x380004: EFIT7 = 8 entries; CLR_SAMP1; SAMP_IT00 |

SAMP is a dedicated self-clearing timing term (p27/29). EINT/EOVF/PLL are
polled separately every 4 ms so persistent terms cannot conceal timing edges.
Each service pass drains at most eight words per board. Status is transmitted
once per second. Identity, PLL lock and all configured registers are checked
on both boards; no one-channel success is presented as two-board readiness.
A one-second per-board FIFO-data watchdog also catches a stalled data path even
if timing edges keep arriving.

## Time model and unresolved association

Both onboard FCLK oscillators remain active. Simultaneous SYNCH is a launch
observation, not exact shared-clock synchronization. Each falling INTB edge
captures `esp_timer_get_time()` and a 64-bit pulse counter into a fixed ISR ring.
The timestamp is GPIO interrupt observation time, with interrupt latency/jitter.
No SPI/serial/allocation/fitting occurs in the ISR. Critical sections protect
all ISR/main shared counters and event copies, including 64-bit values.

FMSTR00 gives 512 Hz ECG progressions (p33/37), while the FIFO is configured for
128 sps. The first internal run observed about 128 SAMP edges/s, so firmware now
advertises **ratio 1**. This measured rate does not establish the first-pulse/FIFO
phase. Pulse width still needs a scope check. These edges are not timestamps
attached to the next FIFO read. Local firmware captures every edge;
it transmits the first and every 32nd anchor. Startup interval consistency,
missing/glitch edges, queue loss and stale timing cause an explicit fault.

The host fits pulse number against common MCU microseconds over at most 256
anchors. Centered robust fitting rejects outliers and continuously updates
offset/rate; readiness requires adequate span, plausible rate and bounded
residual. It never uses cardiac peak matching or packet arrival times.

Candidate output mapping for FIFO position n:

    t_i(n) = fit_i(ratio_i * (n + 1)) - 4906 * b_i / 256
    b_i = ratio_i * fitted_pulse_period_i
    relative_ppm = 1e6 * (fs_III / fs_I - 1)

Positive drift means Board 2 samples faster. Table 4 p19 gives 4906 master-input
ticks of LPF latency at 32768 Hz / 128 sps, nominally 149.719 ms. The delay is
scaled with measured clock rate. The n+1 phase hypothesis and any first-edge
miss during post-SYNCH ISR arming remain **unvalidated**; matching configurations
remove nominal channel-delay differences but not this unknown offset. The common
filter delay is not an exact frequency-independent characterization of the HPF.
Small fit residuals do not prove correct integer/fractional FIFO-to-pulse phase.

Firmware reports association_valid=0. Default Lead II therefore remains
unavailable, even with good rate fits. `--allow-unvalidated-timing` enables only
an explicitly labeled exploratory sum. `--samp-ratio 1|2|4` can investigate a
bench-observed rate; it does not validate phase. A candidate rate far from
128 sps cannot become ready. Measured panes use nominal launch-relative timing
until a fit is ready, then candidate timing; both share the MCU domain. Their
historical fit is retained if anchors become stale; the derived pane is disabled.

Common-grid summation uses only overlapping, bracketing valid samples. Linear
interpolation never crosses settling/missing positions, epochs or resets and
never extrapolates. No lead is normalized or inverted before addition. Settling
tags consume sequence/time positions and plot as NaN. Tags6/7/reserved are not
voltages. FIFO/timing/transport/PLL faults stop both boards and start a fresh
epoch after reporting the loss. A mode change also clears both fits/history.

At 128 sps, linear interpolation's curvature bound is max|x''| * T^2 / 8.
For a sine the maximum midpoint relative error is 1-cos(pi*f/128): about 0.75%
at 5 Hz and 23% at 28.35 Hz. A timing objective of 1 ms does not imply accurate
amplitude reconstruction across the full filter passband. No clinical claim.
History is bounded to 1280 positions/board; the rolling view is five seconds.
FIFO batching adds about 62.5 ms; common-grid rendering waits for both brackets,
and GUI redraws every100 ms. OS/USB stalls are not bounded; host silence is
reported after two seconds normally, or five seconds during fault recovery,
which includes board reconfiguration. The derived pane is cleared when invalid.

## E2 wire protocol (ASCII, newline terminated, 115200 baud)

All integers are decimal except chip ID/status hex. s = random boot session,
e = global acquisition epoch, b = board1/2. Event board0 means global. Epoch0
is allowed for pre-acquisition events. Board1 is I; Board2 is III.

    E2,C,s,e,b,mode,chip_hex,launch_us,ratio,latency_ticks,association_valid
    E2,R,s,e,mode
    E2,D,s,e,b,first_sequence,value:tag;value:tag;...
    E2,A,s,e,b,pulse_count,esp_timestamp_us
    E2,S,s,e,b,status_hex,pulse_count,irq_lost,tx_lost
    E2,E,s,e,b,event_code,esp_timestamp_us

C is emitted only after both configurations/readbacks succeed; R confirms both.
Version2 fixes the register/rate/units above. D batches up to eight signed raw
values with ETAG; tags0/2 valid, 1/3 settling, 2/3 end-of-FIFO. Sequence counts
all0..3 entries from zero, including settling. A counters count observed SAMP
edges, not FIFO entries. Status loss counts are per epoch. Reset/loss events
invalidate association; data cannot repair it until a fresh C/C/R epoch.
Unknown/malformed records invalidate alignment; old-session/epoch records are
ignored. Parser capacity is1024 bytes/line with drop-until-newline recovery.
Old ECG:value/unknown versions raise an upload/version error. CSV stores exact
wire bytes as hex plus readable text, with units/options metadata, so malformed
input and fragmentation replay identically. Recordings are never overwritten.

Transport budget (8N1 conservative UART equivalent =11,520 bytes/s): D lines
about 130 bytes at 32 batches/s, A about 70 bytes at 8/s across both boards at the
observed 128-edge/s rate, S about 90 bytes at 2/s: roughly 4,900 bytes/s. Allowing 2%
rate variation plus configuration/fault reporting stays below 6,000 bytes/s,
with over 45% margin. This excludes adversarial command spam; warnings are throttled
and queue exhaustion is an explicit fault. USB throughput is not assumed as a
reason to ignore that budget. The fixed32-line TX ring is pumped only into
available USB space; partial lines finish before dropping queued records.

## Manual acceptance

Completed: both boards identified/configured; smooth measured I/III internal-test
plots and a clean 120-second recording and replay (15,496/15,488 valid samples, no gaps,
acquisition faults or queue losses). Reopening after serial monitoring and plotting
also worked. SAMP fits were about 128 Hz; FIFO phase remains unvalidated.
Remaining checks:

1. Run Format -> Check -> Build -> Unit Tests; close port users, manually Upload,
   then record internal mode 0 for a longer run and replay it. Check missing-board
   behavior. Internal phases may differ.
2. Scope D2/D3 against common circuit GND, with no person connected. Measure
   falling-edge rate and pulse width. Compare counts with FIFO sequence increments
   over known intervals. Capture SYNCH/CS, timing edges and sample availability
   with a logic analyzer if available. Repeat startup and every recovery type.
   Establish ratio, first-edge numbering and first-FIFO phase; reject missing
   pulses. The exact uncertainty left by filter startup must be bounded.
3. With NO person attached, use reviewed small-signal amplitude/common-mode
   wiring to feed identical signals to both channels. Do not ground VCM or
   parallel two VCM outputs. Use a non-repeating edge pattern or multi-frequency
   waveform; a single periodic sine cannot disambiguate whole-cycle offsets.
4. Record at least10minutes. Verify channel polarity/gain, uncorrected offset,
   relative drift, fit residuals, waveform overlay error, and corrected timing
   against the known stimulus. Record mean/RMS/max error and uncertainty across
   tested frequencies. Evaluate the proposed <=1ms relative target; it is not
   already passed. Compare amplitude error separately from timing error.
5. Investigate candidate sum only after association checks: identical inputs
   should overlay and II should equal twice the input within measured front-end,
   noise, gain and interpolation limits. Deliberately interrupt a timing/input
   path off-body; verify explicit gap/fault, no stale sum, and clean reconvergence.
6. Only under a separately reviewed body-connected safety setup, verify the
   second cable mapping and single-RL/shared-LA common-mode behavior. Direct USB,
   a battery laptop, or a good-looking waveform does not establish patient safety.

Automated synthetic results validate software against assumed clock truth only;
they do not establish physical sample association, electrode safety or
physiological Lead II accuracy.
