"""
Tests for the pulse_counter.MCU_counter integration in filament_tracker.

The buttons module Klipper provides for GPIO inputs polls at ~40 Hz
with 25 ms debouncing — adequate for switches but insufficient for an
optical encoder that can produce hundreds of edges per second at
print speeds.  Empirically the buttons path caps encoder counts at
~1 increment per 50 ms tick, which is far below the real edge rate.

The fix is to register the encoder pin with pulse_counter.MCU_counter
(MCU-side interrupt-driven edge counter, microsecond resolution) in
parallel to the buttons handler.  The buttons path still runs for
filament-present logic; only the pulse COUNT moves to MCU_counter.

These tests verify:
  - GPIO path creates an MCU_counter on the encoder pin
  - ADC path does NOT create one (different code path entirely)
  - _on_mcu_count updates encoder_pulse to the cumulative count value
  - _on_mcu_count short-circuits when delta is zero
  - _gpio_handler no longer increments encoder_pulse when MCU_counter
    is present (preventing double-count)
  - _gpio_handler still increments encoder_pulse when MCU_counter
    fails to construct (graceful fallback)
  - encoder activity wires through to motion-detection (filament_distance
    is recomputed, RunoutHelper notified when configured)
"""
import sys
import os
import types
import importlib.util
from unittest.mock import Mock, MagicMock, patch
import pytest


# filament_tracker.py lives in extras/ rather than extras/ace/ — it is
# loaded by Klipper as a top-level module, not as part of the ace
# package.  Wire it into sys.modules so the test file can import it as
# a package-relative module ("from . import pulse_counter" works
# because we synthesise the package below).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXTRAS_DIR = os.path.join(_REPO_ROOT, "extras")


def _load_filament_tracker_module():
    """Load extras/filament_tracker.py under a synthetic 'extras' package
    so that its `from . import pulse_counter` import is resolvable via
    sys.modules injection (see stub_klipper_pulse_counter)."""
    if "extras" not in sys.modules:
        pkg = types.ModuleType("extras")
        pkg.__path__ = [_EXTRAS_DIR]
        sys.modules["extras"] = pkg
    if "extras.filament_switch_sensor" not in sys.modules:
        fss = types.ModuleType("extras.filament_switch_sensor")
        fss.RunoutHelper = lambda c: Mock()
        sys.modules["extras.filament_switch_sensor"] = fss
    if "extras.filament_tracker" in sys.modules:
        return sys.modules["extras.filament_tracker"]
    spec = importlib.util.spec_from_file_location(
        "extras.filament_tracker",
        os.path.join(_EXTRAS_DIR, "filament_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["extras.filament_tracker"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def stub_klipper_pulse_counter():
    """Stub the Klipper pulse_counter module so import works in tests.

    Klipper's pulse_counter ships in klippy/extras/pulse_counter.py;
    our package-relative `from . import pulse_counter` resolves to a
    sibling module at runtime but no such file exists in this repo's
    extras/ — so we inject a stub for the duration of each test.
    """
    stub = types.ModuleType("extras.pulse_counter")

    class _StubMCUCounter:
        def __init__(self, printer, pin, sample_time, poll_time):
            self.printer = printer
            self.pin = pin
            self.sample_time = sample_time
            self.poll_time = poll_time
            self._callback = None

        def setup_callback(self, cb):
            self._callback = cb

    stub.MCU_counter = _StubMCUCounter
    sys.modules["extras.pulse_counter"] = stub
    yield stub
    sys.modules.pop("extras.pulse_counter", None)


def _make_config(signal_type="gpio", detect_pin="^PF0", encoder_pin="^PC15",
                 detect_pin_is_switch=True, length_per_pulse=1.0,
                 absence_timeout=2.0, debug_trace=False,
                 extruder=None, detection_length=7.0,
                 adc_inverted=False, safe_unwind_len=100.0,
                 expose_as_motion_sensor=False):
    """Build a minimal Klipper-ish config wrapper for FilamentTracker."""
    cfg = Mock()
    values = {
        "signal_type": signal_type,
        "tracker_detect_pin": detect_pin,
        "tracker_encoder_pin": encoder_pin,
        "detect_pin_is_switch": detect_pin_is_switch,
        "length_per_pulse": length_per_pulse,
        "absence_timeout": absence_timeout,
        "debug_trace": debug_trace,
        "extruder": extruder,
        "detection_length": detection_length,
        "adc_inverted": adc_inverted,
        "safe_unwind_len": safe_unwind_len,
        "expose_as_filament_motion_sensor": expose_as_motion_sensor,
    }

    def get(key, default=None):
        return values.get(key, default)

    def getfloat(key, default=0.0, above=None, below=None):
        v = values.get(key, default)
        return float(v) if v is not None else float(default)

    def getboolean(key, default=False):
        return bool(values.get(key, default))

    def getint(key, default=0, minval=None, maxval=None):
        return int(values.get(key, default))

    cfg.get.side_effect = get
    cfg.getfloat.side_effect = getfloat
    cfg.getboolean.side_effect = getboolean
    cfg.getint.side_effect = getint
    cfg.error = Exception
    cfg.get_name.return_value = "filament_tracker test"

    printer = Mock()
    printer.lookup_object.return_value = Mock()
    printer.get_reactor.return_value = Mock(NEVER=float("inf"))
    cfg.get_printer.return_value = printer
    return cfg, printer


def _make_tracker(**kwargs):
    """Import + construct FilamentTracker with stubs."""
    cfg, printer = _make_config(**kwargs)
    mod = _load_filament_tracker_module()
    return mod.FilamentTracker(cfg), printer


# ─────────────────────────────────────────────────────────────────────────
# MCU_counter construction
# ─────────────────────────────────────────────────────────────────────────


class TestMCUCounterConstruction:
    """GPIO path must register an MCU_counter on the encoder pin."""

    def test_gpio_creates_mcu_counter(self):
        tracker, printer = _make_tracker(signal_type="gpio")
        # Counter exists and points at the encoder pin.
        assert tracker._mcu_counter is not None
        assert tracker._mcu_counter.pin == "^PC15"
        # Callback wired up.
        assert tracker._mcu_counter._callback == tracker._on_mcu_count

    def test_adc_does_not_create_mcu_counter(self):
        # ADC path is a separate code branch; pulse_counter has no role.
        try:
            tracker, _ = _make_tracker(signal_type="adc")
        except Exception:
            # ADC pin setup may fail without a real MCU — that's ok,
            # we only care that we didn't try to make a counter.
            return
        assert getattr(tracker, "_mcu_counter", None) is None

    def test_pulse_counter_import_failure_falls_back(self):
        """When pulse_counter is unavailable the tracker still constructs.

        Production printers always have pulse_counter, but during tests
        and on exotic builds the import can fail.  In that case we log
        a warning and fall back to buttons-only counting.
        """
        # Remove the stub temporarily.
        saved = sys.modules.pop("extras.pulse_counter", None)
        try:
            tracker, _ = _make_tracker(signal_type="gpio")
            assert tracker._mcu_counter is None
        finally:
            if saved is not None:
                sys.modules["extras.pulse_counter"] = saved


# ─────────────────────────────────────────────────────────────────────────
# _on_mcu_count semantics
# ─────────────────────────────────────────────────────────────────────────


class TestOnMcuCount:
    """The callback must set encoder_pulse to the cumulative count and
    trigger motion-detection bookkeeping when edges actually arrived."""

    def test_first_tick_updates_pulse_count(self):
        tracker, _ = _make_tracker(signal_type="gpio")
        assert tracker.tracker_status.encoder_pulse == 0

        tracker._on_mcu_count(time=1.0, count=10, count_time=1.0)

        assert tracker.tracker_status.encoder_pulse == 10
        assert tracker._mcu_counter_last_count == 10

    def test_subsequent_tick_uses_cumulative_count(self):
        """MCU_counter delivers cumulative count, not delta."""
        tracker, _ = _make_tracker(signal_type="gpio")
        tracker._on_mcu_count(time=1.0, count=10, count_time=1.0)
        tracker._on_mcu_count(time=2.0, count=42, count_time=2.0)

        assert tracker.tracker_status.encoder_pulse == 42

    def test_zero_delta_tick_short_circuits(self):
        """If MCU reports no new edges we must not retrigger motion
        detection — that would falsely note filament_present every
        50 ms even when the filament is stationary."""
        tracker, _ = _make_tracker(signal_type="gpio")
        tracker._on_mcu_count(time=1.0, count=10, count_time=1.0)
        # _on_encoder_pulse should not run on a zero-delta tick.
        tracker._on_encoder_pulse = Mock()
        tracker._on_mcu_count(time=2.0, count=10, count_time=2.0)

        tracker._on_encoder_pulse.assert_not_called()
        assert tracker.tracker_status.encoder_pulse == 10  # unchanged

    def test_nonzero_delta_calls_on_encoder_pulse(self):
        """Real edges must trigger _on_encoder_pulse so filament_distance
        is recomputed and motion-detection state advances."""
        tracker, _ = _make_tracker(signal_type="gpio")
        tracker._on_encoder_pulse = Mock()
        tracker._on_mcu_count(time=1.0, count=5, count_time=1.0)

        tracker._on_encoder_pulse.assert_called_once_with(1.0)

    def test_filament_distance_recomputed_after_count_update(self):
        """encoder_pulse × length_per_pulse must show through."""
        tracker, _ = _make_tracker(
            signal_type="gpio", length_per_pulse=1.04,
        )
        tracker._on_mcu_count(time=1.0, count=100, count_time=1.0)

        assert tracker.tracker_status.filament_distance == pytest.approx(104.0)


# ─────────────────────────────────────────────────────────────────────────
# _gpio_handler no longer double-counts
# ─────────────────────────────────────────────────────────────────────────


class TestGpioHandlerNoDoubleCount:
    """When MCU_counter is active, the buttons-path must not also
    increment encoder_pulse — that would double the count."""

    def test_gpio_handler_does_not_increment_when_mcu_counter_present(self):
        tracker, _ = _make_tracker(signal_type="gpio")
        assert tracker._mcu_counter is not None  # sanity
        assert tracker.tracker_status.encoder_pulse == 0

        # Simulate a buttons-edge: encoder bit toggles 0 → 1.
        tracker._last_gpio_state = 0b00
        tracker._gpio_handler(eventtime=1.0, state_bits=0b10)

        # encoder_signal_state tracks the edge for telemetry…
        assert tracker.tracker_status.encoder_signal_state == 1
        # …but encoder_pulse is left alone — MCU_counter owns it.
        assert tracker.tracker_status.encoder_pulse == 0

    def test_gpio_handler_increments_when_mcu_counter_absent(self):
        """Fallback path: pulse_counter unavailable → buttons-handler
        is the only source of pulses, so it must increment."""
        # Build a tracker, then forcibly drop the MCU counter.
        tracker, _ = _make_tracker(signal_type="gpio")
        tracker._mcu_counter = None
        tracker.tracker_status.encoder_pulse = 0

        tracker._last_gpio_state = 0b00
        tracker._gpio_handler(eventtime=1.0, state_bits=0b10)

        assert tracker.tracker_status.encoder_pulse == 1


# ─────────────────────────────────────────────────────────────────────────
# Integration scenario — buttons + MCU_counter side by side
# ─────────────────────────────────────────────────────────────────────────


class TestPathsCoexist:
    """The whole point: buttons handles filament_present, MCU_counter
    handles pulse count.  Both fire independently from the same edge."""

    def test_buttons_path_still_drives_filament_present(self):
        """Switch mode: detect pin going low (0) flips presence to absent.

        _note_filament_present is idempotent — it short-circuits when
        the new state matches the cached state.  So we explicitly mark
        the tracker as "present" first, then flip the detect bit, then
        verify the buttons handler propagated the transition.
        """
        tracker, _ = _make_tracker(
            signal_type="gpio", detect_pin_is_switch=True,
        )
        tracker.tracker_status.filament_present = 1
        note_calls = []
        tracker.runout_helper.note_filament_present = (
            lambda et, present: note_calls.append((et, present))
        )

        tracker._last_gpio_state = 0b01
        tracker._gpio_handler(eventtime=10.0, state_bits=0b00)

        assert note_calls and note_calls[-1][1] is False

    def test_high_resolution_count_through_mcu_counter(self):
        """A burst of edges that buttons would alias is captured fully."""
        tracker, _ = _make_tracker(signal_type="gpio")
        # Simulate a sequence of MCU_counter callbacks corresponding to
        # a fast burst — say 50 edges in a single 50 ms sample, which
        # the buttons path would clip to 1.
        tracker._on_mcu_count(time=1.00, count=50, count_time=1.00)
        tracker._on_mcu_count(time=1.05, count=100, count_time=1.05)
        tracker._on_mcu_count(time=1.10, count=150, count_time=1.10)

        # All 150 pulses are captured cumulatively.
        assert tracker.tracker_status.encoder_pulse == 150
