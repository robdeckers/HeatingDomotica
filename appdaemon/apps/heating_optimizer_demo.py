"""AppDaemon entry module for the demo app (AppDaemon loads top-level modules; the code lives in the package)."""

from heating_optimizer.demo_app import DemoApp

__all__ = ["DemoApp"]
