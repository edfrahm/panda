#!/usr/bin/env python3
# comma 3 (dos, STM32F413) support in the panda library: MCU detection, flash layout and DFU recovery.
# Hardware-free: USB handles are faked; the firmware image checks use the local build (board/obj).
import base64
import binascii
import hashlib
import os
import struct
import unittest
from unittest import mock

from panda import Panda, PandaDFU, McuType, BASEDIR, FW_PATH
from panda.python.usb import PandaUsbHandle, STBootloaderUSBHandle

F4_DFU_FLASH_DESC = "@Internal Flash  /0x08000000/04*016Kg,01*064Kg,011*128Kg"
H7_DFU_FLASH_DESC = "@Internal Flash   /0x08000000/08*128Kg"
FLASHER_ECHO_C1 = b'\xff\x00\xc1\x3e\xde\xad\xd0\x0d' + b'\x00' * 4  # bootstub reply without the hw type endpoint


class FakeLibusbHandle:
  def __init__(self, descriptors=None, bcd=0x2300):
    self.descriptors = descriptors or {}
    self.bcd = bcd
    self.writes = []

  def getStringDescriptor(self, i, _lang):
    return self.descriptors.get(i)

  def controlRead(self, request_type, request, value, index, length, timeout=0):
    if request == 3:  # DFU_GETSTATUS: idle, no error
      return b'\x00' * 6
    return b''

  def controlWrite(self, request_type, request, value, index, data, timeout=0):
    self.writes.append((request, value, bytes(data)))

  def getDevice(self):
    return mock.Mock(getbcdDevice=lambda: self.bcd)

  def close(self):
    pass


def fake_panda(hw_type_reply, bootstub=False, bcd=0x2300):
  libusb = FakeLibusbHandle(bcd=bcd)
  handle = PandaUsbHandle(libusb)
  handle.controlRead = mock.Mock(side_effect=lambda rt, req, *a, **kw: hw_type_reply if req == 0xc1 else b'')
  p = Panda.__new__(Panda)
  p._handle = handle
  p._bcd_hw_type = None
  p.bootstub = bootstub
  p._mcu_type = p._detect_mcu_type()
  return p


class TestF4Config(unittest.TestCase):
  def test_flash_layout(self):
    cfg = McuType.F4.config
    self.assertEqual(cfg.mcu_idcode, 0x463)
    self.assertEqual(cfg.sector_sizes, [0x4000] * 4 + [0x10000] + [0x20000] * 11)
    self.assertEqual(cfg.sector_count, 16)
    self.assertEqual(cfg.bootstub_address, 0x8000000)
    self.assertEqual(cfg.app_address, 0x8004000)
    self.assertEqual(cfg.sector_address(1), cfg.app_address)  # app starts right after the 16K bootstub sector
    self.assertEqual(cfg.sector_address(4), 0x8010000)
    self.assertEqual(cfg.sector_address(5), 0x8020000)
    self.assertEqual((cfg.app_fn, cfg.bootstub_fn), ("panda.bin.signed", "bootstub.panda.bin"))

  def test_dos_is_supported_internal_f4(self):
    self.assertIn(Panda.HW_TYPE_DOS, Panda.F4_DEVICES)
    self.assertIn(Panda.HW_TYPE_DOS, Panda.SUPPORTED_DEVICES)
    self.assertIn(Panda.HW_TYPE_DOS, Panda.INTERNAL_DEVICES)
    self.assertNotIn(Panda.HW_TYPE_DOS, Panda.H7_DEVICES)


class TestMcuDetection(unittest.TestCase):
  def test_app_reports_dos(self):
    p = fake_panda(Panda.HW_TYPE_DOS)
    self.assertEqual(p.get_mcu_type(), McuType.F4)
    self.assertEqual(p.get_type(), Panda.HW_TYPE_DOS)

  def test_h7_pandas_stay_h7(self):
    for hw_type in Panda.H7_DEVICES:
      self.assertEqual(fake_panda(hw_type).get_mcu_type(), McuType.H7)
      self.assertEqual(fake_panda(hw_type, bootstub=True).get_mcu_type(), McuType.H7)

  def test_old_bootstub_with_bcd_hw_type(self):
    # case B: no hw type endpoint, panda type in bcdDevice
    p = fake_panda(FLASHER_ECHO_C1, bootstub=True, bcd=0x0600)
    self.assertEqual(p.get_mcu_type(), McuType.F4)
    self.assertEqual(p.get_type(), Panda.HW_TYPE_DOS)

  def test_oldest_bootstub_assumed_f4(self):
    # case A: no hw type endpoint, bcdDevice is the old constant
    p = fake_panda(FLASHER_ECHO_C1, bootstub=True, bcd=0x2300)
    self.assertEqual(p.get_mcu_type(), McuType.F4)

  def test_flash_picks_f4_firmware(self):
    p = fake_panda(Panda.HW_TYPE_DOS, bootstub=True)
    with mock.patch.object(Panda, "up_to_date", return_value=False), \
         mock.patch.object(Panda, "get_version", return_value="DEV"), \
         mock.patch.object(Panda, "flash_static") as flash_static, \
         mock.patch("os.path.isfile", return_value=True), \
         mock.patch("builtins.open", mock.mock_open(read_data=b"\x00" * 1024)) as fopen:
      p.flash(reconnect=False)
    fopen.assert_called_with(os.path.join(FW_PATH, "panda.bin.signed"), "rb")
    self.assertEqual(flash_static.call_args.kwargs["mcu_type"], McuType.F4)


class TestF4Flashing(unittest.TestCase):
  def _flash(self, size):
    handle = mock.Mock()
    handle.controlRead.return_value = b'\xff\x00\xb0\x4f\xde\xad\xd0\x0d' + b'\x00' * 4  # flasher present
    Panda.flash_static(handle, b'\x00' * size, mcu_type=McuType.F4)
    return [c.args[2] for c in handle.controlWrite.call_args_list if c.args[1] == 0xb2]

  def test_erases_only_app_sectors(self):
    # 16K + 16K + 16K + 64K covers a 90K image: sectors 1-4, never the bootstub sector 0
    self.assertEqual(self._flash(90 * 1024), [1, 2, 3, 4])
    self.assertEqual(self._flash(10 * 1024), [1])

  def test_too_large_image_rejected(self):
    with self.assertRaises(AssertionError):
      self._flash(1024 * 1024)


class TestF4Dfu(unittest.TestCase):
  def test_dfu_detects_f4_by_sector_count(self):
    self.assertEqual(STBootloaderUSBHandle(None, FakeLibusbHandle({4: F4_DFU_FLASH_DESC})).get_mcu_type(), McuType.F4)
    self.assertEqual(STBootloaderUSBHandle(None, FakeLibusbHandle({4: H7_DFU_FLASH_DESC})).get_mcu_type(), McuType.H7)

  def test_dfu_serial(self):
    uid = struct.pack("H" * 6, 0x0011, 0x0022, 0x0033, 0x0044, 0x0055, 0x0066).hex()
    self.assertEqual(PandaDFU.st_serial_to_dfu_serial(uid, McuType.F4), "008800700044")
    self.assertEqual(PandaDFU.st_serial_to_dfu_serial(uid, McuType.H7), "008800660044")
    self.assertIsNone(PandaDFU.st_serial_to_dfu_serial("none", McuType.F4))

  def test_recover_writes_f4_bootstub(self):
    libusb = FakeLibusbHandle({4: F4_DFU_FLASH_DESC})
    dfu = PandaDFU.__new__(PandaDFU)
    dfu._handle = STBootloaderUSBHandle(None, libusb)
    dfu._context = None
    dfu._mcu_type = dfu._handle.get_mcu_type()

    code = bytes(range(256)) * 20  # 5120 bytes
    dfu.program_bootstub(code)
    dfu.reset()

    dnload = [(v, d) for req, v, d in libusb.writes if req == STBootloaderUSBHandle.DFU_DNLOAD]
    erases = [struct.unpack("I", d[1:])[0] for v, d in dnload if v == 0 and d[:1] == b"\x41"]
    self.assertEqual(erases, [0x8000000, 0x8004000])  # bootstub sector + first app sector
    set_addr = [struct.unpack("I", d[1:])[0] for v, d in dnload if v == 0 and d[:1] == b"\x21"]
    self.assertEqual(set_addr, [0x8000000, 0x8000000])  # program, then jump to the bootstub
    blocks = [d for v, d in dnload if v >= 2 and d]
    self.assertTrue(all(len(b) == McuType.F4.config.block_size for b in blocks))
    self.assertEqual(b"".join(blocks)[:len(code)], code)


@unittest.skipUnless(os.path.isfile(os.path.join(FW_PATH, "panda.bin.signed")), "F4 firmware not built")
class TestF4Firmware(unittest.TestCase):
  def test_bootstub_vectors(self):
    with open(os.path.join(FW_PATH, McuType.F4.config.bootstub_fn), "rb") as f:
      sp, reset = struct.unpack("<II", f.read(8))
    self.assertEqual(sp, 0x2001FFFC)
    self.assertTrue(0x8000000 <= reset < McuType.F4.config.app_address)
    self.assertLessEqual(os.path.getsize(os.path.join(FW_PATH, McuType.F4.config.bootstub_fn)), 0x4000)

  def test_app_signed_with_debug_key(self):
    with open(os.path.join(FW_PATH, McuType.F4.config.app_fn), "rb") as f:
      dat = f.read()
    length, reset = struct.unpack("<II", dat[:8])
    self.assertTrue(McuType.F4.config.app_address <= reset < 0x8100000)
    self.assertEqual(dat[length - 8:length], b"VERS" + struct.pack("I", 2))
    self.assertEqual(len(dat), length + 128)

    # same check as the bootstub: RSA over the SHA1 of everything after the length word
    with open(os.path.join(BASEDIR, "board/crypto/certs/debug.pub")) as f:
      key = base64.b64decode(f.read().split()[1])
    fields = []
    while key:
      n = int.from_bytes(key[:4], "big")
      fields.append(key[4:4 + n])
      key = key[4 + n:]
    e, n = int.from_bytes(fields[1], "big"), int.from_bytes(fields[2], "big")
    decrypted = pow(int.from_bytes(dat[length:], "big"), e, n).to_bytes(128, "big")
    self.assertEqual(decrypted[-20:], hashlib.sha1(dat[4:length]).digest())
    self.assertEqual(binascii.hexlify(decrypted[:2]), b"0001")


if __name__ == "__main__":
  unittest.main()
