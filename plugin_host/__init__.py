"""Trusted, provider-independent plugin control plane; never imports plugins."""

from .registry import PluginRegistry

__all__ = ["PluginRegistry"]
