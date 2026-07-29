"""Adapters connecting the SDR application layer to external frameworks."""

from sdr.adapters.django_crm import (
    DjangoCRMWriter,
    DjangoLeadDeduplicator,
    LeastLoadedSalesRouter,
)

__all__ = [
    "DjangoCRMWriter",
    "DjangoLeadDeduplicator",
    "LeastLoadedSalesRouter",
]
