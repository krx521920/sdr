"""Validated write boundary between the SDR pipeline and CRM records."""

from sdr.domain import HandoffPackage
from sdr.ports import CRMWriterPort

__all__ = ["CRMWriterPort", "HandoffPackage"]
