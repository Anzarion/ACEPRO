"""
Tests for the feed-assist hooks RunoutMonitor calls on AceManager.

RunoutMonitor reaches these through ``getattr(self.manager, name, None)``:

    _handle_runout_detected()      -> disable_feed_assist_for_tool()
    _verify_feed_assist_on_resume() -> verify_feed_assist_for_tool()

That guard means a missing method degrades SILENTLY - no exception, no log,
the feature simply never runs. This happened once already: the tangle rework
was imported from upstream without the two manager-side methods, leaving the
resume safety net dead for anyone who read the code and assumed otherwise.
TestHooksAreWired exists to make that failure loud.
"""

import pytest
from unittest.mock import MagicMock, patch

from extras.ace.manager import AceManager
from extras.ace.config import (
    FILAMENT_STATE_NOZZLE,
    FILAMENT_STATE_BOWDEN,
)


class TestHooksAreWired:
    """The names RunoutMonitor looks up must exist and be callable."""

    @pytest.mark.parametrize("hook", [
        "disable_feed_assist_for_tool",
        "verify_feed_assist_for_tool",
    ])
    def test_hook_exists_on_manager(self, hook):
        assert callable(getattr(AceManager, hook, None)), (
            f"RunoutMonitor calls manager.{hook}() through getattr - a "
            f"missing method fails silently, not loudly"
        )

    @pytest.mark.parametrize("hook", [
        "disable_feed_assist_for_tool",
        "verify_feed_assist_for_tool",
    ])
    def test_runout_monitor_still_looks_the_hook_up(self, hook):
        """If the caller side is renamed, this pairing must be revisited."""
        import inspect
        from extras.ace import runout_monitor
        src = inspect.getsource(runout_monitor)
        assert hook in src, (
            f"{hook} is no longer referenced by runout_monitor - either the "
            f"hook moved or it is now dead code on the manager"
        )


def _manager():
    manager = MagicMock(spec=AceManager)
    manager.gcode = MagicMock()
    manager.state = MagicMock()
    manager.toolchange_in_progress = False
    return manager


def _instance(assist_index=-1, current_assist=-1, connected=True):
    instance = MagicMock()
    instance.instance_num = 0
    instance._feed_assist_index = assist_index
    instance._get_current_feed_assist_index.return_value = current_assist
    instance.serial_mgr.is_connected.return_value = connected
    return instance


class TestDisableFeedAssistForTool:

    def test_disables_when_assist_is_on_that_slot(self):
        instance = _instance(assist_index=2)
        manager = _manager()

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 2)):
            AceManager.disable_feed_assist_for_tool(manager, 2, "runout")

        instance._disable_feed_assist.assert_called_once_with(2)

    def test_noop_when_assist_is_on_another_slot(self):
        """Never disable assist that belongs to a different tool."""
        instance = _instance(assist_index=0)
        manager = _manager()

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 2)):
            AceManager.disable_feed_assist_for_tool(manager, 2, "runout")

        instance._disable_feed_assist.assert_not_called()

    def test_never_raises(self):
        """Callers are mid-runout and must proceed regardless."""
        instance = _instance(assist_index=2)
        instance._disable_feed_assist.side_effect = Exception("comms")
        manager = _manager()

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 2)):
            AceManager.disable_feed_assist_for_tool(manager, 2, "runout")


class TestVerifyFeedAssistForTool:

    def _state(self, manager, pos=FILAMENT_STATE_NOZZLE, target=-1):
        values = {"ace_target_index": target, "ace_filament_pos": pos}
        manager.state.get.side_effect = lambda k, d=None: values.get(k, d)

    def test_reenables_when_assist_was_lost(self):
        """The actual resume bug: tool loaded, assist gone, print resumes."""
        instance = _instance(current_assist=-1)
        manager = _manager()
        self._state(manager)
        manager.get_switch_state.return_value = True

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is True
        instance._enable_feed_assist.assert_called_once_with(1)

    def test_noop_when_assist_already_active(self):
        instance = _instance(current_assist=1)
        manager = _manager()
        self._state(manager)
        manager.get_switch_state.return_value = True

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is True
        instance._enable_feed_assist.assert_not_called()

    def test_refuses_when_tool_is_not_loaded(self):
        """Arming assist on an unloaded tool pushes parked filament into
        the path - the guard must hold even though the tool is 'current'."""
        instance = _instance(current_assist=-1)
        manager = _manager()
        self._state(manager, pos=FILAMENT_STATE_BOWDEN)
        manager.get_switch_state.return_value = False

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is False
        instance._enable_feed_assist.assert_not_called()

    def test_stale_pos_is_overridden_by_the_sensor(self):
        """filament_pos can lag; a triggered toolhead sensor proves loaded."""
        instance = _instance(current_assist=-1)
        manager = _manager()
        self._state(manager, pos=FILAMENT_STATE_BOWDEN)
        manager.get_switch_state.return_value = True

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is True
        instance._enable_feed_assist.assert_called_once_with(1)

    def test_skipped_when_ace_not_connected(self):
        instance = _instance(current_assist=-1, connected=False)
        manager = _manager()
        self._state(manager)

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is False
        instance._enable_feed_assist.assert_not_called()

    def test_skipped_during_a_toolchange(self):
        instance = _instance(current_assist=-1)
        manager = _manager()
        manager.toolchange_in_progress = True
        self._state(manager)

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 1)):
            result = AceManager.verify_feed_assist_for_tool(manager, 1)

        assert result is False
        instance._enable_feed_assist.assert_not_called()
