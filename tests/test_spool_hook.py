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

        gcmd = MagicMock()
        gcmd.get_int.return_value = 1

        with patch("extras.ace.commands.ace_get_manager", return_value=mock_manager):
            from extras.ace.commands import cmd_ACE_HANDLE_PRINT_END
            cmd_ACE_HANDLE_PRINT_END(gcmd)

        mock_manager.clear_active_spool_if_configured.assert_not_called()


class TestEndlessSpoolFeedAssistDisable:
    """Tests for feed assist disable before endless spool swap.

    When the toolhead sensor detects runout and endless spool triggers,
    feed assist on the old (empty) slot must be stopped before the swap.
    Otherwise ACE2 stays 'busy' and the new slot's feed command deadlocks
    on wait_ready().
    """

    @pytest.fixture
    def mock_endless_spool(self):
        """Create a mock EndlessSpool with wired-up manager/instances."""
        from extras.ace.endless_spool import EndlessSpool

        manager = MagicMock()
        manager.gcode = MagicMock()
        manager.perform_tool_change.return_value = "OK"

        # Create a mock ACE instance for instance 0 (slots 0-3)
        ace_inst = MagicMock()
        ace_inst.inventory = [
            {"status": "empty", "material": "PLA", "color": [0, 0, 0]},
            {"status": "ready", "material": "PLA", "color": [0, 0, 0]},
            {"status": "ready", "material": "ABS", "color": [255, 0, 0]},
            {"status": "ready", "material": "PLA", "color": [0, 0, 0]},
        ]
        manager.instances = {0: ace_inst}

        printer = MagicMock()
        gcode = manager.gcode

        es = EndlessSpool(printer, gcode, manager)

        return es, manager, ace_inst

    def test_feed_assist_disabled_before_swap(self, mock_endless_spool):
        """Feed assist on empty slot must be disabled before perform_tool_change."""
        es, manager, ace_inst = mock_endless_spool

        # Feed assist is active on slot 0 (the empty one)
        ace_inst._get_current_feed_assist_index.return_value = 0

        # Track call order
        call_order = []
        ace_inst._disable_feed_assist.side_effect = (
            lambda slot: call_order.append(("disable_fa", slot))
        )
        manager.perform_tool_change.side_effect = (
            lambda f, t, **kw: call_order.append(("tool_change", f, t)) or "OK"
        )

        with patch("extras.ace.endless_spool.get_instance_from_tool", return_value=0), \
             patch("extras.ace.endless_spool.get_local_slot", return_value=0):
            es.execute_swap(0, 1)

        # Feed assist must be disabled BEFORE tool change
        assert call_order[0] == ("disable_fa", 0), (
            f"Expected disable_feed_assist first, got: {call_order}"
        )
        assert call_order[1] == ("tool_change", 0, 1), (
            f"Expected perform_tool_change second, got: {call_order}"
        )

    def test_feed_assist_not_disabled_when_inactive(self, mock_endless_spool):
        """If feed assist is not active, _disable_feed_assist should not be called."""
        es, manager, ace_inst = mock_endless_spool

        # Feed assist is NOT active (-1)
        ace_inst._get_current_feed_assist_index.return_value = -1

        with patch("extras.ace.endless_spool.get_instance_from_tool", return_value=0), \
             patch("extras.ace.endless_spool.get_local_slot", return_value=0):
            es.execute_swap(0, 1)

        ace_inst._disable_feed_assist.assert_not_called()

    def test_feed_assist_not_disabled_when_active_on_different_slot(self, mock_endless_spool):
        """If feed assist is active on a different slot, don't touch it."""
        es, manager, ace_inst = mock_endless_spool

        # Feed assist is active on slot 2, but runout is on slot 0
        ace_inst._get_current_feed_assist_index.return_value = 2

        with patch("extras.ace.endless_spool.get_instance_from_tool", return_value=0), \
             patch("extras.ace.endless_spool.get_local_slot", return_value=0):
            es.execute_swap(0, 1)

        ace_inst._disable_feed_assist.assert_not_called()


class TestTangleVsEmptySpool:
    """Tests for distinguishing tangle from empty spool in _check_tangle().

    Physical scenario:
    - ACE is ~2.4m from toolhead, connected via bowden tube
    - Feed assist motor is in the ACE, pushing filament into the bowden
    - Entry sensor per slot detects filament presence at the ACE

    Three scenarios when cont_assist_time exceeds threshold:

    1. TANGLE: Slot entry sensor has filament, but feed assist can't deliver
       → filament is stuck → PAUSE + "Tangle Detected" prompt

    2. EMPTY SPOOL: Slot entry sensor reports empty, filament left the gears
       → spool is used up → disable feed assist, keep printing,
         extruder pulls remaining ~2.4m from bowden alone,
         toolhead sensor triggers normal runout later

    3. FILAMENT BREAK: Slot entry sensor has filament (spool still loaded),
       feed assist can't deliver (broken filament somewhere in path)
       → same as tangle → PAUSE (user must intervene)
    """

    @pytest.fixture
    def monitor(self):
        """Create a RunoutMonitor with a mock Gen 1 ACE instance."""
        from extras.ace.runout_monitor import RunoutMonitor

        printer = MagicMock()
        gcode = MagicMock()
        reactor = MagicMock()
        reactor.monotonic.return_value = 100.0
        endless_spool = MagicMock()
        manager = MagicMock()
        manager.toolchange_in_progress = False

        # Gen 1 ACE instance with feed assist active on slot 2
        ace_inst = MagicMock()
        ace_inst._feed_assist_index = 2
        ace_inst.protocol_name = "ace1_json"
        ace_inst.instance_num = 0

        manager.instances = [ace_inst]

        mon = RunoutMonitor(
            printer, gcode, reactor, endless_spool, manager,
            tangle_detection=True, tangle_pump_time=4.0,
        )

        return mon, ace_inst

    def _trigger_tangle_threshold(self, mon, eventtime=100.0):
        """Advance _check_tangle through the phase-start → threshold sequence.

        Requires two calls: first sets phase_start, second crosses threshold.
        """
        # First call: cont_assist_time starts growing → phase_start set
        mon._check_tangle(eventtime, current_tool=2)
        # Second call: still above threshold → should trigger
        mon._check_tangle(eventtime + 1.0, current_tool=2)

    def test_tangle_pause_when_slot_has_filament(self, monitor):
        """Slot has filament + cont_assist_time high → real tangle → pause."""
        mon, ace_inst = monitor

        # Slot 2 has filament (NOT empty)
        ace_inst._is_slot_empty.return_value = False

        # cont_assist_time above threshold (4.0s)
        ace_inst._info = {"cont_assist_time": 5.0}

        self._trigger_tangle_threshold(mon)

        # Must call _handle_tangle_detected (which pauses)
        assert mon.runout_handling_in_progress or \
            mon.gcode.run_script_from_command.call_count > 0, \
            "Tangle should trigger pause when slot has filament"
        # Feed assist must NOT be disabled (filament is stuck, not empty)
        ace_inst._disable_feed_assist.assert_not_called()

    def test_feed_assist_disabled_when_slot_empty(self, monitor):
        """Slot empty + cont_assist_time high → empty spool → disable feed assist, no pause."""
        mon, ace_inst = monitor

        # Slot 2 is empty (spool used up, filament left the gears)
        ace_inst._is_slot_empty.return_value = True

        ace_inst._info = {"cont_assist_time": 5.0}

        self._trigger_tangle_threshold(mon)

        # Feed assist must be disabled
        ace_inst._disable_feed_assist.assert_called_once_with(2)
        # Must NOT pause (extruder pulls remaining filament from bowden)
        assert not mon.runout_handling_in_progress, \
            "Empty spool should not trigger pause — toolhead sensor handles runout later"

    def test_filament_break_treated_as_tangle(self, monitor):
        """Filament break: slot has filament but can't deliver → same as tangle."""
        mon, ace_inst = monitor

        # Slot 2 has filament (spool still loaded, but filament broke)
        ace_inst._is_slot_empty.return_value = False

        ace_inst._info = {"cont_assist_time": 5.0}

        self._trigger_tangle_threshold(mon)

        # Must trigger tangle (pause), NOT disable feed assist
        ace_inst._disable_feed_assist.assert_not_called()

    def test_phase_resets_after_empty_spool_disable(self, monitor):
        """After disabling feed assist for empty spool, phase tracking must reset."""
        mon, ace_inst = monitor

        ace_inst._is_slot_empty.return_value = True
        ace_inst._info = {"cont_assist_time": 5.0}

        self._trigger_tangle_threshold(mon)

        # Phase tracking must be reset so it doesn't re-trigger
        assert mon._pt_phase_start_eventtime is None
        assert mon._pt_last_value_s == 0.0

    def test_no_action_below_threshold(self, monitor):
        """cont_assist_time below threshold → no action regardless of slot state."""
        mon, ace_inst = monitor

        ace_inst._is_slot_empty.return_value = True
        ace_inst._info = {"cont_assist_time": 2.0}  # below 4.0 threshold

        # First tick: phase start
        mon._check_tangle(100.0, current_tool=2)
        # Second tick: still below threshold
        ace_inst._info = {"cont_assist_time": 3.0}
        mon._check_tangle(101.0, current_tool=2)

        ace_inst._disable_feed_assist.assert_not_called()
