"""
Tests for tangle detection in RunoutMonitor.

Tangle detection fires when ALL of these hold simultaneously:
    1. Print state is "printing"
    2. ACE feed-assist is active
    3. RDM detect pin shows filament present
    4. Nozzle sensor shows filament present
    5. Extruder moved >= TANGLE_DETECTION_LENGTH since window reset
    6. RDM encoder pulse count has NOT changed since window reset

Each test disables exactly one condition and verifies no false positive,
then a positive test confirms detection when all conditions are true.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch, call
from ace.runout_monitor import RunoutMonitor
from ace.config import SENSOR_TOOLHEAD, SENSOR_RDM


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_monitor(tangle_detection=True):
    """Create a RunoutMonitor wired for tangle detection tests.

    Returns (monitor, printer, gcode, reactor, manager) so tests can
    configure behaviour on the mocks.
    """
    printer = Mock()
    gcode = Mock()
    reactor = Mock()
    reactor.NOW = 0.0
    reactor.NEVER = float("inf")
    endless_spool = Mock()
    manager = Mock()
    manager.toolchange_in_progress = False
    manager.state = Mock()
    manager.state.get = Mock(return_value=-1)

    monitor = RunoutMonitor(
        printer, gcode, reactor, endless_spool, manager,
        runout_debounce_count=1,
        tangle_detection=tangle_detection,
    )
    return monitor, printer, gcode, reactor, manager


def _setup_printing_state(monitor, printer, manager, gcode,
                          feed_assist=True,
                          rdm_present=True,
                          nozzle_present=True,
                          encoder_pulse=100):
    """Configure mocks so the monitor loop reaches tangle checking.

    Sets print state to "printing", sensor states, feed_assist, and
    the encoder pulse count.  Also puts the monitor in a state where
    it has already established a baseline (prev_toolhead_sensor_state
    is set) so the loop doesn't short-circuit.
    """
    # print_stats returns "printing"
    stats_obj = Mock()
    stats_obj.get_status.return_value = {"state": "printing"}
    printer.lookup_object.side_effect = _printer_lookup(stats_obj, encoder_pulse)

    # save_variables
    save_vars = Mock()
    save_vars.allVariables = {"ace_current_index": 0}
    manager.state = Mock()
    manager.state.get = lambda key, default=None: save_vars.allVariables.get(key, default)
    original_side_effect = printer.lookup_object.side_effect

    def lookup(name, default=None):
        if name == "print_stats":
            return stats_obj
        if name == "save_variables":
            return save_vars
        if name == "extruder":
            ext = Mock()
            ext.find_past_position = Mock(return_value=0.0)
            return ext
        if name == "mcu":
            mcu = Mock()
            mcu.estimated_print_time = Mock(return_value=0.0)
            return mcu
        if default is not None:
            return default
        raise Exception(f"Object {name} not found")

    printer.lookup_object.side_effect = lookup

    # Sensor states
    def get_switch(sensor_name):
        if sensor_name == SENSOR_RDM:
            return rdm_present
        if sensor_name == SENSOR_TOOLHEAD:
            return nozzle_present
        return False

    manager.get_switch_state.side_effect = get_switch
    manager.is_feed_assist_active.return_value = feed_assist
    manager.get_rdm_encoder_pulse.return_value = encoder_pulse

    # Pre-set monitor state so it doesn't short-circuit on baseline init
    monitor.prev_toolhead_sensor_state = nozzle_present
    monitor.last_printing_active = True
    monitor.last_print_state = "printing"
    monitor.runout_detection_active = True
    monitor.runout_handling_in_progress = False


def _printer_lookup(stats_obj, encoder_pulse):
    """Build a printer.lookup_object side_effect function."""
    def lookup(name, default=None):
        if name == "print_stats":
            return stats_obj
        if name == "save_variables":
            sv = Mock()
            sv.allVariables = {"ace_current_index": 0}
            return sv
        if name == "extruder":
            ext = Mock()
            ext.find_past_position = Mock(return_value=0.0)
            return ext
        if name == "mcu":
            mcu = Mock()
            mcu.estimated_print_time = Mock(return_value=0.0)
            return mcu
        if default is not None:
            return default
        raise Exception(f"Object {name} not found")
    return lookup


# ── Initialization ───────────────────────────────────────────────────────

class TestTangleDetectionInit:
    """Verify tangle_detection config is handled correctly."""

    def test_tangle_disabled_by_default(self):
        """Default: tangle detection off."""
        monitor, *_ = _make_monitor(tangle_detection=False)
        assert monitor.tangle_detection_enabled is False
        assert monitor._tangle_runout_pos is None
        assert monitor._tangle_encoder_snapshot is None

    def test_tangle_enabled(self):
        """Explicitly enabling tangle detection."""
        monitor, *_ = _make_monitor(tangle_detection=True)
        assert monitor.tangle_detection_enabled is True

    def test_default_detection_length(self):
        """Default detection length is DEFAULT_TANGLE_DETECTION_LENGTH."""
        monitor, *_ = _make_monitor()
        assert monitor.tangle_detection_length == RunoutMonitor.DEFAULT_TANGLE_DETECTION_LENGTH

    def test_custom_detection_length(self):
        """tangle_detection_length overrides the default."""
        printer = Mock()
        gcode = Mock()
        reactor = Mock()
        reactor.NOW = 0.0
        reactor.NEVER = float("inf")
        monitor = RunoutMonitor(
            printer, gcode, reactor, Mock(), Mock(),
            tangle_detection=True,
            tangle_detection_length=25.0,
        )
        assert monitor.tangle_detection_length == 25.0


# ── Condition gates (each disables one condition) ────────────────────────

class TestTangleConditionGates:
    """Each test ensures that removing one condition prevents detection."""

    def setup_method(self):
        """Create a tangle-enabled monitor."""
        (self.monitor, self.printer, self.gcode,
         self.reactor, self.manager) = _make_monitor(tangle_detection=True)

    def _run_check(self, extruder_pos=0.0):
        """Run _check_tangle with the given extruder position."""
        # Resolve extruder so _check_tangle can work
        ext = Mock()
        ext.find_past_position = Mock(return_value=extruder_pos)
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = mcu.estimated_print_time

        self.monitor._check_tangle(1000.0, current_tool=0)

    def test_no_tangle_when_feed_assist_off(self):
        """Condition 2 fails: feed-assist not active."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            feed_assist=False, encoder_pulse=100)

        # Set a window manually
        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100

        self._run_check(extruder_pos=20.0)  # Past window

        # Should have reset window, not fired
        assert self.monitor._tangle_runout_pos is None
        self.gcode.run_script_from_command.assert_not_called()

    def test_no_tangle_when_rdm_absent(self):
        """Condition 3 fails: RDM shows no filament."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            rdm_present=False, encoder_pulse=100)

        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100

        self._run_check(extruder_pos=20.0)

        assert self.monitor._tangle_runout_pos is None
        self.gcode.run_script_from_command.assert_not_called()

    def test_no_tangle_when_nozzle_absent(self):
        """Condition 4 fails: nozzle sensor shows no filament."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            nozzle_present=False, encoder_pulse=100)

        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100

        self._run_check(extruder_pos=20.0)

        assert self.monitor._tangle_runout_pos is None
        self.gcode.run_script_from_command.assert_not_called()

    def test_no_tangle_when_extruder_below_window(self):
        """Condition 5 fails: extruder hasn't moved enough."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=100)

        # Window at 15.0, extruder at 5.0
        self.monitor._tangle_runout_pos = 15.0
        self.monitor._tangle_encoder_snapshot = 100

        self._run_check(extruder_pos=5.0)

        # No detection, window still active
        assert self.monitor._tangle_runout_pos == 15.0
        self.gcode.run_script_from_command.assert_not_called()

    def test_no_tangle_when_encoder_moves(self):
        """Condition 6 fails: encoder pulse count changed → filament is flowing."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=105)  # Changed from snapshot

        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100  # Different from 105

        self._run_check(extruder_pos=20.0)

        # Window should have been reset (not fired)
        # encoder moved → _reset_tangle_window was called
        assert self.monitor._tangle_encoder_snapshot == 105
        self.gcode.run_script_from_command.assert_not_called()

    def test_no_tangle_when_disabled(self):
        """tangle_detection: False → _check_tangle never runs."""
        monitor, printer, gcode, reactor, manager = _make_monitor(
            tangle_detection=False)
        _setup_printing_state(
            monitor, printer, manager, gcode, encoder_pulse=100)

        # Even with a rigged window it should never fire because the
        # calling code checks tangle_detection_enabled before calling
        assert monitor.tangle_detection_enabled is False

    def test_no_tangle_when_rdm_not_tracker(self):
        """If RDM is a plain switch sensor, encoder is None → skip."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=100)

        # Override: encoder pulse returns None (not a tracker)
        self.manager.get_rdm_encoder_pulse.return_value = None

        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100

        self._run_check(extruder_pos=20.0)

        # Should not fire, should not crash
        self.gcode.run_script_from_command.assert_not_called()


# ── Positive detection ──────────────────────────────────────────────────

class TestTanglePositiveDetection:
    """Verify tangle fires when all 6 conditions hold."""

    def setup_method(self):
        (self.monitor, self.printer, self.gcode,
         self.reactor, self.manager) = _make_monitor(tangle_detection=True)

    def test_tangle_fires_when_all_conditions_met(self):
        """All conditions true → pause + prompt."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            feed_assist=True, rdm_present=True, nozzle_present=True,
            encoder_pulse=100)

        # Set window: runout pos = 10, snapshot = 100
        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 100

        # Resolve extruder
        ext = Mock()
        ext.find_past_position = Mock(return_value=20.0)  # Past 10.0
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = mcu.estimated_print_time

        self.monitor._check_tangle(1000.0, current_tool=0)

        # Should have called PAUSE
        pause_calls = [
            c for c in self.gcode.run_script_from_command.call_args_list
            if "PAUSE" in str(c)
        ]
        assert len(pause_calls) > 0, "PAUSE should have been called"

        # Should have shown a tangle prompt
        prompt_calls = [
            c for c in self.gcode.run_script_from_command.call_args_list
            if "Spool Tangle" in str(c)
        ]
        assert len(prompt_calls) > 0, "Tangle prompt should have been shown"

        # Window should be reset after detection
        assert self.monitor._tangle_runout_pos is None

    def test_tangle_logs_warning(self):
        """Tangle detection logs a warning with details."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            feed_assist=True, rdm_present=True, nozzle_present=True,
            encoder_pulse=50)

        self.monitor._tangle_runout_pos = 10.0
        self.monitor._tangle_encoder_snapshot = 50

        ext = Mock()
        ext.find_past_position = Mock(return_value=25.0)
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = mcu.estimated_print_time

        self.monitor._check_tangle(1000.0, current_tool=2)

        # respond_info should mention tangle
        info_calls = [
            str(c) for c in self.gcode.respond_info.call_args_list
        ]
        tangle_msgs = [c for c in info_calls if "tangle" in c.lower()]
        assert len(tangle_msgs) > 0


# ── Window management ────────────────────────────────────────────────────

class TestTangleWindowManagement:
    """Verify the tangle detection window resets correctly."""

    def setup_method(self):
        (self.monitor, self.printer, self.gcode,
         self.reactor, self.manager) = _make_monitor(tangle_detection=True)

    def test_window_initializes_on_first_check(self):
        """First tangle check with no window → creates window."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=42)

        assert self.monitor._tangle_runout_pos is None

        # Resolve extruder for reset
        ext = Mock()
        ext.find_past_position = Mock(return_value=100.0)
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = mcu.estimated_print_time

        self.monitor._check_tangle(1000.0, current_tool=0)

        assert self.monitor._tangle_runout_pos == 100.0 + self.monitor.tangle_detection_length
        assert self.monitor._tangle_encoder_snapshot == 42

    def test_window_resets_on_encoder_activity(self):
        """Encoder pulse change → window resets to new position."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=55)

        self.monitor._tangle_runout_pos = 50.0
        self.monitor._tangle_encoder_snapshot = 50  # Different from 55

        ext = Mock()
        ext.find_past_position = Mock(return_value=80.0)
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = mcu.estimated_print_time

        self.monitor._check_tangle(1000.0, current_tool=0)

        # Window reset to current extruder pos + detection length
        assert self.monitor._tangle_runout_pos == 80.0 + self.monitor.tangle_detection_length
        assert self.monitor._tangle_encoder_snapshot == 55

    def test_toolchange_resets_window(self):
        """Toolchange in progress → window cleared."""
        self.monitor._tangle_runout_pos = 100.0
        self.monitor._tangle_encoder_snapshot = 50

        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode)
        self.manager.toolchange_in_progress = True

        self.monitor._monitor_runout(1000.0)

        assert self.monitor._tangle_runout_pos is None

    def test_print_stop_resets_window(self):
        """Print stopping resets the tangle window."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode)

        self.monitor._tangle_runout_pos = 100.0
        self.monitor._tangle_encoder_snapshot = 50

        # Simulate print just stopped
        self.monitor.last_printing_active = True

        # Override print_stats to return "complete"
        stats_obj = Mock()
        stats_obj.get_status.return_value = {"state": "complete"}

        def lookup(name, default=None):
            if name == "print_stats":
                return stats_obj
            if name == "save_variables":
                sv = Mock()
                sv.allVariables = {"ace_current_index": 0}
                return sv
            if default is not None:
                return default
            raise Exception(f"Object {name} not found")

        self.printer.lookup_object.side_effect = lookup

        self.monitor._monitor_runout(1000.0)

        assert self.monitor._tangle_runout_pos is None

    def test_pause_resets_window(self):
        """Paused state resets the tangle window."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode)

        self.monitor._tangle_runout_pos = 100.0

        # Override print_stats to return "paused"
        stats_obj = Mock()
        stats_obj.get_status.return_value = {"state": "paused"}

        def lookup(name, default=None):
            if name == "print_stats":
                return stats_obj
            if name == "save_variables":
                sv = Mock()
                sv.allVariables = {"ace_current_index": 0}
                return sv
            if default is not None:
                return default
            raise Exception(f"Object {name} not found")

        self.printer.lookup_object.side_effect = lookup

        self.monitor._monitor_runout(1000.0)

        assert self.monitor._tangle_runout_pos is None


# ── Integration with monitor loop ────────────────────────────────────────

class TestTangleInMonitorLoop:
    """Verify _check_tangle is called from the main monitor loop."""

    def setup_method(self):
        (self.monitor, self.printer, self.gcode,
         self.reactor, self.manager) = _make_monitor(tangle_detection=True)

    def test_check_tangle_called_during_normal_printing(self):
        """_check_tangle runs on each normal monitoring cycle."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=100)

        with patch.object(self.monitor, '_check_tangle') as mock_check:
            self.monitor._monitor_runout(1000.0)
            mock_check.assert_called_once()

    def test_check_tangle_not_called_when_disabled(self):
        """tangle_detection=False → _check_tangle never called."""
        monitor, printer, gcode, reactor, manager = _make_monitor(
            tangle_detection=False)
        _setup_printing_state(
            monitor, printer, manager, gcode, encoder_pulse=100)

        with patch.object(monitor, '_check_tangle') as mock_check:
            monitor._monitor_runout(1000.0)
            mock_check.assert_not_called()

    def test_check_tangle_not_called_during_runout_handling(self):
        """Active runout handling suppresses tangle checks."""
        _setup_printing_state(
            self.monitor, self.printer, self.manager, self.gcode,
            encoder_pulse=100)
        self.monitor.runout_handling_in_progress = True

        with patch.object(self.monitor, '_check_tangle') as mock_check:
            self.monitor._monitor_runout(1000.0)
            mock_check.assert_not_called()


# ── Extruder resolution ─────────────────────────────────────────────────

class TestExtruderResolution:
    """Verify lazy extruder/MCU lookup works correctly."""

    def setup_method(self):
        (self.monitor, self.printer, self.gcode,
         self.reactor, self.manager) = _make_monitor(tangle_detection=True)

    def test_resolve_extruder_success(self):
        """Extruder and MCU resolve on first call."""
        ext = Mock()
        mcu = Mock()
        mcu.estimated_print_time = Mock(return_value=0.0)

        def lookup(name, default=None):
            if name == "extruder":
                return ext
            if name == "mcu":
                return mcu
            raise Exception(f"Not found: {name}")

        self.printer.lookup_object.side_effect = lookup

        assert self.monitor._resolve_extruder() is True
        assert self.monitor._extruder is ext
        assert self.monitor._estimated_print_time is mcu.estimated_print_time

    def test_resolve_extruder_failure(self):
        """Missing extruder → returns False, no crash."""
        self.printer.lookup_object.side_effect = Exception("not found")

        assert self.monitor._resolve_extruder() is False
        assert self.monitor._extruder is None

    def test_resolve_extruder_cached(self):
        """Second call doesn't re-lookup."""
        ext = Mock()
        self.monitor._extruder = ext
        self.monitor._estimated_print_time = Mock()

        result = self.monitor._resolve_extruder()

        assert result is True
        # lookup_object should NOT have been called
        self.printer.lookup_object.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# Read-only baseline telemetry — smoke tests
# ─────────────────────────────────────────────────────────────────────────

def _make_telemetry_monitor(tmp_path, tangle_debug=True):
    """Build a RunoutMonitor with telemetry enabled, pointed at tmp_path."""
    printer = Mock()
    gcode = Mock()
    reactor = Mock()
    reactor.NOW = 0.0
    endless_spool = Mock()
    manager = Mock()
    manager.toolchange_in_progress = False
    manager.state = Mock()
    manager.state.get = Mock(return_value=-1)
    manager.sensors = {}  # no RDM tracker by default

    log_path = str(tmp_path / "ace-tangle-telemetry.log")

    monitor = RunoutMonitor(
        printer, gcode, reactor, endless_spool, manager,
        runout_debounce_count=1,
        tangle_detection=False,
        tangle_debug=tangle_debug,
        tangle_telemetry_log=log_path,
    )
    # Pre-resolve so _resolve_extruder() returns True without lookup_object.
    monitor._extruder = Mock()
    monitor._estimated_print_time = Mock(return_value=0.0)

    # Sensible default sensor mocks — overridable per test.
    manager.get_rdm_encoder_pulse.return_value = 100
    manager.is_feed_assist_active.return_value = True
    manager.get_switch_state.return_value = True

    return monitor, manager, log_path


def _data_rows(path):
    """Read TSV data rows (skip the # comment header lines)."""
    with open(path) as f:
        return [line.rstrip("\n") for line in f if not line.startswith("#")]


class TestTangleTelemetry:
    """Smoke tests for the read-only baseline telemetry path.

    These tests do NOT assert anything about tangle DETECTION behaviour
    — the telemetry layer is supposed to be strictly observational.
    """

    def test_writes_tsv_row_per_tick(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        monitor._log_tangle_telemetry(0.25, current_tool=0)
        monitor._log_tangle_telemetry(0.50, current_tool=0)

        assert len(_data_rows(log_path)) == 2

    def test_deltas_computed_correctly_across_ticks(self, tmp_path):
        """Repro for the hardware-observed bug where d_encoder / d_extruder
        stayed at 0 across ticks even though the raw values were rising."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)

        # Tick 1: encoder=100, extruder=10.0  → first tick, deltas should be 0
        manager.get_rdm_encoder_pulse.return_value = 100
        monitor._get_extruder_pos = Mock(return_value=10.0)
        monitor._log_tangle_telemetry(0.25, current_tool=0)

        # Tick 2: encoder=105, extruder=15.0  → d_enc=5, d_extr=5.000
        manager.get_rdm_encoder_pulse.return_value = 105
        monitor._get_extruder_pos = Mock(return_value=15.0)
        monitor._log_tangle_telemetry(0.50, current_tool=0)

        # Tick 3: encoder=107, extruder=18.5  → d_enc=2, d_extr=3.500
        manager.get_rdm_encoder_pulse.return_value = 107
        monitor._get_extruder_pos = Mock(return_value=18.5)
        monitor._log_tangle_telemetry(0.75, current_tool=0)

        rows = _data_rows(log_path)
        assert len(rows) == 3
        # Row 1: deltas zero (no previous)
        c1 = rows[0].split("\t")
        assert c1[4] == "0", f"row 1 d_encoder: {c1}"
        assert c1[5] == "0.000", f"row 1 d_extruder: {c1}"
        # Row 2: d_encoder=5, d_extruder=5.000
        c2 = rows[1].split("\t")
        assert c2[4] == "5", f"row 2 d_encoder: {c2}"
        assert c2[5] == "5.000", f"row 2 d_extruder: {c2}"
        # Row 3: d_encoder=2, d_extruder=3.500
        c3 = rows[2].split("\t")
        assert c3[4] == "2", f"row 3 d_encoder: {c3}"
        assert c3[5] == "3.500", f"row 3 d_extruder: {c3}"

    def test_header_written_once(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        for t in (0.25, 0.50, 0.75):
            monitor._log_tangle_telemetry(t, current_tool=0)

        with open(log_path) as f:
            content = f.read()
        # One START line, regardless of how many ticks ran.
        assert content.count("START") == 1
        # Theoretical reference value is embedded for later comparison.
        assert "theoretical=1.86532063807" in content

    def test_no_crash_when_extruder_unresolvable(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        # Drop the pre-resolved extruder and force lookup_object to fail.
        monitor._extruder = None
        monitor._estimated_print_time = None
        monitor.printer.lookup_object = Mock(side_effect=Exception("nope"))

        # Must NOT raise.
        monitor._log_tangle_telemetry(0.25, current_tool=0)

        cols = _data_rows(log_path)[0].split("\t")
        # extruder_pos column → fallback 0.000
        assert cols[3] == "0.000"

    def test_no_crash_when_encoder_pulse_none(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        manager.get_rdm_encoder_pulse.return_value = None  # plain switch sensor

        monitor._log_tangle_telemetry(0.25, current_tool=0)

        cols = _data_rows(log_path)[0].split("\t")
        # encoder_pulse column → sentinel -1 when no tracker available
        assert cols[2] == "-1"

    def test_klippy_summary_emitted_after_one_second(self, tmp_path, caplog):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        with caplog.at_level("INFO"):
            monitor._log_tangle_telemetry(0.25, current_tool=0)
            monitor._log_tangle_telemetry(1.50, current_tool=0)  # >= 1s later

        summaries = [
            r.getMessage() for r in caplog.records
            if "tangle-tlm T0 enc=" in r.getMessage()
        ]
        assert len(summaries) >= 1

    def test_picks_up_pending_simple_event(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        monitor._tlm_pending_simple_event = "ABORT:feed_assist_lost"

        monitor._log_tangle_telemetry(0.25, current_tool=0)

        cols = _data_rows(log_path)[0].split("\t")
        # simple_event is the last column.
        assert cols[10] == "ABORT:feed_assist_lost"
        # And the pending slot must be cleared so the next tick is blank.
        assert monitor._tlm_pending_simple_event == ""

    def test_file_open_failure_disables_silently(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        with patch("builtins.open", side_effect=OSError("permission denied")):
            monitor._log_tangle_telemetry(0.25, current_tool=0)

        # The failure flag must latch so subsequent ticks do not retry.
        assert monitor._tlm_file_open_failed is True
        assert monitor._tlm_file_handle is None
        # Another tick must still not crash (file output already disabled).
        monitor._log_tangle_telemetry(0.50, current_tool=0)

    def test_log_file_truncated_on_each_session(self, tmp_path):
        """Each new monitor (= Klipper restart) starts with a fresh file —
        accumulated multi-session bloat is undesirable for analysis and
        the file would otherwise grow unbounded across restarts."""
        log_path = str(tmp_path / "ace-tangle-telemetry.log")

        # Session 1: write three rows.
        monitor1, manager1, _ = _make_telemetry_monitor(tmp_path)
        monitor1._get_extruder_pos = Mock(return_value=10.0)
        for t in (0.25, 0.50, 0.75):
            monitor1._log_tangle_telemetry(t, current_tool=0)
        assert len(_data_rows(log_path)) == 3
        # Simulate Klipper shutdown — release the file handle.
        monitor1._tlm_file_handle.close()
        monitor1._tlm_file_handle = None

        # Session 2 on the same path starts fresh.
        monitor2, manager2, _ = _make_telemetry_monitor(tmp_path)
        monitor2._get_extruder_pos = Mock(return_value=20.0)
        monitor2._log_tangle_telemetry(0.25, current_tool=0)

        # File was truncated — only session 2's single row remains.
        assert len(_data_rows(log_path)) == 1

    # ── Gating on feed_assist ────────────────────────────────────────────

    def test_idle_ticks_skipped_when_feed_assist_inactive(self, tmp_path):
        """No TSV rows and no log file at all while feed_assist stays off."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        manager.is_feed_assist_active.return_value = False

        for t in (0.25, 0.50, 0.75, 1.00, 1.25):
            monitor._log_tangle_telemetry(t, current_tool=0)

        # File should not even have been created — gate cuts before open.
        import os
        assert not os.path.exists(log_path)

    def test_off_to_on_transition_logged_and_resets_baseline(self, tmp_path):
        """The off→on transition logs a row AND resets the delta baseline,
        so the first active tick reports d=0 — no spurious gap across the
        idle period."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)

        # First a few idle ticks (no logging) — extruder/encoder advance
        # in the background but we don't see them.
        manager.is_feed_assist_active.return_value = False
        manager.get_rdm_encoder_pulse.return_value = 50
        monitor._get_extruder_pos = Mock(return_value=5.0)
        for t in (0.25, 0.50):
            monitor._log_tangle_telemetry(t, current_tool=0)

        # Now feed_assist comes on at tick 0.75 (with much higher values
        # than the last idle observation).
        manager.is_feed_assist_active.return_value = True
        manager.get_rdm_encoder_pulse.return_value = 200
        monitor._get_extruder_pos = Mock(return_value=120.0)
        monitor._log_tangle_telemetry(0.75, current_tool=0)

        rows = _data_rows(log_path)
        assert len(rows) == 1, f"expected only the transition row: {rows}"
        cols = rows[0].split("\t")
        # fa column == 1 (the transition target state)
        assert cols[7] == "1", f"fa column: {cols}"
        # Deltas reset to 0 even though absolute values jumped 50→200 / 5→120
        assert cols[4] == "0", f"d_encoder should be reset: {cols}"
        assert cols[5] == "0.000", f"d_extruder should be reset: {cols}"

    def test_on_to_off_transition_logged(self, tmp_path):
        """The on→off transition is logged with fa=0 so the boundary is
        visible in the TSV."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        # Two active ticks.
        for t in (0.25, 0.50):
            monitor._log_tangle_telemetry(t, current_tool=0)

        # Feed-assist drops at tick 0.75.
        manager.is_feed_assist_active.return_value = False
        monitor._log_tangle_telemetry(0.75, current_tool=0)

        # Subsequent idle ticks are silent again.
        for t in (1.00, 1.25):
            monitor._log_tangle_telemetry(t, current_tool=0)

        rows = _data_rows(log_path)
        assert len(rows) == 3  # 2 active + 1 off-transition
        # Last row is the on→off transition: fa=0
        cols = rows[-1].split("\t")
        assert cols[7] == "0", f"fa column on off-transition: {cols}"

    def test_active_ticks_logged_continuously(self, tmp_path):
        """While feed_assist stays True every tick should produce a row."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)

        for t in (0.25, 0.50, 0.75, 1.00, 1.25):
            monitor._log_tangle_telemetry(t, current_tool=0)

        assert len(_data_rows(log_path)) == 5


# ─────────────────────────────────────────────────────────────────────────
# Layer column — slicer-supplied current_layer in TSV
# ─────────────────────────────────────────────────────────────────────────


class TestLayerColumn:
    """The TSV's 12th column carries print_stats.info.current_layer so
    later analysis can correlate stalls with first-layer / specific
    layers.  Falls back to '-' when the slicer hasn't called
    SET_PRINT_STATS_INFO yet."""

    def _wire_print_stats(self, monitor, current_layer):
        """Make printer.lookup_object('print_stats') return a stats
        object whose info dict carries current_layer."""
        info = {"current_layer": current_layer}
        stats = Mock()
        stats.get_status = Mock(return_value={"info": info})
        monitor.printer.lookup_object = Mock(
            side_effect=lambda name, default=None:
                stats if name == "print_stats" else default
        )

    def test_layer_column_present_when_slicer_set_it(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        self._wire_print_stats(monitor, current_layer=42)

        monitor._log_tangle_telemetry(0.25, current_tool=0)

        rows = _data_rows(log_path)
        assert len(rows) == 1
        cols = rows[0].split("\t")
        assert cols[11] == "42", f"layer column: {cols}"

    def test_layer_column_dash_when_print_stats_missing(self, tmp_path):
        """No print_stats object at all → fall back to '-' (no crash)."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        monitor.printer.lookup_object = Mock(return_value=None)

        monitor._log_tangle_telemetry(0.25, current_tool=0)

        cols = _data_rows(log_path)[0].split("\t")
        assert cols[11] == "-", f"layer column: {cols}"

    def test_layer_column_dash_when_slicer_didnt_set_layer(self, tmp_path):
        """print_stats present but info.current_layer is None — the
        slicer just hasn't called SET_PRINT_STATS_INFO yet."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        self._wire_print_stats(monitor, current_layer=None)

        monitor._log_tangle_telemetry(0.25, current_tool=0)

        cols = _data_rows(log_path)[0].split("\t")
        assert cols[11] == "-", f"layer column: {cols}"

    def test_layer_column_in_header(self, tmp_path):
        """The TSV header must mention the new 'layer' column so later
        analysis tooling can parse it by name instead of position."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        monitor._log_tangle_telemetry(0.25, current_tool=0)

        with open(log_path) as f:
            header = f.read().split("\n")[1]  # second comment line = columns
        assert "layer" in header, f"layer not in column header: {header}"


# ─────────────────────────────────────────────────────────────────────────
# TANGLE_TELEMETRY_MARK — model-start anchor
# ─────────────────────────────────────────────────────────────────────────


def _make_gcmd(label=None):
    """Build a fake gcmd whose .get('LABEL', default) honours `label`."""
    gcmd = Mock()
    if label is None:
        gcmd.get.side_effect = lambda key, default=None: default
    else:
        def _get(key, default=None):
            if key == "LABEL":
                return label
            return default
        gcmd.get.side_effect = _get
    return gcmd


def _mark_comment_lines(path):
    """Read MARK comment lines from the telemetry log."""
    with open(path) as f:
        return [line.rstrip("\n") for line in f if line.startswith("# MARK ")]


class TestTelemetryMark:
    """The TANGLE_TELEMETRY_MARK gcode command snapshots the current
    encoder/extruder values and stamps a marker into the TSV.  Used in
    PRINT_START to flag the start of the actual model print (after
    purge/prime), so analysis can compute deltas from t0."""

    def test_mark_writes_comment_line(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=42.5)
        manager.get_rdm_encoder_pulse.return_value = 17
        monitor.reactor.monotonic = Mock(return_value=1234.5)

        # First flush the header / data row so the TSV file exists.
        monitor._log_tangle_telemetry(1.0, current_tool=0)

        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="model_start"))

        marks = _mark_comment_lines(log_path)
        assert len(marks) == 1
        assert "label=model_start" in marks[0]
        assert "extruder_pos=42.500" in marks[0]
        assert "encoder_pulse=17" in marks[0]
        assert "eventtime=1234.500" in marks[0]

    def test_mark_stores_anchor_state(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=300.0)
        manager.get_rdm_encoder_pulse.return_value = 250
        monitor.reactor.monotonic = Mock(return_value=99.0)

        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="model_start"))

        assert monitor._mark_label == "model_start"
        assert monitor._mark_extruder_pos == 300.0
        assert monitor._mark_encoder_pulse == 250
        assert monitor._mark_eventtime == 99.0

    def test_mark_default_label(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        manager.get_rdm_encoder_pulse.return_value = 5
        monitor.reactor.monotonic = Mock(return_value=1.0)

        # No LABEL argument — should default to "model_start".
        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label=None))

        assert monitor._mark_label == "model_start"

    def test_mark_overwrites_previous(self, tmp_path):
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        monitor._get_extruder_pos = Mock(return_value=10.0)
        manager.get_rdm_encoder_pulse.return_value = 5
        monitor.reactor.monotonic = Mock(side_effect=[1.0, 99.0])

        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="first"))
        # Second mark with different values.
        monitor._get_extruder_pos = Mock(return_value=500.0)
        manager.get_rdm_encoder_pulse.return_value = 400
        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="second"))

        # Anchor state reflects the second mark only.
        assert monitor._mark_label == "second"
        assert monitor._mark_extruder_pos == 500.0
        assert monitor._mark_encoder_pulse == 400
        assert monitor._mark_eventtime == 99.0
        # Both mark lines are present in the TSV.
        marks = _mark_comment_lines(log_path)
        assert len(marks) == 2
        assert "label=first" in marks[0]
        assert "label=second" in marks[1]

    def test_mark_without_open_log_does_not_crash(self, tmp_path):
        """When tangle_debug=False the TSV is never opened — the command
        must still snapshot anchor state without raising."""
        monitor, manager, log_path = _make_telemetry_monitor(
            tmp_path, tangle_debug=False
        )
        monitor._get_extruder_pos = Mock(return_value=10.0)
        manager.get_rdm_encoder_pulse.return_value = 5
        monitor.reactor.monotonic = Mock(return_value=1.0)

        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="x"))

        assert monitor._mark_extruder_pos == 10.0
        assert monitor._mark_encoder_pulse == 5
        # Telemetry file should not exist when tangle_debug=False.
        import os
        assert not os.path.exists(log_path)

    def test_mark_handles_unresolvable_extruder(self, tmp_path):
        """If the extruder isn't resolvable, the mark should still fire
        with extruder_pos=None and write 'n/a' into the TSV."""
        monitor, manager, log_path = _make_telemetry_monitor(tmp_path)
        # Force _resolve_extruder to fail.
        monitor._extruder = None
        monitor.printer.lookup_object.side_effect = Exception("boom")
        manager.get_rdm_encoder_pulse.return_value = 7
        monitor.reactor.monotonic = Mock(return_value=42.0)

        monitor._log_tangle_telemetry(1.0, current_tool=0)
        monitor.cmd_TANGLE_TELEMETRY_MARK(_make_gcmd(label="model_start"))

        assert monitor._mark_extruder_pos is None
        assert monitor._mark_encoder_pulse == 7
        marks = _mark_comment_lines(log_path)
        assert len(marks) == 1
        assert "extruder_pos=n/a" in marks[0]
        assert "encoder_pulse=7" in marks[0]

    def test_mark_registered_in_init(self):
        """The command must be registered during __init__ so PRINT_START
        can call it the very first time it runs."""
        printer = Mock()
        gcode = Mock()
        reactor = Mock()
        reactor.NOW = 0.0
        manager = Mock()
        manager.state = Mock()
        manager.state.get = Mock(return_value=-1)

        RunoutMonitor(
            printer, gcode, reactor, Mock(), manager,
            runout_debounce_count=1,
            tangle_detection=False,
            tangle_debug=False,
        )

        registered = [c.args[0] for c in gcode.register_command.call_args_list]
        assert "TANGLE_TELEMETRY_MARK" in registered
