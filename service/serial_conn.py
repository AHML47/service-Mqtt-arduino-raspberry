"""
Serial connection to the Arduino.

Responsibilities:
  - Open / reconnect the serial port
  - Send a command string (device:action:params\n)
  - Wait for a response line (OK:... or ERR:...)
  - Continuously read unsolicited push lines (e.g. dht1:OK:24.5:55.0)
    and forward them via a callback
"""

import logging
import threading
import time
from typing import Callable, Optional

import serial

logger = logging.getLogger(__name__)

# Type alias for the push-data callback
PushCallback = Callable[[str, str], None]  # (device, payload)


class SerialConnection:
    """
    Thread-safe, reconnecting serial bridge to the Arduino.

    Usage:
        conn = SerialConnection(port="/dev/ttyUSB0", baudrate=9600)
        conn.on_push = my_callback   # receives unsolicited lines
        conn.start()                 # launches reader thread
        resp = conn.send("dht1:READ")  # blocks until reply
        conn.stop()
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 1.0,
        response_timeout: float = 2.0,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._response_timeout = response_timeout

        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()           # guards send/receive
        self._response_event = threading.Event()
        self._response_line: Optional[str] = None

        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

        # External callback for unsolicited push lines
        self.on_push: Optional[PushCallback] = None

    # ── Lifecycle ────────────────────────────────────────────

    def start(self):
        """Open serial port and start the background reader."""
        self._connect()
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="serial-reader"
        )
        self._reader_thread.start()
        logger.info("Serial reader started on %s @ %d", self._port, self._baudrate)

    def stop(self):
        """Shut down reader thread and close the port."""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3)
        self._disconnect()
        logger.info("Serial connection closed")

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ── Public API ───────────────────────────────────────────

    def send(self, command: str) -> str:
        """
        Send a command and wait for the Arduino's response.

        Args:
            command: e.g. "dht1:READ" or "step1:MOVE:1000:150"

        Returns:
            The response string (e.g. "OK:24.5") or "ERR:timeout"

        Thread-safe — only one send() at a time.
        """
        with self._lock:
            if not self.is_connected:
                return "ERR:not_connected"

            self._response_event.clear()
            self._response_line = None

            line = command.strip() + "\n"
            try:
                self._ser.write(line.encode("utf-8"))
                self._ser.flush()
                logger.debug("TX → %s", command.strip())
            except serial.SerialException as e:
                logger.error("Serial write failed: %s", e)
                self._schedule_reconnect()
                return "ERR:write_fail"

            # Wait for the reader thread to capture the response
            if self._response_event.wait(timeout=self._response_timeout):
                resp = self._response_line or "ERR:empty"
                logger.debug("RX ← %s", resp)
                return resp
            else:
                logger.warning("Timeout waiting for response to: %s", command)
                return "ERR:timeout"

    # ── Internal: reader loop ────────────────────────────────

    def _reader_loop(self):
        """Background thread that continuously reads lines from serial."""
        while self._running:
            if not self.is_connected:
                self._reconnect_wait()
                continue

            try:
                raw = self._ser.readline()
                if not raw:
                    continue  # timeout, no data

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                self._classify_line(line)

            except serial.SerialException as e:
                logger.error("Serial read error: %s", e)
                self._schedule_reconnect()
            except Exception as e:
                logger.exception("Unexpected error in reader: %s", e)

    def _classify_line(self, line: str):
        """
        Decide if a line is a response to a pending send() or an
        unsolicited push from the Arduino.

        Response lines start with OK: / ERR: / BUSY:
        Push lines start with a device name, e.g. "dht1:OK:24.5:55.0"
        """
        # If a send() is waiting for a reply, give it first dibs —
        # but only if the line looks like a bare response (no device prefix
        # before the status). Push lines always have the device name prepended.
        is_push = False
        if ":" in line:
            first_token = line.split(":")[0]
            # If the first token is a status keyword, this is a direct response.
            if first_token in ("OK", "ERR", "BUSY"):
                is_push = False
            else:
                # Looks like "dht1:OK:24.5" — unsolicited push
                is_push = True

        if is_push:
            device = line.split(":")[0]
            payload = line[len(device) + 1:]  # everything after "dht1:"
            logger.debug("PUSH ← %s → %s", device, payload)
            if self.on_push:
                try:
                    self.on_push(device, payload)
                except Exception as e:
                    logger.error("Push callback error: %s", e)
        else:
            # Direct response to a pending send()
            self._response_line = line
            self._response_event.set()

    # ── Internal: connection management ──────────────────────

    def _connect(self):
        try:
            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
            )
            # Arduino resets on serial open — give it time to boot
            time.sleep(2)
            # Drain any boot messages
            if self._ser.in_waiting:
                self._ser.read(self._ser.in_waiting)
            logger.info("Serial connected: %s", self._port)
        except serial.SerialException as e:
            logger.error("Cannot open serial port %s: %s", self._port, e)
            self._ser = None

    def _disconnect(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _schedule_reconnect(self):
        self._disconnect()

    def _reconnect_wait(self):
        """Try reconnecting every 5 seconds."""
        logger.info("Attempting serial reconnect in 5s...")
        time.sleep(5)
        self._connect()
