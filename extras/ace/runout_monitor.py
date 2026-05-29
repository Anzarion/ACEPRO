"""
Runout and tangle monitoring module for ACE Pro filament management system.

This module handles filament runout detection during printing, coordinating
with the endless spool system for automatic material swapping when runout
is detected.

Optional tangle detection (``tangle_detection: True`` in ``[ace]``):
    While printing with feed-assist active, compares extruder stepper
    movement against RDM encoder pulses.  If the extruder extrudes more
    than ``tangle_detection_length`` (default 15 mm, configurable in
    ``[ace]``) without any encoder activity, and both RDM and nozzle
    sensors still show filament present, a spool tangle is declared —
    the filament is stuck between the spool and RDM while the extruder
    consumes the RDM-to-nozzle buffer.
"""

import logging
import os

from .config import (
    SENSOR_TOOLHEAD,
    SENSOR_RDM,
    get_instance_from_tool,
    get_local_slot,
    ACE_INSTANCES,
)


class RunoutMonitor:
    """
    Monitors filament sensors during printing and handles runout detection.

    Responsibilities:
    - Track sensor state changes during print
    - Detect filament runout (sensor present → absent transition)
    - Detect spool tangle (extruder moves but encoder is stalled)
    - Coordinate with endless spool for automatic material swapping
    - Show user prompts when manual intervention needed
    - Manage runout handling state machine

    The monitor runs as a periodic callback registered with the Klipper reactor,
    checking sensor states and print status to detect runout events.
    """

    # Default detection length for tangle checking (mm).
    # Only used by the legacy "simple" mode.
    DEFAULT_TANGLE_DETECTION_LENGTH = 15.0

    # How often the tangle check runs (seconds).  250 ms matches the
    # Klipper filament_motion_sensor cadence.
    TANGLE_CHECK_INTERVAL = 0.250

    # ── Distance-window detector defaults ─────────────────────────────
    # See PLAN-tangle-detection.md for the full rationale.
    VALID_TANGLE_MODES = ("distance_window", "simple", "off")
    DEFAULT_TANGLE_MODE = "distance_window"
    DEFAULT_TANGLE_WINDOW_EXTRUDE_MM = 30.0      # rolling window width
    DEFAULT_TANGLE_RATIO_THRESHOLD = 0.30        # ppm cutoff
    DEFAULT_TANGLE_CONFIRMATION_COUNT = 1        # consecutive bad evals
    # Hard validation floors / ceilings.  Below these the detector
    # becomes trivially sensitive and would false-positive in normal
    # ACE-hysteresis stalls; above them it would be effectively off.
    TANGLE_WINDOW_EXTRUDE_FLOOR_MM = 10.0
    TANGLE_RATIO_THRESHOLD_MAX = 0.90
    # Evaluate the window every Nth monitor tick.  4 × 50 ms = 200 ms;
    # at the smallest reasonable window (10 mm) and typical print
    # speeds this gives ≥10 evaluations per window — plenty.
    TANGLE_DW_EVAL_EVERY_N_TICKS = 4

    # Theoretical encoder length-per-pulse from the RDM hardware geometry
    # (Kobra-S1/K3M reference).  Used as a comparison anchor in the
    # baseline telemetry header — we want to know whether the real value
    # measured from a print matches this theoretical figure.
    THEORETICAL_LENGTH_PER_PULSE = 1.86532063806894

    def __init__(self, printer, gcode, reactor, endless_spool, manager,
                 runout_debounce_count=1, tangle_detection=False,
                 tangle_detection_length=None,
                 tangle_detection_mode=None,
                 tangle_window_extrude_mm=None,
                 tangle_ratio_threshold=None,
                 tangle_confirmation_count=None,
                 tangle_debug=False,
                 tangle_telemetry_log=None):
        """
        Initialize runout monitor.

        Args:
            printer: Klipper printer object for accessing printer state
            gcode: Klipper gcode object for sending commands and responses
            reactor: Klipper reactor for timer management
            endless_spool: EndlessSpool instance for automatic swapping
            manager: AceManager instance (for sensor queries and state)
            runout_debounce_count: Number of consecutive sensor-absent readings
                required before confirming a runout event. At the default 50ms
                poll interval, 3 readings ≈ 150ms debounce window. Set to 1
                for immediate (no debounce) behaviour (default).
            tangle_detection: When True, monitor encoder vs extruder
                movement to detect spool tangles while printing.
            tangle_detection_length: Distance in mm the extruder must
                move without encoder activity before a tangle is declared.
                Defaults to DEFAULT_TANGLE_DETECTION_LENGTH (15.0 mm).
            tangle_debug: When True, log read-only baseline telemetry
                (encoder, extruder, sensor states, simple-mode events)
                on every monitor tick.  Used to characterise the simple
                detector before deciding whether the windowed approach
                is actually needed.  Does NOT change detection logic.
            tangle_telemetry_log: Path to the per-tick TSV telemetry log.
                A ``~`` is expanded.  Failures to open the file are
                caught and disable telemetry, never crash the monitor.
        """
        self.printer = printer
        self.gcode = gcode
        self.reactor = reactor
        self.endless_spool = endless_spool
        self.manager = manager  # Reference back to manager for sensor queries

        # Debounce configuration
        self.runout_debounce_count = max(1, int(runout_debounce_count))
        self._runout_false_count = 0

        # State tracking
        self.prev_toolhead_sensor_state = None
        self.last_printing_active = False
        self.last_print_state = "idle"
        self.monitor_debug_counter = 0

        # Control flags
        self.runout_detection_active = False
        self.runout_handling_in_progress = False

        # Timer handle
        self._monitoring_timer = None

        # --- Tangle detection state ---
        self.tangle_detection_enabled = bool(tangle_detection)
        self.tangle_detection_length = float(
            tangle_detection_length if tangle_detection_length is not None
            else self.DEFAULT_TANGLE_DETECTION_LENGTH
        )

        # Mode selector: distance_window (new, default), simple (legacy,
        # kept for short Bowdens), off (no detection logic runs).
        requested_mode = (
            tangle_detection_mode if tangle_detection_mode is not None
            else self.DEFAULT_TANGLE_MODE
        )
        if requested_mode not in self.VALID_TANGLE_MODES:
            logging.warning(
                "ACE: tangle_detection_mode=%r is not one of %s; "
                "falling back to %r",
                requested_mode, self.VALID_TANGLE_MODES,
                self.DEFAULT_TANGLE_MODE,
            )
            requested_mode = self.DEFAULT_TANGLE_MODE
        self.tangle_detection_mode = requested_mode

        # Distance-window parameters with hard floors / ceilings.
        window_mm = float(
            tangle_window_extrude_mm if tangle_window_extrude_mm is not None
            else self.DEFAULT_TANGLE_WINDOW_EXTRUDE_MM
        )
        if window_mm < self.TANGLE_WINDOW_EXTRUDE_FLOOR_MM:
            logging.warning(
                "ACE: tangle_window_extrude_mm=%.1f below floor %.1f; "
                "clamping",
                window_mm, self.TANGLE_WINDOW_EXTRUDE_FLOOR_MM,
            )
            window_mm = self.TANGLE_WINDOW_EXTRUDE_FLOOR_MM
        self.tangle_window_extrude_mm = window_mm

        ratio = float(
            tangle_ratio_threshold if tangle_ratio_threshold is not None
            else self.DEFAULT_TANGLE_RATIO_THRESHOLD
        )
        if ratio <= 0.0 or ratio >= self.TANGLE_RATIO_THRESHOLD_MAX:
            logging.warning(
                "ACE: tangle_ratio_threshold=%.2f outside (0, %.2f); "
                "falling back to default %.2f",
                ratio, self.TANGLE_RATIO_THRESHOLD_MAX,
                self.DEFAULT_TANGLE_RATIO_THRESHOLD,
            )
            ratio = self.DEFAULT_TANGLE_RATIO_THRESHOLD
        self.tangle_ratio_threshold = ratio

        self.tangle_confirmation_count = max(1, int(
            tangle_confirmation_count
            if tangle_confirmation_count is not None
            else self.DEFAULT_TANGLE_CONFIRMATION_COUNT
        ))

        # Extruder position beyond which a tangle is declared (simple mode)
        self._tangle_runout_pos = None
        # Encoder pulse snapshot at the time the window was set (simple mode)
        self._tangle_encoder_snapshot = None
        # Klipper objects resolved at first use
        self._extruder = None
        self._estimated_print_time = None

        # --- Distance-window state ---
        # Samples: list of (eventtime, cum_extrude_mm, cum_encoder_pulse)
        # tuples.  We only ever keep enough samples to span tangle_window_
        # extrude_mm — the oldest entries get popped as the window slides.
        self._dw_samples = []
        self._dw_confirmed_count = 0
        self._dw_cum_extrude = 0.0           # monotonic forward-only sum
        self._dw_last_extruder_pos = None    # for computing dext per tick
        self._dw_tick_counter = 0            # for evaluate-every-N gating

        # --- Read-only baseline telemetry ---
        self.tangle_debug = bool(tangle_debug)
        self.tangle_telemetry_log = tangle_telemetry_log
        # Lazy-initialised file handle for the TSV log.
        self._tlm_file_handle = None
        # If opening the log fails once, do not retry every tick.
        self._tlm_file_open_failed = False
        # Has the START header been emitted yet?
        self._tlm_started = False
        # Previous-tick values for delta calculation.
        self._tlm_last_encoder = None
        self._tlm_last_extruder_pos = None
        # 1-second aggregation bucket for the klippy.log summary.
        self._tlm_bucket_d_encoder = 0
        self._tlm_bucket_d_extruder = 0.0
        self._tlm_last_summary_time = None
        # Cross-tick handoff: the simple detector tags state-changing
        # events here, so the next telemetry tick logs them in the TSV.
        self._tlm_pending_simple_event = ""
        # Previous feed_assist state — used by _log_tangle_telemetry to
        # skip idle ticks (pre-print, between prints) while still capturing
        # the on/off transition itself.  None on the very first tick.
        self._tlm_prev_feed_assist = None

        # --- Model-start anchor (TANGLE_TELEMETRY_MARK) ---
        # Snapshot taken when the user-issued mark fires from PRINT_START,
        # immediately before the slicer's first model move.  Lets later
        # analysis cut away heating/prime/purge/travel and look at the
        # encoder/extruder behaviour from t0 = first model G-code.
        self._mark_eventtime = None
        self._mark_extruder_pos = None
        self._mark_encoder_pulse = None
        self._mark_label = None

        # Register the GCODE command used to drop a mark into the log.
        # Safe to register unconditionally — handler is a no-op when
        # telemetry is disabled.
        self.gcode.register_command(
            "TANGLE_TELEMETRY_MARK",
            self.cmd_TANGLE_TELEMETRY_MARK,
            desc=self.cmd_TANGLE_TELEMETRY_MARK_help,
        )

    cmd_TANGLE_TELEMETRY_MARK_help = (
        "Stamp the tangle telemetry log with a labelled marker. "
        "Use at the end of PRINT_START — directly before the first model "
        "G-code — so later analysis can locate the true model-print start. "
        "LABEL= (optional, default 'model_start')."
    )

    def cmd_TANGLE_TELEMETRY_MARK(self, gcmd):
        """Snapshot current encoder/extruder values and emit a marker.

        Writes a single ``# MARK ...`` comment line into the telemetry TSV
        capturing eventtime, encoder_pulse, extruder_pos and the label.
        The same values are also stored as instance state so subsequent
        analyses / detection logic can compute deltas relative to the mark.

        Does nothing harmful when tangle_debug is off or the telemetry
        file failed to open — the snapshot still lands on the instance
        state, but no TSV row is produced.
        """
        label = gcmd.get("LABEL", "model_start")
        eventtime = self.reactor.monotonic()

        # Extruder position — best effort.  When the extruder cannot be
        # resolved (e.g. very early in startup) leave the field empty.
        extruder_pos = None
        if self._resolve_extruder():
            try:
                extruder_pos = self._get_extruder_pos(eventtime)
            except Exception as e:
                logging.warning(
                    "ACE: TANGLE_TELEMETRY_MARK: extruder_pos read failed: %s",
                    e,
                )

        encoder_pulse = self.manager.get_rdm_encoder_pulse()

        self._mark_eventtime = eventtime
        self._mark_extruder_pos = extruder_pos
        self._mark_encoder_pulse = encoder_pulse
        self._mark_label = label

        ext_str = (
            f"{extruder_pos:.3f}" if isinstance(extruder_pos, (int, float))
            else "n/a"
        )
        enc_str = (
            f"{encoder_pulse}" if isinstance(encoder_pulse, int) else "n/a"
        )
        mark_line = (
            f"# MARK label={label} eventtime={eventtime:.3f} "
            f"extruder_pos={ext_str} encoder_pulse={enc_str}"
        )

        if self.tangle_debug and self._open_telemetry_log():
            try:
                self._tlm_file_handle.write(mark_line + "\n")
            except Exception as e:
                logging.warning(
                    "ACE: TANGLE_TELEMETRY_MARK: TSV write failed: %s", e
                )

        logging.info("ACE: %s", mark_line.lstrip("# "))
        gcmd.respond_info(
            f"TANGLE_TELEMETRY_MARK: label={label} "
            f"extruder_pos={ext_str} encoder_pulse={enc_str}"
        )

    def start_monitoring(self):
        """Start runout detection monitor loop."""
        self.gcode.respond_info("ACE: Starting runout detection monitor")
        self.set_detection_active(True)
        self._monitoring_timer = self.reactor.register_timer(
            self._monitor_runout,
            self.reactor.NOW
        )

    def stop_monitoring(self):
        """Stop runout monitoring."""
        self.gcode.respond_info("ACE: Stopping runout detection monitor")
        self.set_detection_active(False)
        if self._monitoring_timer:
            try:
                self.reactor.unregister_timer(self._monitoring_timer)
            except Exception:
                pass
            self._monitoring_timer = None

    def set_detection_active(self, active):
        """
        Enable/disable runout detection with tracing.

        Args:
            active: True to enable detection, False to disable

        Returns:
            bool: The new active state
        """
        old_state = self.runout_detection_active
        self.runout_detection_active = active

        if old_state != active:
            state_str = 'ENABLED' if active else 'DISABLED'
            self.gcode.respond_info(
                f"ACE: Runout detection {state_str} "
                f"(was: {old_state}, now: {active}, "
                f"toolchange_in_progress={self.manager.toolchange_in_progress})"
            )

        return active

    def _monitor_runout(self, eventtime):
        """
        Monitor filament runout during printing.

        This is the main monitoring loop that runs periodically via reactor timer.
        It tracks print state, sensor states, and detects runout events.

        Args:
            eventtime: Current event time from reactor

        Returns:
            float: Next callback time (eventtime + interval)
        """
        # Get current state
        print_stats = self.printer.lookup_object("print_stats", None)
        is_printing = False
        raw_print_state = ""
        if print_stats:
            try:
                stats = print_stats.get_status(eventtime)
                raw_print_state = (stats.get("state") or "").lower()
                is_printing = raw_print_state == "printing"
            except Exception:
                is_printing = False
                raw_print_state = ""

        current_tool = self.manager.state.get("ace_current_index", -1)
        current_sensor_state = self.manager.get_switch_state(SENSOR_TOOLHEAD)

        # Track state changes for logging
        old_printing_active = self.last_printing_active
        old_print_state = self.last_print_state
        self.last_printing_active = is_printing
        self.last_print_state = raw_print_state

        if old_print_state != raw_print_state:
            self.gcode.respond_info(f"ACE: Print state changed: {old_print_state} → {raw_print_state}")

        # Detect print start and force initialize
        print_just_started = (
            is_printing and
            not old_printing_active and
            raw_print_state == "printing" and
            current_tool >= 0
        )

        if print_just_started:
            self.gcode.respond_info("ACE: Print started - initializing runout detection")

            # Force initialize baseline
            self.prev_toolhead_sensor_state = current_sensor_state

            # Enable detection immediately if sensor shows filament
            if current_sensor_state:
                self.set_detection_active(True)
                self.gcode.respond_info(
                    f"ACE: Runout detection ENABLED at print start "
                    f"(sensor: True, tool: T{current_tool})"
                )
            else:
                self.gcode.respond_info(
                    f"ACE: Runout detection WAITING at print start "
                    f"(sensor: False, tool: T{current_tool})"
                )

            # Sync macro state
            try:
                self.gcode.run_script_from_command(
                    f"SET_GCODE_VARIABLE MACRO=_ACE_STATE VARIABLE=active VALUE={current_tool}"
                )
            except Exception as e:
                self.gcode.respond_info(f"ACE: Could not sync macro state: {e}")

            return eventtime + 0.05

        # DEBUG LOGGING every ~15 minutes
        self.monitor_debug_counter += 1
        if self.monitor_debug_counter >= 1200 * 15:
            self.monitor_debug_counter = 0
            self.gcode.respond_info(
                f"ACE: Monitor - Tool: T{current_tool}, "
                f"Printing: {is_printing} ({raw_print_state}), "
                f"Prev sensor: {self.prev_toolhead_sensor_state}, "
                f"Current sensor: {current_sensor_state}, "
                f"Detection active: {self.runout_detection_active}, "
                f"Toolchange: {self.manager.toolchange_in_progress}, "
                f"Runout handling: {self.runout_handling_in_progress}, "
                f"Debounce: {self._runout_false_count}/{self.runout_debounce_count}"
            )

            # For debugging: Auto-recovery check
            # WARN if detection should be active but isn't
            if (is_printing and
                    current_sensor_state and
                    not self.runout_detection_active and
                    current_tool >= 0 and
                    not self.manager.toolchange_in_progress and
                    not self.runout_handling_in_progress):

                self.gcode.respond_info(
                    "ACE: Autorecovery: ⚠ WARNING - Runout detection should be active but is disabled! "
                    "Attempting to enable..."
                )

                # Try to recover
                self.prev_toolhead_sensor_state = current_sensor_state
                self.set_detection_active(True)

                self.gcode.respond_info(
                    f"ACE: Autorecovery: Auto-recovery attempted - detection re-enabled "
                    f"(sensor: {current_sensor_state}, tool: T{current_tool})"
                )

        # Early exit if detection disabled or toolchange in progress.
        # Before exiting we still emit a telemetry tick so the TSV
        # captures the TC / detection-off interval — otherwise long
        # toolchange sequences (60-90 s of filament movement) leave a
        # gap in the log and the toolchange=1 marker is never written.
        if not self.runout_detection_active or self.manager.toolchange_in_progress:
            if self.tangle_debug and not self.runout_handling_in_progress:
                try:
                    self._log_tangle_telemetry(eventtime, current_tool)
                except Exception as e:
                    logging.warning("ACE: tangle telemetry error: %s", e)
            self._tangle_runout_pos = None
            return eventtime + 0.2

        try:
            if current_tool < 0:
                # No active tool - nothing to monitor
                self.prev_toolhead_sensor_state = None
                self._runout_false_count = 0
                return eventtime + 0.1

            print_just_stopped = old_printing_active and (not is_printing) and (raw_print_state != "paused")

            # PRINT STOPPED - clean up state
            if print_just_stopped:
                self.gcode.respond_info("ACE: Print stopped/cancelled - resetting monitor baseline")
                self.prev_toolhead_sensor_state = None
                self._runout_false_count = 0
                self._tangle_runout_pos = None
                self.runout_handling_in_progress = False

                if not self.runout_detection_active:
                    self.gcode.respond_info("ACE: Restoring runout monitoring after print stop")
                    self.set_detection_active(True)

                try:
                    self.gcode.run_script_from_command(
                        "SET_GCODE_VARIABLE MACRO=_ACE_STATE VARIABLE=active VALUE=-1"
                    )
                except Exception as e:
                    self.gcode.respond_info(f"ACE: Could not sync macro state on print stop: {e}")

                return eventtime + 0.2

            # PAUSED or NOT PRINTING - sleep/relax monitoring
            if raw_print_state == "paused" or not is_printing:
                self.prev_toolhead_sensor_state = None
                self._runout_false_count = 0
                self._tangle_runout_pos = None
                return eventtime + 0.2

            # Enhanced baseline initialization
            if self.prev_toolhead_sensor_state is None:
                self.prev_toolhead_sensor_state = current_sensor_state
                self._runout_false_count = 0
                filament_pos = self.manager.state.get("ace_filament_pos", "bowden")

                self.gcode.respond_info(
                    f"ACE: Monitoring baseline established. "
                    f"Sensor: {'present' if current_sensor_state else 'absent'}, "
                    f"Tool: T{current_tool}, State: {filament_pos}"
                )

                # If sensor has filament and we're printing, enable detection immediately
                if current_sensor_state and is_printing and current_tool >= 0:
                    if not self.runout_detection_active:
                        self.set_detection_active(True)
                        self.gcode.respond_info("ACE: Runout detection enabled (baseline init)")

                # Sync macro state
                try:
                    self.gcode.run_script_from_command(
                        f"SET_GCODE_VARIABLE MACRO=_ACE_STATE VARIABLE=active VALUE={current_tool}"
                    )
                except Exception as e:
                    self.gcode.respond_info(f"ACE: Could not sync macro state: {e}")

                return eventtime + 0.05

            # ===== RUNOUT DETECTION - detect present → absent transition =====
            if self.prev_toolhead_sensor_state is True and current_sensor_state is False:
                # Sensor went absent - increment debounce counter
                self._runout_false_count += 1

                if self._runout_false_count < self.runout_debounce_count:
                    # Not yet confirmed - keep prev as True, poll again quickly
                    return eventtime + 0.05

                # Debounce threshold reached - confirmed runout
                self._runout_false_count = 0

                if self.runout_handling_in_progress:
                    self.gcode.respond_info("ACE: Runout detection suppressed (already handling runout)")
                    self.prev_toolhead_sensor_state = current_sensor_state
                    return eventtime + 0.2

                self.gcode.respond_info(
                    f"ACE: Runout detected on T{current_tool} "
                    f"(sensor: present → absent, confirmed after "
                    f"{self.runout_debounce_count} readings)"
                )

                self._handle_runout_detected(current_tool)

                self.prev_toolhead_sensor_state = current_sensor_state
                return eventtime + 0.2

            # Sensor is present (or was already absent) - reset debounce counter
            if self._runout_false_count > 0:
                self._runout_false_count = 0

            # ===== TANGLE TELEMETRY (read-only, behind tangle_debug) =====
            # Runs BEFORE _check_tangle so simple-detector events tagged by
            # that call appear in the NEXT telemetry tick (1-tick / 250 ms
            # lag, acceptable for baseline characterisation).
            if self.tangle_debug and not self.runout_handling_in_progress:
                try:
                    self._log_tangle_telemetry(eventtime, current_tool)
                except Exception as e:
                    logging.warning("ACE: tangle telemetry error: %s", e)

            # ===== TANGLE DETECTION (optional) =====
            if self.tangle_detection_enabled and not self.runout_handling_in_progress:
                self._check_tangle(eventtime, current_tool)

            # Update previous state for next cycle
            self.prev_toolhead_sensor_state = current_sensor_state
            return eventtime + 0.05

        except self.printer.command_error as e:
            # Klipper printer error
            error_msg = str(e)
            if "shutdown" in error_msg.lower() or "lost communication" in error_msg.lower():
                self.gcode.respond_info("ACE: Monitor stopped due to printer shutdown/MCU disconnect")
                self.set_detection_active(False)
                self.runout_handling_in_progress = False
                return self.reactor.NEVER
            else:
                self.gcode.respond_info(f"ACE: Monitor command error: {e}")
                return eventtime + 1.0

        except Exception as e:
            self.gcode.respond_info(f"ACE: Monitor error: {e}")
            return eventtime + 1.0

    # ========== Tangle Detection ==========

    def _resolve_extruder(self):
        """Lazily look up the Klipper extruder and estimated_print_time.

        Called once on the first tangle check.  Returns True on success.
        """
        if self._extruder is not None:
            return True
        try:
            self._extruder = self.printer.lookup_object("extruder")
            mcu = self.printer.lookup_object("mcu")
            self._estimated_print_time = mcu.estimated_print_time
            return True
        except Exception as e:
            logging.warning("ACE: Tangle detection: cannot resolve extruder: %s", e)
            return False

    def _get_extruder_pos(self, eventtime):
        """Return extruder stepper position in mm at *eventtime*."""
        print_time = self._estimated_print_time(eventtime)
        return self._extruder.find_past_position(print_time)

    def _reset_tangle_window(self, eventtime):
        """Reset the tangle detection window.

        Snapshots the current extruder position and encoder pulse count
        so the next check starts fresh.
        """
        if not self._resolve_extruder():
            self._tangle_runout_pos = None
            return
        encoder_pulse = self.manager.get_rdm_encoder_pulse()
        if encoder_pulse is None:
            self._tangle_runout_pos = None
            return
        extruder_pos = self._get_extruder_pos(eventtime)
        self._tangle_runout_pos = extruder_pos + self.tangle_detection_length
        self._tangle_encoder_snapshot = encoder_pulse

    def _check_tangle(self, eventtime, current_tool):
        """Dispatcher: route to the configured tangle detection mode.

        The shared pre-conditions (feed-assist active, both sensors
        present, RDM encoder available) live in the per-mode methods so
        each can record its own debug breadcrumbs into the telemetry log.
        """
        if self.tangle_detection_mode == "off":
            return
        if self.tangle_detection_mode == "simple":
            self._check_tangle_simple(eventtime, current_tool)
            return
        # Default + future expansion point.
        self._check_tangle_distance_window(eventtime, current_tool)

    def _check_tangle_simple(self, eventtime, current_tool):
        """Legacy point-to-point tangle check.

        Tangle is declared when ALL of the following are true:
            1. Print state is "printing" (already guaranteed by caller)
            2. ACE feed-assist is active
            3. RDM detect pin shows filament present
            4. Nozzle sensor shows filament present
            5. Extruder moved >= TANGLE_DETECTION_LENGTH since last reset
            6. RDM encoder pulse count has NOT changed since last reset

        When any condition fails, the detection window is reset so we
        never accumulate stale state.  See PLAN-tangle-detection.md
        section 1 for why this mode is unsuitable for long Bowdens.
        """
        # Condition 2: feed-assist must be active
        if not self.manager.is_feed_assist_active():
            if self.tangle_debug and self._tangle_runout_pos is not None:
                self._tlm_pending_simple_event = "ABORT:feed_assist_lost"
            self._tangle_runout_pos = None
            return

        # Condition 3: RDM sensor shows filament present
        if not self.manager.get_switch_state(SENSOR_RDM):
            if self.tangle_debug and self._tangle_runout_pos is not None:
                self._tlm_pending_simple_event = "ABORT:rdm_cleared"
            self._tangle_runout_pos = None
            return

        # Condition 4: Nozzle sensor shows filament present
        if not self.manager.get_switch_state(SENSOR_TOOLHEAD):
            if self.tangle_debug and self._tangle_runout_pos is not None:
                self._tlm_pending_simple_event = "ABORT:toolhead_cleared"
            self._tangle_runout_pos = None
            return

        # Get current encoder pulse count from RDM tracker
        current_encoder = self.manager.get_rdm_encoder_pulse()
        if current_encoder is None:
            # RDM is not a filament_tracker — cannot do tangle detection
            return

        # Initialize window if not set
        if self._tangle_runout_pos is None:
            self._reset_tangle_window(eventtime)
            return

        # Condition 6: If encoder has moved, filament is flowing — reset window
        if current_encoder != self._tangle_encoder_snapshot:
            self._reset_tangle_window(eventtime)
            return

        # Condition 5: Check extruder position
        if not self._resolve_extruder():
            return
        extruder_pos = self._get_extruder_pos(eventtime)
        if extruder_pos < self._tangle_runout_pos:
            # Extruder hasn't moved far enough yet — no tangle
            return

        # ===== ALL 6 CONDITIONS MET — TANGLE DETECTED =====
        if self.tangle_debug:
            self._tlm_pending_simple_event = "TANGLE_FIRED"
        logging.warning(
            "ACE: TANGLE DETECTED on T%d — extruder at %.1f mm "
            "(window was %.1f mm), encoder stuck at %d pulses",
            current_tool, extruder_pos,
            self._tangle_runout_pos - self.tangle_detection_length,
            current_encoder,
        )
        self._handle_tangle_detected(current_tool)

    def _dw_wipe_state(self):
        """Reset the distance-window detector to a clean slate.

        Called whenever a pre-condition fails (FA off, sensor cleared,
        toolchange starts, runout handling starts) so the window starts
        fresh when conditions become favourable again — never carries
        stale samples across a print pause / TC boundary.
        """
        self._dw_samples = []
        self._dw_confirmed_count = 0
        self._dw_last_extruder_pos = None
        # cum_extrude stays monotonic across wipes; samples reference
        # absolute cumulative values, so a fresh samples list will just
        # start anchoring at the next observed value.

    def _check_tangle_distance_window(self, eventtime, current_tool):
        """Distance-window tangle detector.

        Triggers when the rolling pulses/mm ratio across the last
        ``tangle_window_extrude_mm`` mm of forward extrusion drops
        below ``tangle_ratio_threshold``.  See PLAN-tangle-detection.md
        for the full rationale.

        Pre-conditions (any False ⇒ wipe state, return):
            * ACE feed-assist active
            * RDM detect pin shows filament present
            * Nozzle sensor shows filament present
            * RDM encoder readable
            * Extruder resolvable

        Evaluation happens every TANGLE_DW_EVAL_EVERY_N_TICKS ticks;
        sample collection runs on every tick so the window timeline
        stays accurate regardless of the eval cadence.
        """
        # ── Pre-conditions ────────────────────────────────────────────
        if not self.manager.is_feed_assist_active():
            if self.tangle_debug and self._dw_samples:
                self._tlm_pending_simple_event = "DW_ABORT:feed_assist_lost"
            self._dw_wipe_state()
            return
        if not self.manager.get_switch_state(SENSOR_RDM):
            if self.tangle_debug and self._dw_samples:
                self._tlm_pending_simple_event = "DW_ABORT:rdm_cleared"
            self._dw_wipe_state()
            return
        if not self.manager.get_switch_state(SENSOR_TOOLHEAD):
            if self.tangle_debug and self._dw_samples:
                self._tlm_pending_simple_event = "DW_ABORT:toolhead_cleared"
            self._dw_wipe_state()
            return
        current_encoder = self.manager.get_rdm_encoder_pulse()
        if current_encoder is None:
            # RDM is not a filament_tracker — cannot run detector
            return
        if not self._resolve_extruder():
            return
        try:
            extruder_pos = self._get_extruder_pos(eventtime)
        except Exception as e:
            logging.warning(
                "ACE: distance-window extruder_pos failed: %s", e
            )
            return

        # ── Sample collection (every tick) ────────────────────────────
        # Forward-only summation: retracts contribute zero so they
        # cannot cancel earlier forward extrusion and accidentally
        # collapse the window.
        if self._dw_last_extruder_pos is not None:
            dext = extruder_pos - self._dw_last_extruder_pos
            if dext > 0:
                self._dw_cum_extrude += dext
        self._dw_last_extruder_pos = extruder_pos

        # Append the new sample.
        self._dw_samples.append(
            (eventtime, self._dw_cum_extrude, current_encoder)
        )

        # Drop oldest samples that fall outside the window.  Keep at
        # least one sample older than the window's leading edge so the
        # span is always ≥ tangle_window_extrude_mm when we evaluate.
        W = self.tangle_window_extrude_mm
        while (len(self._dw_samples) >= 2
               and (self._dw_cum_extrude - self._dw_samples[1][1]) >= W):
            self._dw_samples.pop(0)

        # ── Eval gating ───────────────────────────────────────────────
        self._dw_tick_counter += 1
        if self._dw_tick_counter < self.TANGLE_DW_EVAL_EVERY_N_TICKS:
            return
        self._dw_tick_counter = 0

        # ── Window evaluation ─────────────────────────────────────────
        oldest_t, oldest_ext, oldest_enc = self._dw_samples[0]
        extrude_in_window = self._dw_cum_extrude - oldest_ext
        if extrude_in_window < W:
            # Window not yet wide enough — not enough data to judge.
            return

        pulses_in_window = current_encoder - oldest_enc
        ratio = pulses_in_window / extrude_in_window

        if self.tangle_debug:
            logging.debug(
                "ACE: tangle/dw [T%d] ext=%.1fmm pulses=%d ratio=%.3f "
                "threshold=%.2f confirmed=%d/%d",
                current_tool, extrude_in_window, pulses_in_window, ratio,
                self.tangle_ratio_threshold,
                self._dw_confirmed_count, self.tangle_confirmation_count,
            )

        if ratio < self.tangle_ratio_threshold:
            self._dw_confirmed_count += 1
            if self.tangle_debug:
                self._tlm_pending_simple_event = (
                    "DW_SUSPICIOUS:%d/%d"
                    % (self._dw_confirmed_count,
                       self.tangle_confirmation_count)
                )
            if self._dw_confirmed_count >= self.tangle_confirmation_count:
                if self.tangle_debug:
                    self._tlm_pending_simple_event = "DW_TANGLE_FIRED"
                logging.warning(
                    "ACE: TANGLE DETECTED (distance_window) on T%d — "
                    "%.1fmm extruded with only %d encoder pulses "
                    "(ratio %.3f < threshold %.2f, confirmed %dx)",
                    current_tool, extrude_in_window, pulses_in_window,
                    ratio, self.tangle_ratio_threshold,
                    self._dw_confirmed_count,
                )
                self._dw_wipe_state()
                self._handle_tangle_detected(current_tool)
        else:
            # Ratio recovered — reset the confirmation counter.
            if self._dw_confirmed_count > 0 and self.tangle_debug:
                self._tlm_pending_simple_event = "DW_RECOVERED"
            self._dw_confirmed_count = 0

    def _handle_tangle_detected(self, tool_index):
        """Handle a confirmed spool tangle.

        Pauses the print and shows a Mainsail prompt informing the user
        that a tangle was detected.  Resets the tangle window so that
        after the user resolves the tangle and resumes, detection starts
        fresh.

        Args:
            tool_index: Global tool index where the tangle was detected.
        """
        self.runout_handling_in_progress = True
        self._tangle_runout_pos = None

        try:
            self.gcode.respond_info(
                f"ACE: Spool tangle detected on T{tool_index}! "
                f"Filament stuck between spool and RDM. Pausing print."
            )
            self._pause_for_runout()

            # Build prompt
            self.gcode.run_script_from_command(
                'RESPOND TYPE=command MSG="action:prompt_begin Spool Tangle Detected"'
            )
            self.gcode.run_script_from_command(
                f'RESPOND TYPE=command MSG="action:prompt_text '
                f'Spool tangle detected on T{tool_index}! '
                f'The extruder is consuming the tube buffer but no filament '
                f'is passing through the RDM encoder. '
                f'Check the spool for tangles, then resume."'
            )
            self.gcode.run_script_from_command(
                'RESPOND TYPE=command MSG="action:prompt_footer_button '
                'Resume|RESUME|primary"'
            )
            self.gcode.run_script_from_command(
                'RESPOND TYPE=command MSG="action:prompt_footer_button '
                'Cancel Print|CANCEL_PRINT|error"'
            )
            self.gcode.run_script_from_command(
                'RESPOND TYPE=command MSG="action:prompt_show"'
            )
        except Exception as e:
            self.gcode.respond_info(f"ACE: Tangle handling error: {e}")
        finally:
            self.runout_handling_in_progress = False

    def _show_runout_prompt(self, tool_index, instance_num, local_slot, material, color):
        """
        Show simple Mainsail prompt for runout with CANCEL/RESUME buttons.

        Args:
            tool_index: Global tool index (e.g., 0-7)
            instance_num: ACE instance number
            local_slot: Local slot number on instance
            material: Material type (e.g., "PLA")
            color: RGB color array [r, g, b]
        """
        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_begin Filament Runout"'
        )

        color_str = f"RGB({color[0]},{color[1]},{color[2]})"
        prompt_text = (
            f"Filament runout detected on Tool T{tool_index}! "
            f"Please refill ACE {instance_num} Slot {local_slot} with {material} filament "
            f"(Color: {color_str})."
        )

        self.gcode.run_script_from_command(
            f'RESPOND TYPE=command MSG="action:prompt_text {prompt_text}"'
        )

        self.gcode.run_script_from_command(
            f'RESPOND TYPE=command MSG="action:prompt_button Retry T{tool_index}|T{tool_index}|primary"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_button Extrude 100mm|'
            '_EXTRUDE LENGTH=100 SPEED=300|secondary"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_button Retract 100mm|'
            '_RETRACT LENGTH=100 SPEED=300|secondary"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_footer_button Resume|RESUME|primary"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_footer_button Cancel Print|CANCEL_PRINT|error"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_show"'
        )

    def _handle_runout_detected(self, tool_index):
        """
        Handle filament runout detection.

        Flow:
        1. Pause the print immediately
        2. Show interactive prompt with CANCEL/RESUME options
        3. Check if endless spool is enabled
        4. If enabled: try to find exact material/color match in other slots
        5. If match found: close prompt, perform automatic tool swap and resume
        6. If no match or endless spool disabled: stay paused (user must refill)

        Resets sensor tracking to prevent repeated triggers.

        Args:
            tool_index: Tool index where runout was detected
        """
        self.gcode.respond_info(f"ACE: Runout detected on T{tool_index}")
        self.runout_handling_in_progress = True
        self.prev_toolhead_sensor_state = None
        self._runout_false_count = 0

        try:
            # Step 1: PAUSE immediately
            self._pause_for_runout()

            # Get runout details for prompt
            instance_num = get_instance_from_tool(tool_index)
            material = "unknown"
            color = [0, 0, 0]
            local_slot = -1

            if instance_num >= 0:
                local_slot = get_local_slot(tool_index, instance_num)
                ace_inst = ACE_INSTANCES.get(instance_num)
                if ace_inst and 0 <= local_slot < len(ace_inst.inventory):
                    inv = ace_inst.inventory[local_slot]
                    material = inv.get("material", "unknown")
                    color = inv.get("color", [0, 0, 0])
                    self.gcode.respond_info(
                        f"ACE: Runout on T{tool_index}: {material} "
                        f"RGB({color[0]},{color[1]},{color[2]})"
                    )

            # Step 3: Show simple interactive prompt
            self._show_runout_prompt(tool_index, instance_num, local_slot, material, color)

            # Step 4: Check if endless spool is enabled
            endless_spool_enabled = self.manager.state.get("ace_endless_spool_enabled", False)

            if not endless_spool_enabled:
                self.gcode.respond_info(
                    "ACE: Endless spool disabled. Staying paused. "
                    "Refill spool and resume manually."
                )
                return

            # Step 5: Try to find exact material/color match
            next_tool = self.endless_spool.find_exact_match(tool_index)
            if next_tool < 0:
                self.gcode.respond_info(
                    f"ACE: No endless spool match found for T{tool_index}. "
                    f"Staying paused. Refill spool or load matching material."
                )
                return

            # Step 6: Match found - close prompt and execute automatic swap
            self.gcode.respond_info(
                f"ACE: Endless spool match found: T{tool_index} → T{next_tool}"
            )

            # Close prompt before auto-swap (since we're handling it automatically)
            self.gcode.run_script_from_command(
                'RESPOND TYPE=command MSG="action:prompt_end"'
            )

            self.endless_spool.execute_swap(tool_index, next_tool)

        except Exception as e:
            self.gcode.respond_info(f"ACE: Runout handling error: {e}")
        finally:
            self.runout_handling_in_progress = False

    def _pause_for_runout(self):
        """
        Pause the print for runout handling.

        Uses Klipper's PAUSE command to stop the print and move
        toolhead to safe position.
        """
        try:
            self.gcode.respond_info("ACE: Pausing print")
            self.gcode.run_script_from_command("PAUSE")
        except Exception as e:
            self.gcode.respond_info(f"ACE: Error pausing print: {e}")

    # ========== Read-only Baseline Telemetry ==========
    #
    # The telemetry path is strictly observational: it samples raw encoder
    # pulses, extruder position and sensor states every monitor tick and
    # writes them to a TSV file (per tick) and to klippy.log (aggregated
    # once a second).  It does NOT touch tangle detection state and never
    # raises out of the monitor.

    def _get_length_per_pulse(self):
        """Return the RDM tracker's configured length_per_pulse, or None.

        Reads the underlying filament_tracker's ``length_per_pulse``
        attribute via the FilamentTrackerAdapter on the manager.  Returns
        ``None`` when the RDM sensor is a plain filament_switch_sensor
        (no encoder geometry available).
        """
        try:
            sensor = self.manager.sensors.get(SENSOR_RDM)
            if sensor is None:
                return None
            tracker = getattr(sensor, "_tracker", None)
            if tracker is None:
                return None
            return getattr(tracker, "length_per_pulse", None)
        except Exception:
            return None

    def _open_telemetry_log(self):
        """Open the TSV telemetry log file lazily.

        Returns True on success, False on failure.  A failure disables
        further retry attempts so a bad path cannot spam the Klipper log
        on every monitor tick.
        """
        if self._tlm_file_handle is not None:
            return True
        if self._tlm_file_open_failed:
            return False
        try:
            path = os.path.expanduser(
                self.tangle_telemetry_log
                or "~/printer_data/logs/ace-tangle-telemetry.log"
            )
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            # Truncate on open so each Klipper session starts with a fresh
            # file — accumulated multi-session logs are useless for analysis
            # and the file would otherwise grow unbounded across restarts.
            # Line-buffered so tail records survive a crash mid-write.
            self._tlm_file_handle = open(path, "w", buffering=1)
            self._tlm_file_handle.write(
                "# ACE tangle baseline telemetry — read-only, "
                "no detection logic\n"
                "# Columns: eventtime tool encoder_pulse extruder_pos "
                "d_encoder d_extruder print_state feed_assist rdm "
                "toolhead simple_event layer toolchange filament_pos\n"
            )
            self._tlm_resolved_log_path = path
            return True
        except Exception as e:
            logging.warning(
                "ACE: tangle telemetry log open failed (%s) — disabling "
                "file output for this session", e
            )
            self._tlm_file_open_failed = True
            return False

    def _emit_telemetry_start_header(self):
        """Log a one-time START line summarising the encoder calibration."""
        if self._tlm_started:
            return
        self._tlm_started = True
        actual_lpp = self._get_length_per_pulse()
        if isinstance(actual_lpp, (int, float)):
            actual_str = f"{actual_lpp:.6f}"
        else:
            actual_str = "unavailable"
        log_path = getattr(
            self, "_tlm_resolved_log_path", "<file open failed>"
        )
        header_line = (
            f"ACE: tangle-tlm START "
            f"length_per_pulse={actual_str} "
            f"(theoretical={self.THEORETICAL_LENGTH_PER_PULSE:.11f}) "
            f"log={log_path}"
        )
        logging.info(header_line)
        if self._tlm_file_handle is not None:
            try:
                self._tlm_file_handle.write(f"# {header_line}\n")
            except Exception:
                pass

    def _log_tangle_telemetry(self, eventtime, current_tool):
        """Read-only baseline telemetry — one TSV row per tick, one
        klippy.log summary per second.

        Captures the raw encoder pulse count, extruder position and
        sensor states.  Picks up any simple-detector event the previous
        ``_check_tangle`` call left in ``_tlm_pending_simple_event`` and
        logs it as the ``simple_event`` column.  All I/O is wrapped so
        a broken sensor or closed file never raises out of here.

        Gating: ticks are skipped while feed-assist is inactive, except
        for the on→off and off→on transitions themselves.  This keeps
        the TSV focused on the actual extrusion phase (no QGL / bed-mesh
        / homing noise) while still capturing the boundary events that
        bracket each print or toolchange.
        """
        # ---- Gate: skip idle ticks, log transitions ----
        try:
            feed_assist_active = self.manager.is_feed_assist_active()
        except Exception:
            feed_assist_active = False
        transition = (
            self._tlm_prev_feed_assist is not None
            and self._tlm_prev_feed_assist != feed_assist_active
        )
        # Always update prev, even when skipping.
        self._tlm_prev_feed_assist = feed_assist_active
        if not feed_assist_active and not transition:
            return
        # On the off→on transition, reset the delta baseline so the first
        # active tick reports d=0 instead of a gap accumulated across the
        # idle period.
        if transition and feed_assist_active:
            self._tlm_last_encoder = None
            self._tlm_last_extruder_pos = None
            self._tlm_last_summary_time = None
            self._tlm_bucket_d_encoder = 0
            self._tlm_bucket_d_extruder = 0.0

        self._open_telemetry_log()
        if not self._tlm_started:
            self._emit_telemetry_start_header()

        # ---- Snapshot the current state ----
        encoder_pulse = self.manager.get_rdm_encoder_pulse()
        encoder_value = encoder_pulse if encoder_pulse is not None else -1

        extruder_pos = 0.0
        if self._resolve_extruder():
            try:
                extruder_pos = self._get_extruder_pos(eventtime)
            except Exception:
                extruder_pos = 0.0

        try:
            feed_assist = 1 if self.manager.is_feed_assist_active() else 0
        except Exception:
            feed_assist = 0
        try:
            rdm = 1 if self.manager.get_switch_state(SENSOR_RDM) else 0
        except Exception:
            rdm = 0
        try:
            toolhead = (
                1 if self.manager.get_switch_state(SENSOR_TOOLHEAD) else 0
            )
        except Exception:
            toolhead = 0

        print_state = self.last_print_state or "unknown"

        # ---- Current layer (best-effort, '-' if not set by slicer) ----
        # Slicers expose layer via SET_PRINT_STATS_INFO CURRENT_LAYER=...
        # in their G-code; the value lands on print_stats.info.current_layer
        # and is None until first written.  We log it raw so the analyst
        # can correlate stalls with first-layer / specific layers.
        current_layer = "-"
        try:
            print_stats = self.printer.lookup_object("print_stats", None)
            if print_stats is not None:
                info = print_stats.get_status(eventtime).get("info", {})
                layer_val = info.get("current_layer")
                if layer_val is not None:
                    current_layer = str(layer_val)
        except Exception:
            pass

        # ---- Toolchange + filament position ----
        # toolchange_in_progress is set by the @toolchange_in_progress_guard
        # decorator around AceManager methods that move filament; while it
        # is True the runout monitor early-exits anyway, but having it in
        # the TSV lets us correlate stalls / encoder anomalies with the
        # tail of a TC sequence.  filament_pos tracks where the filament
        # currently lives (bowden / splitter / toolhead / nozzle) — this
        # changes the encoder's expected behaviour completely.
        try:
            toolchange = 1 if self.manager.toolchange_in_progress else 0
        except Exception:
            toolchange = 0
        try:
            fpos = self.manager.state.get("ace_filament_pos", None)
            filament_pos = str(fpos) if fpos is not None else "-"
        except Exception:
            filament_pos = "-"

        # ---- Deltas ----
        if self._tlm_last_encoder is None or encoder_value < 0:
            d_encoder = 0
        else:
            d_encoder = encoder_value - self._tlm_last_encoder
        if self._tlm_last_extruder_pos is None:
            d_extruder = 0.0
        else:
            d_extruder = extruder_pos - self._tlm_last_extruder_pos
        if encoder_value >= 0:
            self._tlm_last_encoder = encoder_value
        self._tlm_last_extruder_pos = extruder_pos

        # Event tagged by _check_tangle on the previous tick.
        simple_event = self._tlm_pending_simple_event
        self._tlm_pending_simple_event = ""

        # ---- TSV row ----
        if self._tlm_file_handle is not None:
            try:
                self._tlm_file_handle.write(
                    f"{eventtime:.3f}\t{current_tool}\t{encoder_value}\t"
                    f"{extruder_pos:.3f}\t{d_encoder}\t{d_extruder:.3f}\t"
                    f"{print_state}\t{feed_assist}\t{rdm}\t{toolhead}\t"
                    f"{simple_event}\t{current_layer}\t"
                    f"{toolchange}\t{filament_pos}\n"
                )
            except Exception as e:
                logging.warning(
                    "ACE: tangle telemetry write failed (%s) — disabling "
                    "file output for this session", e
                )
                try:
                    self._tlm_file_handle.close()
                except Exception:
                    pass
                self._tlm_file_handle = None
                self._tlm_file_open_failed = True

        # ---- 1-second aggregation for klippy.log ----
        self._tlm_bucket_d_encoder += d_encoder
        self._tlm_bucket_d_extruder += d_extruder
        if self._tlm_last_summary_time is None:
            self._tlm_last_summary_time = eventtime
        elapsed = eventtime - self._tlm_last_summary_time
        if elapsed >= 1.0:
            per_s_enc = (
                self._tlm_bucket_d_encoder / elapsed if elapsed > 0 else 0
            )
            per_s_extr = (
                self._tlm_bucket_d_extruder / elapsed if elapsed > 0 else 0
            )
            logging.info(
                "ACE: tangle-tlm T%d enc=%d extr=%.1fmm "
                "%senc/s=%.1f %sextr/s=%.2fmm fa=%d rdm=%d th=%d",
                current_tool, encoder_value, extruder_pos,
                "Δ", per_s_enc, "Δ", per_s_extr,
                feed_assist, rdm, toolhead,
            )
            self._tlm_bucket_d_encoder = 0
            self._tlm_bucket_d_extruder = 0.0
            self._tlm_last_summary_time = eventtime
