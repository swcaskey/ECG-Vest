from types import SimpleNamespace
import unittest

from ecg.desktop_app import NANO_USB_ID, available_ports, connection_ports, device_id


def port(name, usb_id=NANO_USB_ID, serial_number=None, hwid="USB"):
    return SimpleNamespace(
        device=name,
        vid=usb_id[0],
        pid=usb_id[1],
        serial_number=serial_number,
        hwid=hwid,
    )


class DesktopConnectionTests(unittest.TestCase):
    def test_single_nano_is_automatic_and_bluetooth_com_is_hidden(self):
        nano = port("COM7")
        bluetooth = port("COM8", (None, None), hwid=r"BTHENUM\0000")
        adapter = port("COM9", (0x1A86, 0x7523))
        ports = available_ports([nano, bluetooth, adapter])
        self.assertEqual(ports, [nano, adapter])
        self.assertEqual(connection_ports(ports, None, None), [nano])
        self.assertEqual(connection_ports([adapter], None, None), [])

    def test_two_unknown_nanos_require_a_choice(self):
        nanos = [port("COM7"), port("COM10")]
        self.assertEqual(connection_ports(nanos, None, None), nanos)

    def test_remembered_device_follows_serial_number_and_separates_roles(self):
        wired = port("COM7", serial_number="wired")
        receiver = port("COM10", serial_number="receiver")
        identity = device_id(port("COM4", serial_number="receiver"))
        self.assertEqual(
            connection_ports([wired, receiver], identity, None), [receiver]
        )
        self.assertEqual(
            connection_ports([wired, receiver], None, device_id(wired)), [receiver]
        )
        self.assertEqual(connection_ports([wired], None, device_id(wired)), [])
