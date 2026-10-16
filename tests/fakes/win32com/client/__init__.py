"""Fake ``win32com.client`` (test double). Production code uses ``win32com.client.dynamic.Dispatch`` only."""

from . import dynamic

__fake__ = True

Dispatch = dynamic.Dispatch
DispatchEx = dynamic.Dispatch
