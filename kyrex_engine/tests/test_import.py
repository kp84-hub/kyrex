"""Verify kyrex.core.PlaneExecute imports cleanly."""

from kyrex.core import PlaneExecute


class TestPlaneExecuteImport:
    """Test that PlaneExecute imports cleanly and exposes the expected API."""

    def test_plane_execute_imports_cleanly(self):
        """PlaneExecute should be importable without error."""
        assert PlaneExecute is not None

    def test_has_get_usage_stats_method(self):
        """PlaneExecute should expose a get_usage_stats method."""
        assert hasattr(PlaneExecute, "get_usage_stats")
        assert callable(getattr(PlaneExecute, "get_usage_stats"))
