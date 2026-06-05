"""JSON-RPC API definition."""

import dataclasses
from typing import Dict, Union

from ._utils import _snake2camel
from .transport import RpcTransport
from .types import MsgData


class Rpc:
    """Access to the Delta Chat JSON-RPC API."""

    def __init__(self, transport: RpcTransport) -> None:
        self.transport = transport

    def __getattr__(self, attr: str):
        return lambda *args: self.transport.call(attr, *args)

    def send_msg(self, accid: int, chatid: int, data: Union[MsgData, Dict]) -> int:
        """Send a message and return the message ID of the sent message."""
        if dataclasses.is_dataclass(data):
            json_obj = _snake2camel(dataclasses.asdict(data))
        else:
            # Assume it's already a dict (e.g., from JSON-RPC)
            json_obj = _snake2camel(data)
        return self.transport.call("send_msg", accid, chatid, json_obj)
