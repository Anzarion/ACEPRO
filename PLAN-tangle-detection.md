# Distance-Window Tangle Detection — Implementation Plan

Status: **PROPOSED**, awaiting review.
Branch: `debug/tangle-baseline-data`.
Replaces: the legacy `simple` mode in `RunoutMonitor._check_tangle`.

---

## 1. Problem & Goal

### What is broken in the simple mode

```python
if extruder_pos >= window_start + 15mm
   and encoder_pulse_count == snapshot:
    declare_tangle()
# Any single pulse resets the window.
```

Two structural failures on long-Bowden setups (≥1500 mm):

1. **15 mm threshold is below the physical buffer reserve.** Sektion B
   (RDM→Hotend, ~1100 mm) stores ~20 mm of filament as Krümmungs-/
   elastische Reserve.  During slow-print phases the extruder consumes
   this reserve without any flow through the RDM — encoder is legitimately
   idle for the full reserve length.  15 mm therefore false-positives on
   every layer.
2. **Any single pulse resets the window.**  An encoder that pulses every
   25 mm (ACE hysteresis regime) is enough to keep the detector silent
   forever — the window never accumulates.

### What the new detector must do

- Catch a real tangle reliably (verified against two provoked datasets,
  one mild and one full-block).
- Not false-positive on layer-0/1 natural stalls (verified ppm ≥ 0.47 in
  the dataset).
- Not false-positive during toolchanges (already gated, must remain so).
- Latency proportional to print speed (a fast print can fail fast,
  a slow print accepts longer detection).

---

## 2. Algorithm

### Concept in one sentence

> "Over the last N mm of forward extrusion, fewer than ratio_threshold
> of the expected pulses arrived → tangle."

### Pseudo-code

```python
# State retained across ticks:
samples = deque()   # entries: (time, cumulative_forward_extrude_mm,
                    #            cumulative_encoder_pulse)
confirmed_suspicious = 0

def on_tick(eventtime, current_tool):
    # ─── Pre-conditions (any False ⇒ wipe state, return) ──────────
    if not feed_assist_active(): wipe_and_return()
    if not rdm_present():        wipe_and_return()
    if not toolhead_present():   wipe_and_return()
    if toolchange_in_progress:   wipe_and_return()
    # runout_handling_in_progress, print_state != "printing"
    # are filtered by the outer monitor loop already.

    # ─── Sample collection ────────────────────────────────────────
    cum_extrude = previous_cum_extrude + max(0, dext)   # forward-only
    cum_encoder = encoder_pulse_count
    samples.append((eventtime, cum_extrude, cum_encoder))

    # Drop the oldest sample(s) while the window is wider than W
    # mm — but keep at least one sample older than W so the window
    # is guaranteed ≥ W mm whenever we evaluate.
    while len(samples) >= 2 and (cum_extrude - samples[1].extrude) >= W:
        samples.popleft()

    # ─── Window evaluation ────────────────────────────────────────
    oldest = samples[0]
    extrude_in_window = cum_extrude - oldest.extrude
    if extrude_in_window < W:
        return    # window not yet wide enough

    pulses_in_window = cum_encoder - oldest.encoder
    ratio = pulses_in_window / extrude_in_window

    if ratio < tangle_ratio_threshold:
        confirmed_suspicious += 1
        if confirmed_suspicious >= tangle_confirmation_count:
            declare_tangle()
            wipe_state()
    else:
        confirmed_suspicious = 0
```

### Why distance-window, not time-window

Time-window penalises fast prints (10 s at 5 mm/s = 50 mm, at 1 mm/s
only 10 mm — far below the buffer reserve, missed detection).
Distance-window scales latency proportional to speed: fast prints fail
fast, slow prints accept longer wait — physically correct.

### Why ratio, not absolute pulse-count

Absolute pulse-count (`pulses < 5`) couples the threshold to the
calibrated `length_per_pulse`.  Ratio normalises against the actual
window extrusion, decoupling the detector from the encoder constant
(which we already know varies hardware-to-hardware).

---

## 3. Configuration

New keys under `[ace]`:

| Key | Default | Validation | Meaning |
|---|---|---|---|
| `tangle_detection_mode` | `"distance_window"` | str in {`"simple"`, `"distance_window"`, `"off"`} | Algorithm selector.  `"simple"` kept for back-compat / kurze Bowden; `"off"` disables detection entirely. |
| `tangle_window_extrude_mm` | `30.0` | float, > 0 | Window width `W` in mm of forward extrusion. |
| `tangle_ratio_threshold` | `0.30` | float, 0 < x < 1 | Trigger when pulses/mm in window is below this. |
| `tangle_confirmation_count` | `1` | int, ≥ 1 | Consecutive suspicious window evaluations required.  1 = fire immediately (current design); 2 = require two consecutive suspicious windows (more conservative). |
| `tangle_debug` | `False` | bool | Existing; logs per-tick window state to klippy.log. |

Existing keys preserved (used by simple mode and telemetry):

| Key | Notes |
|---|---|
| `tangle_detection` | `True/False`.  When `False`, no mode runs.  When `True`, `tangle_detection_mode` selects which. |
| `tangle_detection_length` | Only used by simple mode. |
| `tangle_telemetry_log` | Unchanged. |

---

## 4. State Machine

```
              ┌─────────────────┐
              │ NORMAL          │
              │ confirmed = 0   │
   any pre-   │ samples populated│
 condition    └─────────────────┘
   fails              ▲    │ ratio < threshold
   in tick            │    │
                      │    ▼
                      │  ┌─────────────────┐
                      │  │ SUSPICIOUS      │
       ratio ≥        │  │ confirmed = N   │
       threshold ─────┘  └─────────────────┘
                              │
                              │ confirmed ≥ count
                              ▼
                       ┌─────────────────┐
                       │ TANGLE DECLARED │ ─► _handle_tangle_detected
                       │ wipe state      │
                       └─────────────────┘

wipe_state() = clear samples deque, confirmed = 0
```

`wipe_state()` is also called on every pre-condition failure (TC start,
FA drops, sensor cleared) so the window starts fresh when monitoring
resumes — never carries stale data across a print pause / TC boundary.

---

## 5. Test-Cases

Built against the **real telemetry data** captured this session
(`debug/tangle-baseline-data` branch, current druck).

| # | Scenario | Input | Expected |
|---|---|---|---|
| 1 | **Tangle 1 (mild)** — Layer 3, t=1047 s | 30,3 mm window, 9 pulses (ratio 0,297) | Trigger ✓ |
| 2 | **Tangle 2 (worst)** — Layer 3, t=1149 s | 30,1 mm window, 9 pulses (ratio 0,299) | Trigger ✓ |
| 3 | **Layer-0 natural stall cluster** (oscillating ppm 0,47–1,69) | rolling window over 540–595 s | **No trigger** |
| 4 | **Toolchange T2→T3** (1367 → 3679 pulses over 60 s) | tc=1 throughout | **No trigger** (gate) |
| 5 | **Steady-state ppm ~1,0** (large sample) | typical 5 mm/s, ppm 0,9–1,2 | No trigger |
| 6 | **Cold start, window not yet wide enough** | only 10 mm extruded total | No trigger (window < W) |
| 7 | **Retract dominates last ticks** — extruder moves backward 5 mm, no forward | cum_extrude unchanged, encoder may pulse from roller backwards | Window does not shrink; no false trigger from retract |
| 8 | **FA on/off transition mid-window** | wipe + restart, no spurious trigger | No trigger |
| 9 | **tangle_confirmation_count=2** | one suspicious then one ratio=0,7 window | No trigger (counter resets) |
| 10 | **tangle_confirmation_count=2** | two suspicious windows in row | Trigger ✓ |

Tests 1, 2, 3 use synthesised sample sequences derived from the real
telemetry data — i.e. each test feeds the detector the same
`(t, dext, denc)` sequence Klipper actually produced.

---

## 6. Edge-Cases (explicit handling)

| Situation | Handling |
|---|---|
| First few ticks after FA on | Window underfilled, no evaluation, no trigger |
| Negative `dext` (retract) | Counted as `0` in cum_extrude.  Encoder pulses from backwards roller still increment cum_encoder — they reduce the apparent ratio, which is **conservatively** safe (less likely to falsely trigger; never more likely). |
| `dext` exactly 0 across many ticks (paused extruder) | Window doesn't grow, no evaluation. |
| Print pause / resume | `print_state` becomes `paused` → outer monitor loop wipes state; on resume, fresh window starts. |
| Toolchange end (tc 1→0) | Wiped via FA off/on transition that brackets every TC.  Detector starts with empty samples on the resume side. |
| Spike of `denc` (encoder catches up after polling lag — should not happen with MCU_counter, defensive) | Counted as positive pulses, makes ratio higher → less likely to trigger.  Safe. |
| `length_per_pulse` set wrong in config | Detector is **ratio-based, not absolute** → unaffected. |
| `tangle_window_extrude_mm` set very small (e.g. 5) | Validation must reject below a floor (proposed: ≥ 10 mm) to prevent detector becoming trivially sensitive. |
| `tangle_ratio_threshold` set ≥ 1.0 | Validation must reject (would always trigger).  Proposed: enforce 0 < x < 0.9. |

---

## 7. Implementation Strategy

### Files to change

1. **`extras/ace/runout_monitor.py`**
   - Add 5 instance fields in `__init__`:
     `_dw_samples` (deque), `_dw_confirmed` (int),
     `_dw_cum_extrude` (float), `_dw_last_extruder_pos` (float),
     `_dw_last_encoder` (int).
   - Add config reads for the 5 new keys.
   - Add `_check_tangle_distance_window(eventtime, current_tool)` method
     implementing the algorithm.
   - Modify the dispatcher in `_check_tangle` to call the right mode method
     based on `tangle_detection_mode`.
   - Add `_dw_wipe_state()` helper called on pre-condition failures.

2. **`extras/ace/manager.py`**
   - Pass new config keys when constructing `RunoutMonitor`.

3. **`tests/test_tangle_detection.py`**
   - Add `TestDistanceWindowDetection` class with the 10 test cases
     above.
   - Test data fed from synthesised `(t, dext, denc)` sequences,
     not the full TSV — keeps tests fast and deterministic.

### What is NOT touched

- Telemetry logic (`_log_tangle_telemetry`) — unchanged.
- `_handle_tangle_detected` — unchanged, just called by the new method
  when it fires.
- Simple-mode code path — kept intact for back-compat / short-Bowden
  setups; selected via `tangle_detection_mode = "simple"`.

### Estimated diff size

~120 lines in `runout_monitor.py`, ~150 lines in tests, ~5 lines in
`manager.py`.

---

## 8. What this detector **will NOT** catch

Honest about its limits.  These are accepted scope limits, not bugs:

- **Tangles in layer 0** with extruder speed < ~1,5 mm/s where the
  buffer-reserve interaction looks similar to natural stalls.
  Detection latency at 1 mm/s is 30 s — by then a real tangle could
  already have abandoned half a layer.  Acceptable: layer 1 is rarely
  critical to print quality vs. the false-positive cost of triggering
  there.
- **Partial tangles that maintain ratio > 0,30** — the mild end of the
  spectrum.  Tangle 1 here was at ratio 0,297 (just barely caught).
  A user-induced "very gentle" tangle could stay above the threshold
  indefinitely.  This is by design — over-sensitivity costs more than
  under-sensitivity for that segment.
- **Mechanical problems upstream of the ACE** (spool not advancing but
  ACE still pulling internal buffer).  Encoder will keep counting from
  ACE-buffer drain.  The ACE-buffer is small enough (~20 mm) that this
  will become a real tangle quickly, but the first ~5 s post-block are
  invisible to us.
- **Filament cuts cleanly without resistance** — the toolhead sensor
  (existing runout detection) catches that, not us.

---

## 9. Open questions before implementation

1. **Default `tangle_detection_mode`**: should new installs default to
   `"distance_window"` (recommended) or `"simple"` (preserves current
   behaviour but bad for long Bowden)?  Either way, existing configs
   with `tangle_detection: True` keep working.
2. **Should the `tangle_detection_length` key be deprecated** when
   `mode = distance_window`?  Proposal: ignore it silently (with debug
   log), keep it parsed so simple-mode users aren't broken.
3. **Window evaluation cadence**: every tick (50 ms) is fine on
   performance, but evaluation each tick is ~20× redundant per second.
   Proposal: evaluate every 4th tick (200 ms = matches existing
   TANGLE_CHECK_INTERVAL).  Same trigger latency in practice.

---

## 10. Approval

When this plan is approved:

- I will implement the changes on `debug/tangle-baseline-data`.
- Tests will pass before any commit.
- Diff will be shown in the chat before commit.
- After commit + push, you pull on the CM4, FIRMWARE_RESTART, and we
  validate against a live print with provoked tangles.

If anything in this plan needs to change — defaults, threshold,
window size, edge-case handling, scope — comment and I'll revise
before code goes anywhere.
