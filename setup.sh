#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# Canon LBP2900B WiFi Print Server for Termux (Rooted Android)
# =============================================================================
# Turns a rooted Android phone into a wireless print server for the
# Canon LBP2900/2900B without needing the usblp kernel module.
#
# How it works:
#   - termux-usb opens the printer via Android USB Host API (no usblp needed)
#   - libusb + pyusb talk to the printer through that file descriptor
#   - captdriver (mounaiban fork) renders documents to CAPT bitmap format
#   - A minimal IPP server accepts print jobs from any device on your WiFi
#
# Supports printing from: Windows, Linux, macOS, Android (via any print app)
#
# GitHub: [your repo here]
# License: MIT
# =============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="$HOME/canon-print-server"
LOG="$INSTALL_DIR/setup.log"

log()  { echo -e "${GREEN}[+]${NC} $1" | tee -a "$LOG"; }
warn() { echo -e "${YELLOW}[!]${NC} $1" | tee -a "$LOG"; }
err()  { echo -e "${RED}[✗]${NC} $1" | tee -a "$LOG"; exit 1; }
info() { echo -e "${BLUE}[i]${NC} $1" | tee -a "$LOG"; }

echo "============================================="
echo "  Canon LBP2900B Termux WiFi Print Server"
echo "============================================="
echo ""

# --- Preflight checks ---------------------------------------------------------

log "Checking root access..."
if ! su -c "id" 2>/dev/null | grep -q "uid=0"; then
    err "Root access required. Make sure your phone is rooted and Termux has root permission."
fi

log "Checking termux-usb..."
if ! command -v termux-usb &>/dev/null; then
    warn "termux-usb not found. Installing Termux:API..."
    pkg install termux-api -y
    echo ""
    warn "ACTION REQUIRED: Install the 'Termux:API' app from F-Droid or Play Store"
    warn "Then re-run this script."
    exit 1
fi

log "Checking USB device..."
USB_DEVICE=$(ls /dev/bus/usb/*/*  2>/dev/null | head -1)
if [ -z "$USB_DEVICE" ]; then
    err "No USB device found. Connect the printer via OTG cable and try again."
fi
info "Found USB device: $USB_DEVICE"

# --- Install packages ---------------------------------------------------------

log "Updating package lists..."
pkg update -y 2>&1 | tail -5

log "Installing build tools and dependencies..."
pkg install -y \
    python \
    libusb \
    clang \
    make \
    autoconf \
    automake \
    libtool \
    git \
    ghostscript \
    cups \
    termux-api \
    2>&1 | tail -10

log "Installing Python packages..."
pip install --quiet pyusb usb1 pillow 2>&1 | tail -5

# --- Build captdriver ---------------------------------------------------------

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

log "Cloning captdriver (mounaiban fork - LBP2900B compatible)..."
if [ -d "captdriver" ]; then
    warn "captdriver already cloned, pulling latest..."
    cd captdriver && git pull && cd ..
else
    git clone https://github.com/mounaiban/captdriver.git
fi

log "Building captdriver (this may take a few minutes)..."
cd captdriver

# Termux clang workaround — autoconf expects gcc
if [ ! -f "$(which gcc 2>/dev/null)" ]; then
    ln -sf "$(which clang)" "$PREFIX/bin/gcc" 2>/dev/null || true
    ln -sf "$(which clang++)" "$PREFIX/bin/g++" 2>/dev/null || true
fi

aclocal 2>&1 | tail -3
autoconf 2>&1 | tail -3
automake --add-missing 2>&1 | tail -3
./configure --prefix="$PREFIX" 2>&1 | tail -5
make -j$(nproc) 2>&1 | tail -10

log "Installing rastertocapt filter..."
cp src/rastertocapt "$PREFIX/lib/cups/filter/rastertocapt"
chmod 755 "$PREFIX/lib/cups/filter/rastertocapt"

log "Installing PPD file..."
mkdir -p "$PREFIX/share/cups/model"
# LBP2900/2900B PPD
if [ -f "ppd/CanonLBP-2900-3000.ppd" ]; then
    cp ppd/CanonLBP-2900-3000.ppd "$PREFIX/share/cups/model/Canon-LBP2900.ppd"
elif [ -f "Canon-LBP2900.ppd" ]; then
    cp Canon-LBP2900.ppd "$PREFIX/share/cups/model/Canon-LBP2900.ppd"
else
    warn "PPD not found in expected location, searching..."
    PPD=$(find . -name "*.ppd" | grep -i "2900\|lbp" | head -1)
    if [ -n "$PPD" ]; then
        cp "$PPD" "$PREFIX/share/cups/model/Canon-LBP2900.ppd"
        info "Used PPD: $PPD"
    else
        warn "No LBP2900 PPD found. Will use generic CAPT PPD."
        cp ppd/*.ppd "$PREFIX/share/cups/model/" 2>/dev/null || true
    fi
fi

cd "$INSTALL_DIR"

# --- Install the print server script ------------------------------------------

log "Installing print server..."
cp "$(dirname "$0")/print_server.py" "$INSTALL_DIR/print_server.py"
chmod +x "$INSTALL_DIR/print_server.py"

# --- Configure CUPS -----------------------------------------------------------

log "Configuring CUPS..."
CUPS_CONF="$PREFIX/etc/cups/cupsd.conf"

# Backup original
cp "$CUPS_CONF" "${CUPS_CONF}.bak" 2>/dev/null || true

cat > "$CUPS_CONF" << 'EOF'
LogLevel warn
PageLogFormat
MaxLogSize 0
ErrorPolicy retry-job

Listen 0.0.0.0:631
Listen /data/data/com.termux/files/usr/var/run/cups/cups.sock

Browsing Yes
BrowseLocalProtocols dnssd
BrowseWebIF Yes

DefaultAuthType Basic
WebInterface Yes

<Location />
  Order allow,deny
  Allow from @LOCAL
  Allow from localhost
</Location>

<Location /admin>
  Order allow,deny
  Allow from @LOCAL
  Allow from localhost
</Location>

<Location /admin/conf>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow from @LOCAL
  Allow from localhost
</Location>
EOF

# --- Create the launcher script -----------------------------------------------

cat > "$INSTALL_DIR/start.sh" << 'LAUNCHER'
#!/data/data/com.termux/files/usr/bin/bash
# Start the Canon LBP2900B print server

INSTALL_DIR="$HOME/canon-print-server"
LOG="$INSTALL_DIR/server.log"

echo "[$(date)] Starting Canon print server..." | tee -a "$LOG"

# Find printer USB device
# Find Canon printer (VendorID 04a9) specifically, skip root hub (001/001)
USB_DEVICE=""
for dev in /dev/bus/usb/*/*; do
    # Read idVendor from sysfs
    bus=$(echo "$dev" | awk -F/ '{print $5}')
    devnum=$(echo "$dev" | awk -F/ '{print $6}')
    vendor=$(cat /sys/bus/usb/devices/usb${bus}/${bus}-*/idVendor 2>/dev/null | head -1)
    if [ "$vendor" = "04a9" ]; then
        USB_DEVICE="$dev"
        break
    fi
done

# Fallback: take first non-001 device
if [ -z "$USB_DEVICE" ]; then
    USB_DEVICE=$(ls /dev/bus/usb/*/* 2>/dev/null | grep -v '/001/001$' | head -1)
fi

if [ -z "$USB_DEVICE" ]; then
    echo "[ERROR] Printer not found. Is OTG cable connected?" | tee -a "$LOG"
    exit 1
fi

echo "[+] Printer device: $USB_DEVICE" | tee -a "$LOG"
echo "[+] WiFi IP: $(ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)" | tee -a "$LOG"

# Start CUPS in background
echo "[+] Starting CUPS..." | tee -a "$LOG"
cupsd 2>&1 | tee -a "$LOG" &
sleep 2

# Add printer to CUPS if not already added
if ! lpstat -p LBP2900 &>/dev/null; then
    echo "[+] Adding LBP2900 to CUPS..." | tee -a "$LOG"
    # Use termux-usb based backend
    lpadmin -p LBP2900 \
        -P "$PREFIX/share/cups/model/Canon-LBP2900.ppd" \
        -v "usb://Canon/LBP2900" \
        -E 2>&1 | tee -a "$LOG"
    lpadmin -d LBP2900 2>&1 | tee -a "$LOG"
    cupsaccept LBP2900 2>&1 | tee -a "$LOG"
    cupsenable LBP2900 2>&1 | tee -a "$LOG"
fi

# Start the IPP print server (handles USB via termux-usb)
echo "[+] Starting IPP server on port 631..." | tee -a "$LOG"
# NEW (permission first, then launch):
termux-usb -r "$USB_DEVICE"
sleep 2
termux-usb -e "python $INSTALL_DIR/print_server.py" "$USB_DEVICE" 2>&1 | tee -a "$LOG"
LAUNCHER

chmod +x "$INSTALL_DIR/start.sh"

# --- Termux:Boot autostart ----------------------------------------------------

log "Setting up autostart (requires Termux:Boot app)..."
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/canon-print.sh" << 'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
sleep 10  # wait for WiFi
$HOME/canon-print-server/start.sh
BOOT
chmod +x "$HOME/.termux/boot/canon-print.sh"

# --- Done ---------------------------------------------------------------------

WIFI_IP=$(ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)

echo ""
echo "============================================="
echo -e "${GREEN}  Setup Complete!${NC}"
echo "============================================="
echo ""
echo "  Printer server address:"
echo -e "  ${BLUE}http://${WIFI_IP}:631/printers/LBP2900${NC}"
echo ""
echo "  To start the server:"
echo "    ~/canon-print-server/start.sh"
echo ""
echo "  Add printer on other devices:"
echo "  - Windows: Devices & Printers > Add > Network > enter URL above"
echo "  - Linux:   lpadmin -p LBP2900 -v ipp://${WIFI_IP}:631/printers/LBP2900 -E"
echo "  - Android: NokoPrint or PrinterShare > Network > enter IP"
echo ""
echo "  Logs: ~/canon-print-server/server.log"
echo "============================================="
