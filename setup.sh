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
# License: MIT
# =============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="$HOME/canon-print-server"
mkdir -p "$INSTALL_DIR"
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
    warn "termux-usb not found. Installing Termux:API package..."
    pkg install termux-api -y
    echo ""
    warn "ACTION REQUIRED: Install the 'Termux:API' companion app from F-Droid or Play Store."
    warn "Then re-run this script."
    exit 1
fi

# Detect Canon printer (VendorID 04a9) specifically — skip root hub 001/001
log "Checking USB device..."
USB_DEVICE=""
for dev in /dev/bus/usb/*/*; do
    [ -e "$dev" ] || continue
    bus=$(echo "$dev" | awk -F/ '{print $5}')
    vendor=$(cat /sys/bus/usb/devices/usb${bus}/${bus}-*/idVendor 2>/dev/null | head -1)
    if [ "$vendor" = "04a9" ]; then
        USB_DEVICE="$dev"
        break
    fi
done
# Fallback: first device that isn't the root hub (001/001)
if [ -z "$USB_DEVICE" ]; then
    USB_DEVICE=$(ls /dev/bus/usb/*/* 2>/dev/null | grep -v '/001$' | head -1)
fi
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
if ! command -v gcc &>/dev/null; then
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
PPD_DEST="$PREFIX/share/cups/model/Canon-LBP2900.ppd"
if [ -f "ppd/CanonLBP-2900-3000.ppd" ]; then
    cp ppd/CanonLBP-2900-3000.ppd "$PPD_DEST"
elif [ -f "Canon-LBP2900.ppd" ]; then
    cp Canon-LBP2900.ppd "$PPD_DEST"
else
    warn "PPD not found in expected location, searching..."
    PPD=$(find . -name "*.ppd" | grep -i "2900\|lbp" | head -1)
    if [ -n "$PPD" ]; then
        cp "$PPD" "$PPD_DEST"
        info "Used PPD: $PPD"
    else
        warn "No LBP2900 PPD found — copying all available PPDs."
        cp ppd/*.ppd "$PREFIX/share/cups/model/" 2>/dev/null || true
    fi
fi
# Ensure lowercase alias exists (CUPS 2.4+ lowercases model names on lookup)
if [ -f "$PPD_DEST" ]; then
    cp "$PPD_DEST" "$PREFIX/share/cups/model/canon-lbp2900.ppd"
fi

cd "$INSTALL_DIR"

# --- Install the print server script ------------------------------------------

log "Installing print_server.py..."
chmod +x "$INSTALL_DIR/print_server.py"

# --- Configure CUPS -----------------------------------------------------------

log "Configuring CUPS..."
CUPS_CONF="$PREFIX/etc/cups/cupsd.conf"
CUPS_FILES_CONF="$PREFIX/etc/cups/cups-files.conf"

# Backup originals
cp "$CUPS_CONF" "${CUPS_CONF}.bak" 2>/dev/null || true
cp "$CUPS_FILES_CONF" "${CUPS_FILES_CONF}.bak" 2>/dev/null || true

# Fix cups-files.conf: remove 'sys' group which doesn't exist on Android
if grep -q "SystemGroup" "$CUPS_FILES_CONF" 2>/dev/null; then
    sed -i 's/^SystemGroup.*/SystemGroup root/' "$CUPS_FILES_CONF"
    info "Fixed SystemGroup in cups-files.conf"
fi

# Ensure CUPS run directory exists
mkdir -p "$PREFIX/var/run/cups"

cat > $PREFIX/etc/cups/cupsd.conf << EOF
LogLevel warn
MaxLogSize 0
ErrorPolicy retry-job

Listen 0.0.0.0:6310
Listen $PREFIX/var/run/cups/cups.sock

Browsing No

DefaultAuthType Basic
WebInterface Yes

<Location />
  Order allow,deny
  Allow all
</Location>

<Location /admin>
  Order allow,deny
  Allow all
</Location>
EOF

# Validate CUPS config before proceeding
if ! cupsd -t 2>&1 | grep -q "OK"; then
    warn "cupsd config check had warnings — see above. Continuing anyway."
fi

# --- Create run_server.sh wrapper (required for termux-usb -e) ---------------
#
# termux-usb -e expects a path to a single executable, not a shell string.
# Passing "python script.py" inline causes Android's intent system to mangle
# the command and TERMUX_USB_FD never gets set in the child process.
# This wrapper is the clean fix.

log "Creating USB wrapper script..."
cat > "$INSTALL_DIR/run_server.sh" << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Launched directly by termux-usb -e — do not run manually.
# termux-usb sets TERMUX_USB_FD and TERMUX_USB_DEVICE automatically.
exec python "$HOME/canon-print-server/print_server.py"
EOF
chmod +x "$INSTALL_DIR/run_server.sh"

# --- Create start.sh ----------------------------------------------------------

log "Creating start.sh..."
cat > "$INSTALL_DIR/start.sh" << 'LAUNCHER'
#!/data/data/com.termux/files/usr/bin/bash
# =============================================================
# Canon LBP2900B print server launcher
# =============================================================

INSTALL_DIR="$HOME/canon-print-server"
LOG="$INSTALL_DIR/server.log"

log()  { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG"; }
err()  { echo "[$(date '+%H:%M:%S')] ERROR: $1" | tee -a "$LOG"; exit 1; }

echo "" | tee -a "$LOG"
log "========================================="
log "  Starting Canon LBP2900B print server"
log "========================================="

# --- Find Canon printer (VendorID 04a9), skip root hub -----------------------
USB_DEVICE=""
for dev in /dev/bus/usb/*/*; do
    [ -e "$dev" ] || continue
    bus=$(echo "$dev" | awk -F/ '{print $5}')
    vendor=$(cat /sys/bus/usb/devices/usb${bus}/${bus}-*/idVendor 2>/dev/null | head -1)
    if [ "$vendor" = "04a9" ]; then
        USB_DEVICE="$dev"
        break
    fi
done
# Fallback: first non-root-hub device
if [ -z "$USB_DEVICE" ]; then
    USB_DEVICE=$(ls /dev/bus/usb/*/* 2>/dev/null | grep -v '/001$' | head -1)
fi
if [ -z "$USB_DEVICE" ]; then
    err "Printer not found. Is the OTG cable connected?"
fi
log "Using USB device: $USB_DEVICE"

WIFI_IP=$(ip addr 2>/dev/null | awk '/inet / && !/127./ {print $2}' | cut -d/ -f1 | head -1)
log "WiFi IP: ${WIFI_IP:-unknown}"

# --- Start CUPS ---------------------------------------------------------------
log "Starting CUPS..."
mkdir -p "$PREFIX/var/run/cups"

# Kill any stale cupsd
pkill cupsd 2>/dev/null || true
sleep 1

cupsd
sleep 3

# Confirm CUPS is responding
if ! lpstat -H &>/dev/null; then
    err "CUPS failed to start. Run 'cupsd -f' to see errors."
fi
log "CUPS is running."

# --- Register printer with CUPS (driverless — rendering done by print_server.py)
if ! lpstat -p LBP2900 &>/dev/null; then
    log "Registering LBP2900 with CUPS..."
    lpadmin -p LBP2900 \
        -v "ipp://localhost:631/printers/LBP2900" \
        -m everywhere \
        -E 2>&1 | tee -a "$LOG"
    lpadmin -d LBP2900     2>&1 | tee -a "$LOG"
    cupsaccept LBP2900     2>&1 | tee -a "$LOG"
    cupsenable LBP2900     2>&1 | tee -a "$LOG"
    log "Printer registered."
else
    log "LBP2900 already registered with CUPS."
fi
lpstat -p LBP2900 | tee -a "$LOG"

# --- Request USB permission ---------------------------------------------------
log "Requesting USB permission for $USB_DEVICE..."
log "(Watch for the Android permission dialog and tap Allow)"
termux-usb -r "$USB_DEVICE"
sleep 2

# --- Launch IPP server via termux-usb -----------------------------------------
log "Starting IPP server on port 631..."
log "Printer URI: ipp://${WIFI_IP}:631/printers/LBP2900"
log ""
log "Add printer on other devices:"
log "  Windows : http://${WIFI_IP}:631/printers/LBP2900"
log "  Linux   : lpadmin -p LBP2900 -v ipp://${WIFI_IP}:631/printers/LBP2900 -E"
log "  Android : NokoPrint > Network > IPP > ${WIFI_IP}:631/printers/LBP2900"
log ""

# termux-usb -e requires a path to a single executable (not a shell string).
# run_server.sh is that wrapper — it just calls: exec python print_server.py
# TERMUX_USB_FD and TERMUX_USB_DEVICE are injected automatically by termux-usb.
termux-usb -e "$INSTALL_DIR/run_server.sh" "$USB_DEVICE" 2>&1 | tee -a "$LOG"

log "IPP server exited. Check $LOG for details."
LAUNCHER

chmod +x "$INSTALL_DIR/start.sh"

# --- Termux:Boot autostart ----------------------------------------------------

log "Setting up autostart (requires Termux:Boot app)..."
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/canon-print.sh" << 'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
sleep 15  # wait for WiFi to connect
$HOME/canon-print-server/start.sh
BOOT
chmod +x "$HOME/.termux/boot/canon-print.sh"

# --- Done ---------------------------------------------------------------------

WIFI_IP=$(ip addr 2>/dev/null | awk '/inet / && !/127./ {print $2}' | cut -d/ -f1 | head -1)

echo ""
echo "============================================="
echo -e "${GREEN}  Setup Complete!${NC}"
echo "============================================="
echo ""
echo "  To start the server:"
echo "    ~/canon-print-server/start.sh"
echo ""
echo "  Printer address (once server is running):"
echo -e "  ${BLUE}ipp://${WIFI_IP}:631/printers/LBP2900${NC}"
echo ""
echo "  Add printer on other devices:"
echo "  - Windows : http://${WIFI_IP}:631/printers/LBP2900"
echo "  - Linux   : lpadmin -p LBP2900 -v ipp://${WIFI_IP}:631/printers/LBP2900 -E"
echo "  - Android : NokoPrint > Network > IPP > ${WIFI_IP}:631"
echo ""
echo "  Logs:"
echo "    Setup : ~/canon-print-server/setup.log"
echo "    Server: ~/canon-print-server/server.log"
echo "============================================="
