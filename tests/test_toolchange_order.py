"""Reihenfolge, nicht Funktion.

Am 2026-09-18 hat ein Toolchange eine volle Spule durch die Düse gedrückt.
Keine der beteiligten Funktionen war kaputt — die Reihenfolge war es:

  1. Der Ziel-Guard lehnte T0 ab, BEVOR T1 entladen war.
  2. Der Fehlerzweig trug daraufhin T0 als geladen ein, obwohl sich nichts
     bewegt hatte. Ab da log der Zustand.
  3. Ein späterer Unload leerte Slot 0, der Pfad blieb belegt (T1), und der
     Flush schloss auf verwaistes Filament — bei einer Spule, die weiter
     nachlieferte.

Diese Tests halten die Reihenfolge fest. Sie steigen deshalb über
perform_tool_change() ein statt die Einzelteile aufzurufen: dass die Teile
funktionieren, war nie das Problem.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestEmptySlotErrorIstUnterscheidbar:
    """Der Guard muss sagen können: hier hat sich noch nichts bewegt."""

    def test_guard_wirft_empty_slot_error(self):
        from extras.ace.manager import AceManager
        from extras.ace.config import EmptySlotError

        manager = MagicMock(spec=AceManager)
        instance = MagicMock()
        instance.instance_num = 0
        instance._is_slot_empty.return_value = True
        instance.inventory = [{"status": "empty"} for _ in range(4)]

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(instance, 0)):
            with pytest.raises(EmptySlotError):
                AceManager.ensure_tool_slot_loaded(manager, 0)

    def test_bleibt_ein_valueerror(self):
        """Bestehende Handler fangen ValueError - die duerfen nicht ausfallen."""
        from extras.ace.config import EmptySlotError
        assert issubclass(EmptySlotError, ValueError)


class TestGuardLaeuftNachDemEntladen:
    """Der Kern: bei geladenem Tool erst entladen, dann das Ziel pruefen."""

    def _manager(self, filament_pos="nozzle"):
        from extras.ace.manager import AceManager

        manager = MagicMock(spec=AceManager)
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.reactor = MagicMock()
        manager.state = MagicMock()
        manager.state.get.side_effect = lambda k, d=None: (
            filament_pos if k == "ace_filament_pos" else d)
        manager.get_switch_state.return_value = True
        manager.has_rdm_sensor.return_value = True
        manager.is_filament_path_free.return_value = False
        manager.is_filament_path_free_instant.return_value = False
        manager.smart_unload.return_value = True
        return manager

    def _run(self, manager, current, target):
        from extras.ace.manager import AceManager
        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(None, None)):
            return AceManager.perform_tool_change(manager, current, target)

    def test_entladen_passiert_vor_der_ablehnung(self):
        """Das eigentliche Ereignis vom 2026-09-18, andersherum."""
        from extras.ace.config import EmptySlotError

        manager = self._manager()
        order = []
        manager.smart_unload.side_effect = lambda *a, **k: (
            order.append("unload") or True)
        manager.ensure_tool_slot_loaded.side_effect = lambda t: (
            order.append("guard"),
            (_ for _ in ()).throw(EmptySlotError("slot leer")))[0]

        with pytest.raises(EmptySlotError):
            self._run(manager, current=1, target=0)

        assert order == ["unload", "guard"], (
            "Das alte Tool muss draussen sein, bevor der leere Zielslot den "
            "Wechsel abbricht - sonst pausiert der Druck mit vollem Pfad.")

    def test_zustand_meldet_nichts_geladen_wenn_das_ziel_abgelehnt_wird(self):
        """Sonst luegt der Zustand ueber das, was physisch im Pfad steckt."""
        from extras.ace.config import EmptySlotError

        manager = self._manager()
        manager.ensure_tool_slot_loaded.side_effect = EmptySlotError("leer")

        with pytest.raises(EmptySlotError):
            self._run(manager, current=1, target=0)

        manager.state.set.assert_any_call("ace_current_index", -1)

    def test_ohne_geladenes_tool_wird_frueh_geprueft(self):
        """Dann kostet der frueher Abbruch nichts und spart das Leerdrehen
        eines ACE2-Feeds auf einen leeren Slot."""
        from extras.ace.config import EmptySlotError

        manager = self._manager()
        manager.ensure_tool_slot_loaded.side_effect = EmptySlotError("leer")

        with pytest.raises(EmptySlotError):
            self._run(manager, current=-1, target=0)

        manager.smart_unload.assert_not_called()

    def test_guard_wird_nicht_doppelt_gerufen(self):
        """Frueh ODER spaet, nicht beides - sonst zwei Fehlerquellen fuer
        dieselbe Bedingung."""
        manager = self._manager()
        manager._feed_filament_into_toolhead = MagicMock(return_value=0.0)

        with patch("extras.ace.manager.get_ace_instance_and_slot_for_tool",
                   return_value=(None, None)):
            from extras.ace.manager import AceManager
            with pytest.raises(Exception):
                # Laeuft bis in den Ladeblock und scheitert dort am Mock -
                # geprueft wird nur die Anzahl der Guard-Aufrufe davor.
                AceManager.perform_tool_change(manager, 1, 0)

        assert manager.ensure_tool_slot_loaded.call_count == 1


class TestFlushBrichtAbWennNachgeliefertWird:
    """Die Notbremse: der RDM verraet, ob der Strang endlich ist."""

    def _manager(self, rdm_clears_after=None):
        from extras.ace.manager import AceManager
        from extras.ace.config import SENSOR_TOOLHEAD, SENSOR_RDM

        manager = MagicMock(spec=AceManager)
        manager.gcode = MagicMock()
        manager.printer = MagicMock()
        manager.reactor = MagicMock()
        manager.reactor.monotonic.return_value = 100.0
        manager.state = MagicMock()

        heater = MagicMock()
        heater.get_temp.return_value = (220.0, 220.0)
        heater.min_extrude_temp = 170.0
        extruder = MagicMock()
        extruder.get_heater.return_value = heater
        manager.printer.lookup_object.return_value = extruder

        manager.has_rdm_sensor.return_value = True
        manager._get_config_for_tool = MagicMock(side_effect=lambda t, p: {
            "total_max_feeding_length": 3000.0,
            "flush_overshoot_length": 10.0,
            "flush_forward_speed": 6.0,
            "parkposition_to_rdm_length": 1000.0,   # Schwelle also 1500mm
        }[p])

        fed = {"mm": 0.0}

        def _move(dist, *a, **k):
            fed["mm"] += dist

        manager._extruder_move = MagicMock(side_effect=_move)
        manager._turn_off_heater_if_idle = MagicMock()

        def _sensor(name):
            if name == SENSOR_TOOLHEAD:
                return True   # wird nie frei: es wird ja nachgeliefert
            if name == SENSOR_RDM:
                return rdm_clears_after is None or fed["mm"] < rdm_clears_after
            return False

        manager.get_switch_state = MagicMock(side_effect=_sensor)
        return manager, fed

    def test_bricht_ab_wenn_der_rdm_belegt_bleibt(self):
        from extras.ace.manager import AceManager
        manager, fed = self._manager(rdm_clears_after=None)

        assert AceManager.flush_forward_until_clear(manager, 0) is False
        assert fed["mm"] < 2000, (
            f"nach {fed['mm']:.0f}mm haette Schluss sein muessen - der RDM "
            f"war die ganze Zeit belegt, es wurde also nachgeliefert")

        text = " ".join(str(c) for c in manager.gcode.respond_info.call_args_list)
        assert "SUPPLIED" in text

    def test_laeuft_weiter_wenn_der_rdm_frei_wird(self):
        """Der legitime Fall: endlicher Strang, RDM wird unterwegs frei."""
        from extras.ace.manager import AceManager
        manager, fed = self._manager(rdm_clears_after=700.0)

        AceManager.flush_forward_until_clear(manager, 0)
        assert fed["mm"] >= 3000, (
            "ohne Nachlieferung darf die Notbremse nicht greifen - hier muss "
            "die normale Obergrenze entscheiden")

    def test_ohne_rdm_sensor_keine_notbremse(self):
        from extras.ace.manager import AceManager
        manager, fed = self._manager(rdm_clears_after=None)
        manager.has_rdm_sensor.return_value = False

        AceManager.flush_forward_until_clear(manager, 0)
        assert fed["mm"] >= 3000
