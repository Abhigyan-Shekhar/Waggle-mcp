import json
import struct

from rlm.core.comms_utils import socket_recv


class FakeSocket:
    """Socket test double that simulates fragmented TCP header delivery."""

    def __init__(self):
        payload = json.dumps({"hello": "world"}).encode("utf-8")
        header = struct.pack(">I", len(payload))

        self.chunks = [
            header[:2],  # partial header
            header[2:],  # rest of header
            payload,
        ]

    def recv(self, _size):
        """Return the next chunk of socket data."""
        return self.chunks.pop(0)


def test_socket_recv_handles_partial_header():
    """Verify socket_recv reconstructs fragmented TCP length headers."""
    result = socket_recv(FakeSocket())

    assert result == {"hello": "world"}
