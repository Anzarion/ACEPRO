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
