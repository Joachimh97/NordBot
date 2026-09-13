"""Offline test runner: fails any accidental network connection."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

if __name__=='__main__':
    root=Path(__file__).resolve().parent
    with patch('socket.socket.connect',side_effect=AssertionError('Programtestene tillater ikke nettverk.')):
        suite=unittest.defaultTestLoader.discover(str(root/'tests'))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
