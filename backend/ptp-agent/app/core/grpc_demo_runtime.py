from __future__ import annotations

import asyncio
from concurrent import futures
import logging
import os
from typing import Optional

import grpc

from app.core.nacos_register import AgentNacosRegister

logger = logging.getLogger(__name__)


def _encode_varint(value: int) -> bytes:
    encoded = bytearray()
    remaining = int(value)
    while True:
        current = remaining & 0x7F
        remaining >>= 7
        if remaining:
            encoded.append(current | 0x80)
        else:
            encoded.append(current)
            return bytes(encoded)


def _decode_varint(payload: bytes, index: int = 0) -> tuple[int, int]:
    shift = 0
    result = 0
    cursor = index
    while True:
        current = payload[cursor]
        cursor += 1
        result |= (current & 0x7F) << shift
        if not (current & 0x80):
            return result, cursor
        shift += 7


def _encode_string_field(field_number: int, value: str) -> bytes:
    text = value.encode("utf-8")
    key = (field_number << 3) | 2
    return _encode_varint(key) + _encode_varint(len(text)) + text


def _decode_first_string_field(payload: bytes, field_number: int) -> str:
    if not payload:
        return ""
    expected_key = (field_number << 3) | 2
    key, cursor = _decode_varint(payload, 0)
    if key != expected_key:
        raise ValueError(f"unexpected field key {key}")
    length, cursor = _decode_varint(payload, cursor)
    return payload[cursor : cursor + length].decode("utf-8")


def _say_hello(payload: bytes, _context: grpc.ServicerContext) -> bytes:
    name = _decode_first_string_field(payload, field_number=1) if payload else ""
    return _encode_string_field(1, f"Hello, {name or 'world'}")


class GrpcDemoRuntime:
    def __init__(self):
        self.enabled = os.getenv("ENABLE_GRPC_DEMO_SERVICE", "0") == "1"
        self.port = int(os.getenv("GRPC_DEMO_PORT", "50052"))
        self.service_name = os.getenv("GRPC_DEMO_NACOS_SERVICE_NAME", "ptp-grpc-demo")
        self.server: Optional[grpc.Server] = None
        self.registrar = AgentNacosRegister()
        self.registrar.service_name = self.service_name
        self.registrar.port = self.port
        self.registrar.heartbeat_interval = int(
            os.getenv("GRPC_DEMO_NACOS_HEARTBEAT_INTERVAL", "5")
        )

    async def start(self) -> bool:
        if not self.enabled:
            logger.info("ENABLE_GRPC_DEMO_SERVICE!=1, skip grpc demo runtime")
            return False

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        rpc_handler = grpc.unary_unary_rpc_method_handler(
            _say_hello,
            request_deserializer=lambda payload: payload,
            response_serializer=lambda payload: payload,
        )
        generic_handler = grpc.method_handlers_generic_handler(
            "hello.Hello",
            {"SayHello": rpc_handler},
        )
        server.add_generic_rpc_handlers((generic_handler,))
        bound_port = server.add_insecure_port(f"0.0.0.0:{self.port}")
        if bound_port != self.port:
            logger.warning("gRPC demo port bind failed: expected=%s actual=%s", self.port, bound_port)
            return False
        server.start()
        self.server = server
        registered = await self.registrar.start()
        if not registered:
            logger.warning(
                "gRPC demo runtime started on %s but nacos register failed; server still accepts connections",
                self.port,
            )
        else:
            logger.info("gRPC demo runtime started on %s and registered as %s", self.port, self.service_name)
        return True

    async def stop(self):
        await self.registrar.stop()
        if self.server is not None:
            await asyncio.to_thread(self.server.stop(grace=None).wait)
            self.server = None
