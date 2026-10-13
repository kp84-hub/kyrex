"""Pytest plugin for connector checks: deny every real network connection.

Use with PYTHONPATH=kyrex-cloud and pytest -p offline_tests. Provider responses
must be injected; synthetic credentials can never reach a live provider.
"""

import socket


def _deny_network(*args, **kwargs):
    raise AssertionError("Offline connector tests attempted a network connection")


def pytest_configure(config):
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    socket.create_connection = _deny_network
