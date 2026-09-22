"""Release gates: disabled presentation layers must not start resources."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.config import load_config
from src.core.session_control import SessionController
from src.desktop.overlay import ControlOverlay
from uah.hosts.embedded.bootstrap import uah_begin_boot


class ReleaseScope(unittest.TestCase):
    def test_legacy_overlay_cannot_start_a_thread(self):
        with patch('src.desktop.overlay.threading.Thread', side_effect=AssertionError('UI thread started')):
            overlay = ControlOverlay(SessionController())
            overlay.start()
            self.assertFalse(overlay.available)
            self.assertIsNone(overlay._thread)
            self.assertIsNone(overlay._root)
            overlay.stop()

    def test_uah_cannot_start_even_when_old_configuration_enables_it(self):
        with patch('uah.core.daemon.ensure_persistent_hub', side_effect=AssertionError('Hub started')):
            boot = uah_begin_boot({'uah': {'enabled': True}})
            self.assertFalse(boot.enabled)
            self.assertIsNone(boot.adapter)
            self.assertIsNone(boot.hub_server)

    def test_example_configuration_disables_both_presentation_layers(self):
        config = load_config()
        self.assertFalse(config.get('uah.enabled'))
        self.assertFalse(config.get('desktop.overlay.enabled'))
        self.assertTrue(config.get('desktop.safety_banner.enabled', True))


if __name__ == '__main__':
    unittest.main()
