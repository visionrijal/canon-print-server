# Canon LBP2900/2900B WiFi Print Server for Termux

Turn a **rooted Android phone** into a wireless print server for the Canon LBP2900/2900B
— no `usblp` kernel module required, no Raspberry Pi, no extra hardware.

## How it works

Most guides fail on stock Android because the `usblp` kernel module isn't compiled
into Samsung/Xiaomi/etc. stock kernels. This project bypasses that entirely:

```
Other devices (WiFi)
       ↓  IPP protocol (port 631)
  print_server.py  ←  listens for jobs
       ↓
  Ghostscript  ←  renders PDF/PS to raster
       ↓
  rastertocapt  ←  converts raster to Canon CAPT protocol
       ↓
  termux-usb + libusb  ←  sends via Android USB Host API
       ↓
  Canon LBP2900B (OTG cable)
```

Key insight: `termux-usb` gives us a USB file descriptor via Android's USB Host API.
We pass that fd to `libusb_wrap_sys_device()` to get a handle — same trick PrinterShare
uses internally, but open source.

## Requirements

- Rooted Android phone (any model — doesn't need usblp)
- Termux + Termux:API installed (from F-Droid recommended)
- Canon LBP2900 or LBP2900B printer
- USB OTG cable
- Phone and printing devices on same WiFi network

## Installation

```bash
# 1. Clone this repo in Termux
git clone https://github.com/YOUR_USERNAME/canon-termux-print
cd canon-termux-print

# 2. Run setup
chmod +x setup.sh
./setup.sh
```

Setup will:
- Install all dependencies (ghostscript, cups, python, libusb, clang)
- Clone and compile mounaiban/captdriver for ARM
- Install rastertocapt filter
- Configure CUPS
- Set up autostart via Termux:Boot

## Usage

```bash
# Start the server
~/canon-print-server/start.sh
```

Then add the printer on other devices:

**Windows:**
1. Control Panel → Devices and Printers → Add a Printer
2. "The printer I want isn't listed"
3. "Select a shared printer by name"
4. Enter: `http://<phone-ip>:631/printers/LBP2900`

**Linux:**
```bash
lpadmin -p LBP2900 -v ipp://<phone-ip>:631/printers/LBP2900 -E -m everywhere
```

**Android (NokoPrint):**
- Network → IPP → `<phone-ip>:631/printers/LBP2900`

**Android (PrinterShare):**
- Network → enter IP and port 631

## Supported formats

- PDF (recommended)
- PostScript

## Troubleshooting

**Printer not found on startup:**
```bash
ls /dev/bus/usb/  # Should show device files
```

**rastertocapt not found:**
```bash
# Re-run setup or manually:
ls ~/canon-print-server/captdriver/src/rastertocapt
cp ~/canon-print-server/captdriver/src/rastertocapt ~/../usr/lib/cups/filter/
```

**Job stuck / not printing:**
```bash
tail -f ~/canon-print-server/server.log
```
Try power cycling the printer.

**Port 631 permission denied:**
The server automatically falls back to port 6310. Use `<ip>:6310` in that case,
or run as root: `su -c "python ~/canon-print-server/print_server.py"`.

## Credits

- [mounaiban/captdriver](https://github.com/mounaiban/captdriver) — open source Canon CAPT driver
- [Querela/termux-usb-python](https://github.com/Querela/termux-usb-python) — termux USB fd technique
- [itapplication/Canon-LBP2900B](https://github.com/itapplication/Canon-LBP2900B) — LBP2900B specific driver

## License

MIT — free for everyone.
