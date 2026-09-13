"""
Tests for the spoolman/filaman spool tracking hook in register_tool_macros.

These tests verify that:
1. Dynamic T-macros call _SET_SPOOL_BY_TOOL after ACE_CHANGE_TOOL
   when spoolman_logic.cfg is included (macro exists).
2. The hook is skipped when _SET_SPOOL_BY_TOOL is not defined.
3. The correct TOOL= parameter is passed to _SET_SPOOL_BY_TOOL.
4. User-defined gcode_macro T<n> takes precedence (ACE skips registration).
5. Config-defined gcode_macro T<n> takes precedence (has_section check).
6. The hook is not called when cmd_ACE_CHANGE_TOOL raises an exception.
7. T-macros are created for the correct number of slots per instance.
"""

import pytest
from unittest.mock import MagicMock, patch, call


# Constants matching manager.py
SLOTS_PER_ACE = 4


def get_tool_offset(instance_num):
    """Mirror the real get_tool_offset function."""
    return instance_num * SLOTS_PER_ACE


class TestSpoolHookInjection:
    """Tests for _SET_SPOOL_BY_TOOL hook in dynamically generated T-macros."""

    @pytest.fixture
    def mock_manager(self):
        """Create a mock AceManager with required attributes."""
        manager = MagicMock()
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.config = MagicMock()

        # Default: no user-defined macros, no config sections
        manager.printer.lookup_object.return_value = None
        manager.config.has_section.return_value = False

        return manager

    def _register_and_get_macros(self, manager, instance_num=0):
        """
        Simulate register_tool_macros and return the registered T-macro
        callables as a dict {name: handler}.
        """
        from extras.ace import commands

        registered = {}

        def capture_register(name, handler, desc=None):
            registered[name] = handler

        manager.gcode.register_command.side_effect = capture_register

        # Import and call the real method, bound to our mock
        from extras.ace.manager import AceManager
        AceManager.register_tool_macros(manager, instance_num)

        return registered

    def test_spool_hook_called_when_macro_exists(self, mock_manager):
        """When _SET_SPOOL_BY_TOOL exists, it should be called after toolchange."""
        spool_macro_sentinel = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro _SET_SPOOL_BY_TOOL":
                return spool_macro_sentinel
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        macros = self._register_and_get_macros(mock_manager)
        assert "T0" in macros

        gcmd = MagicMock()

        with patch("extras.ace.commands.cmd_ACE_CHANGE_TOOL") as mock_change:
            macros["T0"](gcmd)

            # cmd_ACE_CHANGE_TOOL should be called with manager, gcmd, tool_idx=0
            mock_change.assert_called_once_with(mock_manager, gcmd, 0)

            # _SET_SPOOL_BY_TOOL should be dispatched
            mock_manager.gcode.run_script_from_command.assert_called_once_with(
                "_SET_SPOOL_BY_TOOL TOOL=0"
            )

    def test_spool_hook_skipped_when_macro_missing(self, mock_manager):
        """When _SET_SPOOL_BY_TOOL is not defined, hook should be skipped."""
        # lookup_object returns None for everything (default)
        macros = self._register_and_get_macros(mock_manager)

        gcmd = MagicMock()

        with patch("extras.ace.commands.cmd_ACE_CHANGE_TOOL"):
            macros["T0"](gcmd)

            # run_script_from_command should NOT be called
            mock_manager.gcode.run_script_from_command.assert_not_called()

    def test_spool_hook_correct_tool_index(self, mock_manager):
        """Each T-macro should pass the correct TOOL= to _SET_SPOOL_BY_TOOL."""
        spool_macro_sentinel = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro _SET_SPOOL_BY_TOOL":
                return spool_macro_sentinel
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        macros = self._register_and_get_macros(mock_manager)
        gcmd = MagicMock()

        with patch("extras.ace.commands.cmd_ACE_CHANGE_TOOL"):
            for tool_idx in range(4):
                mock_manager.gcode.run_script_from_command.reset_mock()
                macros[f"T{tool_idx}"](gcmd)

                mock_manager.gcode.run_script_from_command.assert_called_once_with(
                    f"_SET_SPOOL_BY_TOOL TOOL={tool_idx}"
                )

    def test_spool_hook_correct_tool_index_instance_1(self, mock_manager):
        """Instance 1 should register T4-T7 with correct tool indices."""
        spool_macro_sentinel = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro _SET_SPOOL_BY_TOOL":
                return spool_macro_sentinel
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        macros = self._register_and_get_macros(mock_manager, instance_num=1)
        gcmd = MagicMock()

        assert set(macros.keys()) == {"T4", "T5", "T6", "T7"}

        with patch("extras.ace.commands.cmd_ACE_CHANGE_TOOL"):
            for tool_idx in range(4, 8):
                mock_manager.gcode.run_script_from_command.reset_mock()
                macros[f"T{tool_idx}"](gcmd)

                mock_manager.gcode.run_script_from_command.assert_called_once_with(
                    f"_SET_SPOOL_BY_TOOL TOOL={tool_idx}"
                )

    def test_spool_hook_not_called_on_toolchange_error(self, mock_manager):
        """If cmd_ACE_CHANGE_TOOL raises, _SET_SPOOL_BY_TOOL must not be called."""
        spool_macro_sentinel = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro _SET_SPOOL_BY_TOOL":
                return spool_macro_sentinel
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        macros = self._register_and_get_macros(mock_manager)
        gcmd = MagicMock()

        with patch("extras.ace.commands.cmd_ACE_CHANGE_TOOL") as mock_change:
            mock_change.side_effect = Exception("Toolchange failed")

            with pytest.raises(Exception, match="Toolchange failed"):
                macros["T0"](gcmd)

            # Hook must NOT have been called
            mock_manager.gcode.run_script_from_command.assert_not_called()


class TestTMacroRegistrationPrecedence:
    """Tests for T-macro registration precedence (user macros vs. ACE dynamic)."""

    @pytest.fixture
    def mock_manager(self):
        """Create a mock AceManager."""
        manager = MagicMock()
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.config = MagicMock()
        manager.printer.lookup_object.return_value = None
        manager.config.has_section.return_value = False
        return manager

    def _register_and_get_macros(self, manager, instance_num=0):
        """Register and capture T-macros."""
        registered = {}

        def capture_register(name, handler, desc=None):
            registered[name] = handler

        manager.gcode.register_command.side_effect = capture_register

        from extras.ace.manager import AceManager
        AceManager.register_tool_macros(manager, instance_num)
        return registered

    def test_skips_when_runtime_macro_exists(self, mock_manager):
        """If gcode_macro T0 is already loaded at runtime, skip registration."""
        existing_macro = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro T0":
                return existing_macro
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        macros = self._register_and_get_macros(mock_manager)

        # T0 should NOT be registered (user's macro takes precedence)
        assert "T0" not in macros
        # T1, T2, T3 should still be registered
        assert "T1" in macros
        assert "T2" in macros
        assert "T3" in macros

    def test_skips_when_config_section_exists(self, mock_manager):
        """If gcode_macro T2 is defined in config but not yet loaded, skip."""
        def has_section_side_effect(name):
            return name == "gcode_macro T2"

        mock_manager.config.has_section.side_effect = has_section_side_effect

        macros = self._register_and_get_macros(mock_manager)

        # T2 should NOT be registered
        assert "T2" not in macros
        # Others should be registered
        assert "T0" in macros
        assert "T1" in macros
        assert "T3" in macros

    def test_registers_correct_count_instance_0(self, mock_manager):
        """Instance 0 should register T0-T3 (4 macros)."""
        macros = self._register_and_get_macros(mock_manager, instance_num=0)
        assert set(macros.keys()) == {"T0", "T1", "T2", "T3"}

    def test_registers_correct_count_instance_1(self, mock_manager):
        """Instance 1 should register T4-T7 (4 macros)."""
        macros = self._register_and_get_macros(mock_manager, instance_num=1)
        assert set(macros.keys()) == {"T4", "T5", "T6", "T7"}


class TestClearActiveSpoolHook:
    """Tests for CLEAR_ACTIVE_SPOOL hook after unload operations."""

    @pytest.fixture
    def mock_manager(self):
        """Create a mock AceManager with required attributes."""
        manager = MagicMock()
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.state = MagicMock()

        # Default: CLEAR_ACTIVE_SPOOL macro does not exist
        manager.printer.lookup_object.return_value = None

        return manager

    def test_clear_called_when_macro_exists(self, mock_manager):
        """CLEAR_ACTIVE_SPOOL should be called when the macro is defined."""
        clear_macro_sentinel = MagicMock()

        def lookup_side_effect(name, default=None):
            if name == "gcode_macro CLEAR_ACTIVE_SPOOL":
                return clear_macro_sentinel
            return default

        mock_manager.printer.lookup_object.side_effect = lookup_side_effect

        from extras.ace.manager import AceManager
        AceManager.clear_active_spool_if_configured(mock_manager)

        mock_manager.gcode.run_script_from_command.assert_called_once_with(
            "CLEAR_ACTIVE_SPOOL"
        )

    def test_clear_skipped_when_macro_missing(self, mock_manager):
        """CLEAR_ACTIVE_SPOOL should not be called when macro is not defined."""
        from extras.ace.manager import AceManager
        AceManager.clear_active_spool_if_configured(mock_manager)

        mock_manager.gcode.run_script_from_command.assert_not_called()

    def test_smart_unload_calls_clear(self, mock_manager):
        """cmd_ACE_SMART_UNLOAD should call clear_active_spool_if_configured on success."""
        mock_manager.get_ace_global_enabled.return_value = True
        mock_manager.state.get.return_value = 0  # current tool index
        mock_manager.smart_unload.return_value = True

        gcmd = MagicMock()
        gcmd.get_int.return_value = -1  # no TOOL= param → use current

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_SMART_UNLOAD
            cmd_ACE_SMART_UNLOAD(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_called_once()

    def test_smart_unload_no_clear_on_failure(self, mock_manager):
        """cmd_ACE_SMART_UNLOAD should NOT call clear on failure."""
        mock_manager.get_ace_global_enabled.return_value = True
        mock_manager.state.get.return_value = 0
        mock_manager.smart_unload.return_value = False

        gcmd = MagicMock()
        gcmd.get_int.return_value = -1

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_SMART_UNLOAD
            cmd_ACE_SMART_UNLOAD(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_not_called()

    def test_full_unload_single_calls_clear(self, mock_manager):
        """cmd_ACE_FULL_UNLOAD (single tool) should call clear on success."""
        mock_manager.full_unload_slot.return_value = True
        mock_manager.state.get.return_value = 0

        gcmd = MagicMock()
        gcmd.get.return_value = None  # not TOOL=ALL
        gcmd.get_int.return_value = 0  # TOOL=0

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_FULL_UNLOAD
            cmd_ACE_FULL_UNLOAD(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_called_once()

    def test_full_unload_single_no_clear_on_failure(self, mock_manager):
        """cmd_ACE_FULL_UNLOAD (single tool) should NOT call clear on failure."""
        mock_manager.full_unload_slot.return_value = False
        mock_manager.state.get.return_value = 0

        gcmd = MagicMock()
        gcmd.get.return_value = None
        gcmd.get_int.return_value = 0

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_FULL_UNLOAD
            cmd_ACE_FULL_UNLOAD(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_not_called()

    def test_print_end_calls_clear(self, mock_manager):
        """cmd_ACE_HANDLE_PRINT_END should call clear after successful unload."""
        mock_manager.get_ace_global_enabled.return_value = True
        mock_manager.state.get.return_value = 0  # current tool
        mock_manager.smart_unload.return_value = True
        # Spool did not deplete at the ACE, so the normal unload path applies
        # (a bare Mock attribute would be truthy and select flush-forward).
        mock_manager.runout_monitor._empty_spool_detected = False

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1  # CUT_TIP=1

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager), \
             patch("extras.ace.commands.for_each_instance"):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_called_once()

    def test_print_end_no_clear_on_failure(self, mock_manager):
        """cmd_ACE_HANDLE_PRINT_END should NOT call clear if unload fails."""
        mock_manager.get_ace_global_enabled.return_value = True
        mock_manager.state.get.return_value = 0
        mock_manager.smart_unload.return_value = False
        mock_manager.runout_monitor._empty_spool_detected = False

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_not_called()


class TestFlushForwardAtPrintEnd:
    """Tests for flush-forward logic when spool depletes near print end.

    Edge case: spool empties during printing, _check_tangle() detects it
    and sets _empty_spool_detected = True, but the print finishes (or is
    cancelled) before the toolhead sensor triggers normal runout.  The
    filament is orphaned in the bowden — the ACE has nothing to grip.

    Expected behavior:
    - cmd_ACE_HANDLE_PRINT_END detects the flag and calls
      flush_forward_until_clear() instead of smart_unload().
    - flush_forward_until_clear() extrudes forward in chunks until the
      toolhead sensor clears.
    - The flag is reset after handling.
    """

    @pytest.fixture
    def mock_manager_for_flush(self):
        """Create a mock AceManager with runout_monitor._empty_spool_detected."""
        manager = MagicMock()
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.state = MagicMock()
        manager.get_ace_global_enabled.return_value = True
        manager.state.get.return_value = 3  # current tool T3

        # Runout monitor with _empty_spool_detected flag
        manager.runout_monitor = MagicMock()
        manager.runout_monitor._empty_spool_detected = False
        manager.runout_monitor.runout_detection_active = True

        return manager

    def test_flush_forward_called_when_spool_depleted(self, mock_manager_for_flush):
        """When _empty_spool_detected is True, flush_forward should be called
        instead of smart_unload."""
        manager = mock_manager_for_flush
        manager.runout_monitor._empty_spool_detected = True
        manager.flush_forward_until_clear.return_value = True

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1  # CUT_TIP=1

        with patch("extras.ace.commands.ace_get_manager", return_value=manager), \
             patch("extras.ace.commands.for_each_instance"):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        manager.flush_forward_until_clear.assert_called_once_with(3)
        manager.smart_unload.assert_not_called()

    def test_smart_unload_called_when_no_depletion(self, mock_manager_for_flush):
        """When _empty_spool_detected is False, normal smart_unload is used."""
        manager = mock_manager_for_flush
        manager.runout_monitor._empty_spool_detected = False
        manager.smart_unload.return_value = True

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=manager), \
             patch("extras.ace.commands.for_each_instance"):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        manager.smart_unload.assert_called_once_with(3, prepare_toolhead=True)
        manager.flush_forward_until_clear.assert_not_called()

    def test_flag_reset_after_flush_forward(self, mock_manager_for_flush):
        """The _empty_spool_detected flag must be reset after flush forward."""
        manager = mock_manager_for_flush
        manager.runout_monitor._empty_spool_detected = True
        manager.flush_forward_until_clear.return_value = True

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=manager), \
             patch("extras.ace.commands.for_each_instance"):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        assert manager.runout_monitor._empty_spool_detected is False

    def test_clear_spool_called_after_flush_success(self, mock_manager_for_flush):
        """After successful flush forward, clear_active_spool must be called."""
        manager = mock_manager_for_flush
        manager.runout_monitor._empty_spool_detected = True
        manager.flush_forward_until_clear.return_value = True

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=manager), \
             patch("extras.ace.commands.for_each_instance"):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        manager.clear_active_spool_if_configured.assert_called_once()

    def test_no_clear_spool_after_flush_failure(self, mock_manager_for_flush):
        """If flush forward fails, clear_active_spool must NOT be called."""
        manager = mock_manager_for_flush
        manager.runout_monitor._empty_spool_detected = True
        manager.flush_forward_until_clear.return_value = False

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=manager):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        manager.clear_active_spool_if_configured.assert_not_called()


class TestFlushForwardMethod:
    """Unit tests for AceManager.flush_forward_until_clear().

    Tests the flush loop: extrude in chunks, check sensor, stop when clear.
    """

    @pytest.fixture
    def mock_manager_flush(self):
        """Create a mock manager wired for flush_forward_until_clear."""
        from extras.ace.manager import AceManager

        manager = MagicMock(spec=AceManager)
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.reactor = MagicMock()
        manager.reactor.monotonic.return_value = 100.0
        manager.state = MagicMock()

        # Extruder/heater mock — already hot
        heater = MagicMock()
        heater.get_temp.return_value = (220.0, 220.0)
        heater.min_extrude_temp = 170.0
        extruder = MagicMock()
        extruder.get_heater.return_value = heater
        manager.printer.lookup_object.return_value = extruder

        # Sensor: toolhead starts triggered, clears after some extrusion
        manager.get_switch_state = MagicMock(return_value=True)

        # _extruder_move is the workhorse — no-op in test
        manager._extruder_move = MagicMock()

        # _turn_off_heater_if_idle — no-op
        manager._turn_off_heater_if_idle = MagicMock()

        return manager

    def test_flush_stops_when_sensor_clears(self, mock_manager_flush):
        """Flush should stop as soon as toolhead sensor reports absent."""
        manager = mock_manager_flush

        # Sensor clears after first chunk
        manager.get_switch_state.side_effect = [True, False]

        from extras.ace.manager import AceManager
        result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is True
        # One chunk extruded (50mm), then sensor cleared
        manager._extruder_move.assert_called_once()
        args = manager._extruder_move.call_args
        assert args[0][0] == 50.0  # chunk size

    def test_flush_multiple_chunks(self, mock_manager_flush):
        """Flush should extrude multiple chunks until sensor clears."""
        manager = mock_manager_flush

        # Sensor stays triggered for 3 chunks, clears on 4th check
        manager.get_switch_state.side_effect = [True, True, True, False]

        from extras.ace.manager import AceManager
        result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is True
        assert manager._extruder_move.call_count == 3

    def test_flush_fails_at_max_distance(self, mock_manager_flush):
        """If sensor never clears within MAX_FLUSH_MM, return False."""
        manager = mock_manager_flush

        # Sensor never clears
        manager.get_switch_state.return_value = True

        from extras.ace.manager import AceManager
        result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is False

    def test_flush_immediate_clear(self, mock_manager_flush):
        """If sensor is already clear at start, no extrusion needed."""
        manager = mock_manager_flush

        # Sensor already clear
        manager.get_switch_state.return_value = False

        from extras.ace.manager import AceManager
        result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is True
        manager._extruder_move.assert_not_called()

    def test_flush_heats_when_cold(self, mock_manager_flush):
        """When nozzle is below min_extrude_temp, M109 must be called."""
        manager = mock_manager_flush

        # Nozzle is cold
        heater = manager.printer.lookup_object.return_value.get_heater.return_value
        heater.get_temp.return_value = (30.0, 30.0)
        heater.min_extrude_temp = 170.0

        # Sensor clears immediately
        manager.get_switch_state.return_value = False

        from extras.ace.manager import AceManager

        # Mock get_ace_instance_and_slot_for_tool for temp lookup
        ace_inst = MagicMock()
        ace_inst.inventory = [
            {}, {}, {}, {"temp": 240}
        ]
        with patch(
            "extras.ace.manager.get_ace_instance_and_slot_for_tool",
            return_value=(ace_inst, 3)
        ):
            result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is True
        # Check that M109 was called with the inventory temp
        m109_calls = [
            c for c in manager.gcode.run_script_from_command.call_args_list
            if "M109" in str(c)
        ]
        assert len(m109_calls) == 1
        assert "240" in str(m109_calls[0])

    def test_flush_cleans_up_gcode_state(self, mock_manager_flush):
        """G92 E0 and G90 must be called in finally block."""
        manager = mock_manager_flush
        manager.get_switch_state.return_value = False  # immediate clear

        from extras.ace.manager import AceManager
        AceManager.flush_forward_until_clear(manager, tool_index=3)

        gcode_calls = [str(c) for c in manager.gcode.run_script_from_command.call_args_list]
        assert any("G92 E0" in c for c in gcode_calls)
        assert any("G90" in c for c in gcode_calls)

    def test_flush_holds_target_when_hot_but_heater_off(self, mock_manager_flush):
        """Regression: nozzle still hot but target already 0 (PRINT_END did
        M104 S0). Checking the temperature once would skip heating and the
        nozzle would coast below min_extrude_temp mid-flush, aborting the
        extruder move with filament in the melt zone. A target must be set."""
        manager = mock_manager_flush

        heater = manager.printer.lookup_object.return_value.get_heater.return_value
        heater.get_temp.return_value = (200.0, 0.0)  # hot now, but cooling
        heater.min_extrude_temp = 170.0

        manager.get_switch_state.return_value = False  # clears immediately

        from extras.ace.manager import AceManager
        ace_inst = MagicMock()
        ace_inst.inventory = [{}, {}, {}, {"temp": 215}]
        with patch(
            "extras.ace.manager.get_ace_instance_and_slot_for_tool",
            return_value=(ace_inst, 3)
        ):
            result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is True
        m109_calls = [
            c for c in manager.gcode.run_script_from_command.call_args_list
            if "M109" in str(c)
        ]
        assert len(m109_calls) == 1, "flush must hold a target for its duration"
        assert "215" in str(m109_calls[0])

    def test_flush_does_not_lower_an_active_print_target(self, mock_manager_flush):
        """An still-active, higher print target must not be cooled down to the
        inventory temperature just because the flush prefers that value."""
        manager = mock_manager_flush

        heater = manager.printer.lookup_object.return_value.get_heater.return_value
        heater.get_temp.return_value = (245.0, 245.0)  # ABS print still hot
        heater.min_extrude_temp = 170.0

        manager.get_switch_state.return_value = False

        from extras.ace.manager import AceManager
        ace_inst = MagicMock()
        ace_inst.inventory = [{}, {}, {}, {"temp": 205}]
        with patch(
            "extras.ace.manager.get_ace_instance_and_slot_for_tool",
            return_value=(ace_inst, 3)
        ):
            AceManager.flush_forward_until_clear(manager, tool_index=3)

        m109_calls = [
            str(c) for c in manager.gcode.run_script_from_command.call_args_list
            if "M109" in str(c)
        ]
        assert len(m109_calls) == 1
        assert "245" in m109_calls[0], "must not cool an active print target"

    def test_flush_turns_off_heater_on_failure(self, mock_manager_flush):
        """Regression: a failed flush used to return early, skipping heater
        shutdown and leaving a hot nozzle parked over the bucket after the
        print had already finished."""
        manager = mock_manager_flush
        manager.get_switch_state.return_value = True  # never clears

        from extras.ace.manager import AceManager
        result = AceManager.flush_forward_until_clear(manager, tool_index=3)

        assert result is False
        manager._turn_off_heater_if_idle.assert_called_once()

    def test_flush_no_nozzle_clean_on_failure(self, mock_manager_flush):
        """Wiping makes no sense while filament is still being pushed out;
        only the success path should run NOZZLE_CLEAN."""
        manager = mock_manager_flush
        manager.get_switch_state.return_value = True  # never clears

        from extras.ace.manager import AceManager
        AceManager.flush_forward_until_clear(manager, tool_index=3)

        gcode_calls = [str(c) for c in manager.gcode.run_script_from_command.call_args_list]
        assert not any("NOZZLE_CLEAN" in c for c in gcode_calls)
        manager.state.set.assert_not_called()

    def test_flush_turns_off_heater_when_move_raises(self, mock_manager_flush):
        """An aborted flush (e.g. cold-extrude abort) must not leave the
        heater running either."""
        manager = mock_manager_flush
        manager.get_switch_state.return_value = True
        manager._extruder_move.side_effect = Exception("Extrude below minimum temp")

        from extras.ace.manager import AceManager
        with pytest.raises(Exception, match="minimum temp"):
            AceManager.flush_forward_until_clear(manager, tool_index=3)

        manager._turn_off_heater_if_idle.assert_called_once()
