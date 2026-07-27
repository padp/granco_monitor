"""Thin pylogix wrapper: batched reads, explicit connect/close.

Reconnect-on-repeated-failure is handled by the caller (collector.py),
which owns the retry/backoff loop and can swap in a fresh PlcClient.
"""
from pylogix import PLC

from . import config


class PlcClient:
    def __init__(self, ip_address: str = config.PLC_IP):
        self.ip_address = ip_address
        self._plc = PLC()
        self._plc.IPAddress = ip_address

    def read_all(self, tag_names: list) -> dict:
        """Read a list of tags in one batched call.

        Returns {tag_name: value}. A tag-level failure (e.g. a typo'd or
        momentarily unavailable tag) yields None for that tag rather than
        raising, so one bad tag doesn't take down the whole poll. A
        connection-level failure (PLC unreachable) raises, for the
        caller's retry/backoff loop to handle.
        """
        results = self._plc.Read(tag_names)
        return {r.TagName: (r.Value if r.Status == "Success" else None) for r in results}

    def close(self):
        self._plc.Close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
