# Hardware identification, backup, and recovery

**Do not flash anything until you've completed this checklist.** If your
device was previously running other firmware (e.g. NerdMiner), a backup lets
you go back to stock at any time.

## 1. Identify your board (read-only, safe)

Visual checks first:
- Look for a silkscreen label on the back: "T-Display", "TTGO", "LILYGO", a
  Waveshare/Sunton part number, etc.
- Measure the display diagonal and note the resolution if visible in any
  existing UI.
- Count and note the position of the physical buttons.
- Note the USB connector type and the USB-serial chip printed near it
  (CP2102, CH340, CH9102, …).

Then, with the board connected over USB (this does not touch the flash):

```bash
pip install esptool
esptool.py --port /dev/ttyUSB0 chip_id     # or /dev/ttyACM0, /dev/cu.usbserial-*
esptool.py --port /dev/ttyUSB0 flash_id
```

This tells you the exact chip variant (e.g. `ESP32-D0WDQ6` = classic ESP32,
**not** S3/C3/C6) and flash size — both read-only.

This project was built and tested against a classic (non-S3) LilyGO/TTGO
T-Display: `ESP32-D0WDQ6`, ST7789 135×240, buttons on GPIO0/GPIO35. If your
board reports a different chip family (S3/C3/C6) or a different display
size, the pin/display config in `firmware/platformio.ini` and
`firmware/src/main.cpp` will need to be adjusted — check
[BitMaker-hub/NerdMiner_v2](https://github.com/BitMaker-hub/NerdMiner_v2)
for the board profile matching your specific variant.

## 2. Back up the current flash contents

```bash
esptool.py --port /dev/ttyUSB0 flash_id   # note the flash size, e.g. 16MB = 0x1000000
esptool.py --port /dev/ttyUSB0 read_flash 0x0 0x1000000 backup_$(date +%Y%m%d).bin
sha256sum backup_$(date +%Y%m%d).bin
```

Store this file in at least two places. It contains a full image of whatever
firmware and configuration (including any WiFi credentials stored in NVS)
was previously on the device — do not commit it anywhere public.

## 3. Restore procedure (if you ever need to go back)

```bash
esptool.py --port /dev/ttyUSB0 write_flash 0x0 backup_YYYYMMDD.bin
```

## 4. Flashing this project

Once you're confident about your board profile and have a backup:

```bash
cd firmware
pio run -t upload
```

If the upload fails with a handshake/connect error, try a lower
`upload_speed` in `platformio.ini` (e.g. `115200`) — some USB-serial chips
and cables are unreliable at higher baud rates.
