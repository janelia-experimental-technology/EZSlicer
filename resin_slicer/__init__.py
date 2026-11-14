"""Dependency-light MSLA resin slicer."""

__version__ = "0.3.0"

from .config import PrintConfig, SupportConfig
from .pipeline import SliceJob, slice_to_file
from .transform import MeshTransform

__all__ = ["__version__", "MeshTransform", "PrintConfig", "SupportConfig", "SliceJob", "slice_to_file"]
