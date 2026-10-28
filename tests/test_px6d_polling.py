"""Synthetic serial transactions; no device is opened or hardware qualified."""
import struct
import threading

import pytest

from ninefbg import devices
from ninefbg.devices import PX6DSource, command_packet, crc8


VALUES = [1.25, -2.5, 3.75, -.125, .25, -.5]


def force_packet(values=VALUES):
    body = bytes.fromhex("aa550103") + struct.pack("<6f", *values)
    return body + bytes((crc8(body),))


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000

    def advance(self, ns):
        self.ns += ns

    def seconds(self):
        return self.ns / 1e9


class FakePort:
    is_open = True

    def __init__(self, clock, packet=None):
        self.clock = clock
        self.packet = force_packet() if packet is None else packet
        self.rx = bytearray()
        self.events = []
        self.flush_calls = self.read_calls = 0
        self.written_length = 6
        self.write_complete = False

    def reset_input_buffer(self):
        self.events.append("reset")
        self.clock.advance(2_000_000)
        self.rx.clear()

    def write(self, data):
        assert data == command_packet(5)
        self.events.append("write")
        self.clock.advance(3_000_000)
        self.write_complete = True
        self.rx.extend(self.packet)
        return self.written_length

    def flush(self):
        # Model serialwin32.flush's 50 ms sleep when out_waiting is nonzero.
        self.flush_calls += 1
        self.clock.advance(50_000_000)

    @property
    def in_waiting(self):
        assert self.write_complete
        return len(self.rx)

    def read(self, length):
        assert self.write_complete
        self.events.append("read")
        self.read_calls += 1
        self.clock.advance(4_000_000)
        value = bytes(self.rx[:length])
        del self.rx[:length]
        return value


@pytest.fixture
def transaction(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(devices.time, "perf_counter", clock.seconds)
    monkeypatch.setattr(devices.time, "perf_counter_ns", lambda: clock.ns)
    source = PX6DSource({"poll_hz": 50, "device_id": 127, "read_timeout_sec": .02})
    source.port = FakePort(clock)
    source.stream_epoch = "synthetic-force-epoch"
    return source, source.port, clock


def test_steady_poll_uses_completed_write_without_flush_and_preserves_raw_time(transaction):
    source, port, clock = transaction
    start = clock.ns
    frame = source.read()
    assert port.flush_calls == 0
    assert port.events == ["reset", "write", "read"]
    assert frame["force_values"] == VALUES
    assert frame["raw_packet_hex"] == force_packet().hex()
    assert frame["request_device_id"] == 127 and frame["response_device_id"] == 1
    assert frame["response_address_verified"] is False
    assert frame["stream_epoch"] == "synthetic-force-epoch"
    assert frame["device_sequence"] is frame["device_timestamp_raw"] is None
    assert frame["read_start_ns"] == start
    assert frame["host_rx_monotonic_ns"] == frame["read_end_ns"] == start + 9_000_000
    assert frame["roundtrip_ms"] == 9
    timing = frame["read_timing"]
    assert timing == {
        "schema": "px6d_read_timing_v1", "read_start_ns": start,
        "input_reset_start_ns": start, "input_reset_end_ns": start + 2_000_000,
        "input_reset_ms": 2, "write_start_ns": start + 2_000_000,
        "write_end_ns": start + 5_000_000, "write_ms": 3,
        "response_start_ns": start + 5_000_000, "response_end_ns": start + 9_000_000,
        "response_ms": 4, "roundtrip_ms": 9, "status": "valid",
    }
    assert frame["preprocessing"] == {"software_tare_applied": False, "filter_applied": False, "hardware_zero_command_sent": False}
    assert source.read() is None, "The existing 50 Hz polling gate remains active"
    clock.advance(11_000_000)
    second = source.read()
    assert second["frame_id"] == second["local_receive_id"] == 2
    assert second["read_start_ns"] == start + 20_000_000
    assert port.flush_calls == 0
    assert source.metrics.received == 2 and source.metrics.errors == 0
    diagnostic = source.diagnostics()
    assert diagnostic["read_timing"] == second["read_timing"]
    diagnostic["read_timing"]["write_ms"] = 999
    second["read_timing"]["write_ms"] = 888
    assert source.diagnostics()["read_timing"]["write_ms"] == 3


def test_response_read_waits_for_blocking_write_completion(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    responses, failures, packet_reads = [], [], []

    class BlockingPort:
        is_open = True
        completed = False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            entered.set()
            assert release.wait(2)
            self.completed = True
            return len(data)

        def flush(self):
            pytest.fail("Steady reads must not call flush")

    source = PX6DSource({"poll_hz": 50, "device_id": 127})
    source.port = BlockingPort()

    def receive(length, timeout):
        assert source.port.completed
        packet_reads.append((length, timeout))
        return force_packet()

    monkeypatch.setattr(source, "_packet", receive)

    def run():
        try:
            responses.append(source.read())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        assert not packet_reads and not responses
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not failures
    assert len(packet_reads) == len(responses) == 1
    assert responses[0]["force_values"] == VALUES


@pytest.mark.parametrize("written", [0, 5, None, True])
def test_incomplete_write_rejects_transaction_before_reading_response(transaction, written):
    source, port, _ = transaction
    port.written_length = written
    with pytest.raises(OSError, match="未完整发送"):
        source.read()
    assert port.read_calls == port.flush_calls == 0
    assert source.metrics.received == 0 and source.metrics.errors == 1
    timing = source.diagnostics()["read_timing"]
    assert timing["status"] == "failed" and timing["failed_stage"] == "write"
    assert timing["write_ms"] == 3 and "response_start_ns" not in timing


def test_timeout_keeps_error_and_next_query_discards_pending_bytes(transaction):
    source, port, clock = transaction
    port.packet = b""
    with pytest.raises(TimeoutError, match="读取超时"):
        source.read()
    timing = source.diagnostics()["read_timing"]
    assert timing["status"] == "failed" and timing["failed_stage"] == "response"
    assert timing["response_ms"] >= 20 and source.metrics.received == 0
    assert source.metrics.errors == 1
    # Neither a stale port response nor a parser fragment may become new data.
    port.rx.extend(force_packet([9.] * 6))
    source._buffer.extend(force_packet([8.] * 6))
    port.packet = force_packet()
    clock.advance(20_000_000)
    frame = source.read()
    assert frame["force_values"] == VALUES and frame["frame_id"] == 1
    assert frame["read_timing"]["status"] == "valid"
    assert port.events.count("reset") == 2 and port.flush_calls == 0
    assert source.metrics.errors == 1, "Existing cumulative error semantics remain unchanged"


def test_crc_failure_keeps_original_packet_and_receive_boundary(transaction):
    source, port, _ = transaction
    good = force_packet()
    port.packet = good[:-1] + bytes((good[-1] ^ 1,))
    with pytest.raises(ValueError, match="CRC"):
        source.read()
    diagnostic = source.diagnostics()
    failed, timing = diagnostic["last_failed_packet"], diagnostic["read_timing"]
    assert diagnostic["last_failed_packet_hex"] == port.packet.hex()
    assert failed["calculated_crc"] == good[-1] and failed["received_crc"] == port.packet[-1]
    assert failed["read_start_ns"] == timing["read_start_ns"]
    assert failed["host_rx_monotonic_ns"] == timing["response_end_ns"]
    assert timing["status"] == "failed" and timing["failed_stage"] == "validation"
    assert source.metrics.received == 0 and source.metrics.errors == 1
    assert port.flush_calls == 0


def test_handshake_preserves_flush_and_positive_write_timeout(monkeypatch):
    import serial

    clock = Clock()
    monkeypatch.setattr(devices.time, "perf_counter", clock.seconds)
    monkeypatch.setattr(devices.time, "perf_counter_ns", lambda: clock.ns)
    monkeypatch.setattr(devices.time, "sleep", lambda seconds: clock.advance(int(seconds * 1e9)))
    handshake_body = bytes.fromhex("aa557f0700") + b"v1.0.3\x00"
    handshake = handshake_body + bytes((crc8(handshake_body),))

    class HandshakePort(FakePort):
        def reset_output_buffer(self):
            pass

        def write(self, data):
            if data == command_packet(7):
                self.write_complete = True
                self.rx.extend(handshake)
                return len(data)
            return super().write(data)

    port, arguments = HandshakePort(clock), []

    def create_port(*args, **kwargs):
        arguments.append((args, kwargs))
        return port

    monkeypatch.setattr(serial, "Serial", create_port)
    source = PX6DSource({"poll_hz": 50, "device_id": 127})
    monkeypatch.setattr(source.lease, "acquire", lambda: None)
    monkeypatch.setattr(source, "_find_port", lambda: "synthetic-port")
    source.open()
    assert arguments[0][1]["write_timeout"] == .25
    assert source.firmware == "v1.0.3" and port.flush_calls == 1
    assert source.diagnostics()["read_timing"] is None
    frame = source.read()
    assert frame["force_values"] == VALUES and port.flush_calls == 1
