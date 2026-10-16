# LilyGo T-Display S3 (CircuitPython 10.3.1)
# 1. Connect to WiFi  2. Show Singapore time  3. Show 24-hr PSI and 1-hr PM2.5 (data.gov.sg)
# Uses only modules built into the firmware (no /lib files needed).

import json
import os
import struct
import time

import board
import displayio
import keypad
import rtc
import socketpool
import ssl
import terminalio
import wifi

TZ_OFFSET_S = 8 * 3600       # Singapore, UTC+8, no daylight saving
NTP_SERVER = os.getenv("NTP_SERVER") or "pool.ntp.org"
NTP_RESYNC_S = 3600          # re-sync the clock hourly
DIM_AFTER_S = 10            # dim the backlight after this many seconds
DIM_BRIGHTNESS = 0.1        # 10%
DATA_REFRESH_S = 600         # PSI / PM2.5 are published hourly; poll every 10 min

# ---------- Display (board.DISPLAY is built in: 320x170 ST7789) ----------
display = board.DISPLAY
root = displayio.Group()
display.root_group = root

FONT = terminalio.FONT
GLYPH_W, GLYPH_H = FONT.get_bounding_box()
SPACE = FONT.get_glyph(ord(" ")).tile_index


class TextLine:
    """One line of fixed-width text drawn from terminalio.FONT tiles."""

    def __init__(self, chars, x, y, scale, color):
        palette = displayio.Palette(2)
        palette.make_transparent(0)
        palette[1] = color
        self.palette = palette
        self.chars = chars
        self.grid = displayio.TileGrid(FONT.bitmap, pixel_shader=palette,
                                       width=chars, height=1,
                                       tile_width=GLYPH_W, tile_height=GLYPH_H)
        group = displayio.Group(scale=scale, x=x, y=y)
        group.append(self.grid)
        root.append(group)
        self.text = ""

    def set(self, text):
        if text == self.text:
            return
        self.text = text
        for i in range(self.chars):
            glyph = FONT.get_glyph(ord(text[i])) if i < len(text) else None
            self.grid[i] = glyph.tile_index if glyph else SPACE

    def set_color(self, color):
        self.palette[1] = color


def hline(y, color=0x555555):
    """2 px divider line across the screen: a 1-colour Bitmap in a TileGrid."""
    bitmap = displayio.Bitmap(display.width - 8, 2, 1)  # every pixel = palette[0]
    palette = displayio.Palette(1)
    palette[0] = color
    root.append(displayio.TileGrid(bitmap, pixel_shader=palette, x=4, y=y))


# 26 columns at scale 2 (12 px per glyph) fill the 320 px width
TOP = 2                       # top margin above the first text row
ROW_Y = (TOP, TOP + 36, TOP + 72)  # header, PSI, PM2.5
ROW_H = GLYPH_H * 2           # text height at scale 2
for top, below in ((ROW_Y[0], ROW_Y[1]), (ROW_Y[1], ROW_Y[2])):
    hline((top + ROW_H + below) // 2 - 1)  # centred in the gap between rows

time_line = TextLine(7, 4, ROW_Y[0], 2, 0xFFFFFF)  # HH:MM in the header row's label column
hdr_line = TextLine(26, 4, ROW_Y[0], 2, 0xFFCC00)
psi_line = TextLine(26, 4, ROW_Y[1], 2, 0xFFFFFF)
pm25_line = TextLine(26, 4, ROW_Y[2], 2, 0xFFFFFF)
extra_line = TextLine(26, 4, TOP + 108, 2, 0xFF6666)  # overflow row for long error text

# One 4-char cell per region after the 6-char label, each with its own colour
CELL_X = [4 + (6 + 4 * i) * GLYPH_W * 2 for i in range(5)]
psi_cells = [TextLine(4, x, ROW_Y[1], 2, 0xFFFFFF) for x in CELL_X]
pm25_cells = [TextLine(4, x, ROW_Y[2], 2, 0xFFFFFF) for x in CELL_X]

# Colours per health band: (upper limit inclusive, colour)
GREEN, BLUE, YELLOW, ORANGE, RED = 0x00DD44, 0x3399FF, 0xFFDD00, 0xFF8800, 0xFF2222
# NEA 24-hr PSI bands (https://www.haze.gov.sg/): Good 0-50, Moderate 51-100,
# Unhealthy 101-200, Very unhealthy 201-300, Hazardous >300
PSI_BANDS = ((50, GREEN), (100, BLUE), (200, YELLOW), (300, ORANGE), (None, RED))
# NEA 1-hr PM2.5 bands (ug/m3): 1 Normal 0-55, 2 Elevated 56-150,
# 3 High 151-250, 4 Very High >=251. No orange for PM2.5.
PM25_BANDS = ((55, GREEN), (150, BLUE), (250, YELLOW), (None, RED))


# Mini legend: 15x15 px squares, no text, bottom of screen, starting at the
# same left margin (x=4) as the PSI / PM2.5 labels
LEGEND_SIZE, LEGEND_GAP = 15, 3
LEGEND_X, LEGEND_Y = 4, display.height - LEGEND_SIZE - 4
for i, color in enumerate((GREEN, BLUE, YELLOW, ORANGE, RED)):
    square = displayio.Bitmap(LEGEND_SIZE, LEGEND_SIZE, 1)  # every pixel = palette[0]
    square_palette = displayio.Palette(1)
    square_palette[0] = color
    root.append(displayio.TileGrid(square, pixel_shader=square_palette,
                                   x=LEGEND_X + i * (LEGEND_SIZE + LEGEND_GAP),
                                   y=LEGEND_Y))


# Cat-hiding-indoors icons, drawn from text pixel art (no image file needed).
# '.' = transparent; every other letter is a colour in ICON_COLORS.
# Orange icon: cat in house
ICON_ORANGE_ART = (
    "................RRRR................",
    "..............RRRRRRRR..KKK.........",
    "............RRRRRRRRRRRRCCC.........",
    "..........RRRRRRRRRRRRRRCCC.........",
    "........RRRRRRRRRRRRRRRRRRRR........",
    "......RRRRRRRRRRRRRRRRRRRRRRRR......",
    "....RRRRRRRRRRRRRRRRRRRRRRRRRRRR....",
    "......WWWWWWWWWWWWWWWWWWWWWWWW......",
    "......WWWWWWWWWWWWWWWWWWWWWWWW......",
    "......WWWFFFFFFFFFFFFWWWWWWWWW......",
    "......WWWFBOBBBBBBOBFWWWWWWWWW......",
    "......WWWFBOOBBBBOOBFWWWWWWWWW......",
    "......WWWFBOOOOOOOOBFWWDDDDDWW......",
    "......WWWFBOOGOOGOOBFWWDDDDDWW......",
    "......WWWFBOOOPPOOOBFWWDDDDDWW......",
    "......WWWFBOOOOOOOOBFWWDDDDDWW......",
    "......WWWFBBOOOOOOBBFWWDDDYDWW......",
    "......WWWFFFFFFFFFFFFWWDDDDDWW......",
    "......WWWWWWWWWWWWWWWWWDDDDDWW......",
    "......WWWWWWWWWWWWWWWWWDDDDDWW......",
    "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
)
# Red icon: same, cat wears a white mask, grey cloud over roof and door
ICON_RED_ART = (
    "................RRRR................",
    "..............RRRRRRRR..KKK.........",
    "............RRRRRRRRRRRRCCC.........",
    "..........RRRRRRRRRRRRRRCCC.........",
    "........RRRRRRRRRRRRRRRRRRRR........",
    "......RRRRRRRRRRRRRRRRRRRSSSSR......",
    "....RRRRRRRRRRRRRRRRRRRSSSSSSSSSSS..",
    "......WWWWWWWWWWWWWWWWSSSSSSSSSSSSS.",
    "......WWWWWWWWWWWWWWWSSSSSSSSSSSSSS.",
    "......WWWFFFFFFFFFFFFSSSSSSSSSSSSSS.",
    "......WWWFBOBBBBBBOBFWSSSSSSSSSSSS..",
    "......WWWFBOOBBBBOOBFWWSSSSSSSSSSS..",
    "......WWWFBOOOOOOOOBFWWDSSSSSSSSS...",
    "......WWWFBOOGOOGOOBFWWDDSSSWWSSS...",
    "......WWWFBLMMMMMMLBFWWDDDDDWW......",
    "......WWWFBOMMMMMMOBFWWDDDDDWW......",
    "......WWWFBBOMMMMOBBFWWDDDYDWW......",
    "......WWWFFFFFFFFFFFFWWDDDDDWW......",
    "......WWWWWWWWWWWWWWWWWDDDDDWW......",
    "......WWWWWWWWWWWWWWWWWDDDDDWW......",
    "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
)
# Yellow icon: cat sitting down, wearing a white mask, grey cloud behind it (upper right)
ICON_YELLOW_ART = (
    ".............O........O....SSSS.....",
    ".............OO......OO...SSSSSS....",
    ".............OOOOOOOOOO..SSSSSSSS...",
    "............OOOOOOOOOOOOSSSSSSSSSSS.",
    "............OOGGOOOOGGOOSSSSSSSSSSSS",
    "............OOGKOOOOGKOOSSSSSSSSSSSS",
    "............OLMMMMMMMMLOSSSSSSSSSSS.",
    "............OOMMMMMMMMOOSSSSSSSSSS..",
    ".............OOMMMMMMOO...SSSSS.....",
    ".............OOOOOOOOOO.............",
    "............OOOOTTTTOOOO............",
    "...........OOOOTTTTTTOOOO...........",
    "...........OOOTTTTTTTTOOO.......OO..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO......OO..",
    "..........OOOOOTTTTTTOOOOO.....OO...",
    "..........OOOTTOOOOOOTTOOO...OOO....",
    "...........OOTTOOOOOOTTOOOOOOO......",
    "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
)
# Blue icon: same sitting cat, no mask
ICON_BLUE_ART = (
    ".............O........O.............",
    ".............OO......OO.............",
    ".............OOOOOOOOOO.............",
    "............OOOOOOOOOOOO............",
    "............OOGGOOOOGGOO............",
    "............OOGKOOOOGKOO............",
    "............OOOOOPPOOOOO............",
    "............OOOOOOOOOOOO............",
    ".............OOOOOOOOOO.............",
    ".............OOOOOOOOOO.............",
    "............OOOOTTTTOOOO............",
    "...........OOOOTTTTTTOOOO...........",
    "...........OOOTTTTTTTTOOO.......OO..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO.......O..",
    "..........OOOOTTTTTTTTOOOO......OO..",
    "..........OOOOOTTTTTTOOOOO.....OO...",
    "..........OOOTTOOOOOOTTOOO...OOO....",
    "...........OOTTOOOOOOTTOOOOOOO......",
    "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
)
# Green icon: same cat running, no mask (side view)
ICON_GREEN_ART = (
    "....................................",
    "....................................",
    "..........................O...O.....",
    "..........................OO.OO.....",
    ".OO.......................OOOOOO....",
    "..OO.....................OOOOOOOO...",
    "...OO....................OOOGKOOO...",
    "....OO...................OOOOOOOOO..",
    ".....OO.....OOOOOOOOOOOOOOOOOOOOPO..",
    "......OO..OOOOOOOOOOOOOOOOOOOOOOO...",
    ".......OOOOOOOOOOOOOOOOOOOOOOOOO....",
    "........OOOOOOOOOOOOOOOOOOOOOOO.....",
    "........OOOOOOTTTTTTTTTTTOOOOOO.....",
    ".......OOOOO..TTTTTTTTTT...OOOOO....",
    "......OOOO..................OOOOO...",
    ".....OOO.....................OOOO...",
    "....OOO........................OOO..",
    "...OOO..........................OOO.",
    "..TTT............................TTT",
    "....................................",
    "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
)
ICON_COLORS = {
    "R": 0xE53935,  # roof
    "C": 0xB71C1C,  # chimney
    "K": 0x424242,  # chimney top
    "W": 0xFFE082,  # walls
    "F": 0xFFFFFF,  # window frame
    "B": 0x64B5F6,  # window glass
    "O": 0xFF9800,  # cat
    "G": 0x43A047,  # cat eyes / grass
    "P": 0xF48FB1,  # cat nose
    "D": 0x8D6E63,  # door
    "Y": 0xFFEB3B,  # door knob
    "M": 0xFFFFFF,  # mask
    "L": 0x9E9E9E,  # mask ear loops
    "S": 0x9E9E9E,  # cloud
    "T": 0xFFE0B2,  # cat belly / paws
}


def make_icon(art):
    """Fit an icon into the space below the PM2.5 row and right of the legend."""
    keys = list(ICON_COLORS)
    art_w, art_h = len(art[0]), len(art)
    bitmap = displayio.Bitmap(art_w, art_h, len(keys) + 1)
    palette = displayio.Palette(len(keys) + 1)
    palette.make_transparent(0)
    for i, k in enumerate(keys):
        palette[i + 1] = ICON_COLORS[k]
    for y, line in enumerate(art):
        for x, ch in enumerate(line):
            if ch != ".":
                bitmap[x, y] = keys.index(ch) + 1
    left = LEGEND_X + 5 * (LEGEND_SIZE + LEGEND_GAP) + 5
    top = ROW_Y[2] + ROW_H + 4
    avail_w, avail_h = display.width - 4 - left, display.height - 2 - top
    scale = max(1, min(avail_w // art_w, avail_h // art_h))
    group = displayio.Group(scale=scale,
                            x=left + (avail_w - art_w * scale) // 2,
                            y=top + avail_h - art_h * scale)
    group.append(displayio.TileGrid(bitmap, pixel_shader=palette))
    group.hidden = True
    root.append(group)
    return group


icons = {
    "green": make_icon(ICON_GREEN_ART),
    "blue": make_icon(ICON_BLUE_ART),
    "yellow": make_icon(ICON_YELLOW_ART),
    "orange": make_icon(ICON_ORANGE_ART),
    "red": make_icon(ICON_RED_ART),
}


def show_icon(level):
    """level: "green", "blue", "yellow", "orange", "red", or None to hide all icons."""
    for name, group in icons.items():
        group.hidden = name != level


def band_color(value, bands):
    for limit, color in bands:
        if limit is None or value <= limit:
            return color


time_line.set("--:----")
hdr_line.set("         N   S   E   W   C")


def status(msg):
    print(msg)
    pm25_line.set(msg)
    extra_line.set("")


def show_cells(cells, values, bands):
    for c, region in zip(cells, REGIONS):
        value = values.get(region)
        c.set(cell(value))
        c.set_color(0xFFFFFF if value is None else band_color(round(value), bands))


def clear_cells():
    for c in psi_cells + pm25_cells:
        c.set("")


def show_error(msg):
    """Spread a long error message over the three data rows."""
    print(msg)
    clear_cells()
    show_icon(None)
    psi_line.set(msg[0:26])
    pm25_line.set(msg[26:52])
    extra_line.set(msg[52:78])


# ---------- 1. WiFi ----------
status("Connecting WiFi...")
ssid = os.getenv("CIRCUITPY_WIFI_SSID")
password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
while not wifi.radio.connected:
    try:
        wifi.radio.connect(ssid, password)
    except Exception as e:  # ConnectionError / OSError
        status("WiFi error, retrying")
        print(e)
        time.sleep(5)
status("WiFi OK " + str(wifi.radio.ipv4_address))

pool = socketpool.SocketPool(wifi.radio)
ssl_context = ssl.create_default_context()
# Optional: trust one specific root CA (PEM) instead of the firmware's built-in bundle.
# SSLContext.load_verify_locations(cadata=...) accepts a single PEM certificate.
try:
    with open("/ca.pem") as f:
        ssl_context.load_verify_locations(cadata=f.read())
    print("Using /ca.pem as the trusted root CA")
except OSError:
    pass  # no /ca.pem: use the built-in certificate bundle

# ---------- 2. Time (SNTP over UDP -> on-board RTC) ----------
NTP_TO_UNIX = 2208988800  # seconds between 1900-01-01 and 1970-01-01


def sync_time():
    packet = bytearray(48)
    packet[0] = 0x1B  # LI=0, version 3, mode 3 (client)
    addr = pool.getaddrinfo(NTP_SERVER, 123)[0][4]
    sock = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)
    try:
        sock.settimeout(5)
        sock.sendto(packet, addr)
        sock.recvfrom_into(packet)
    finally:
        sock.close()
    ntp_seconds = struct.unpack_from("!I", packet, 40)[0]  # transmit timestamp
    rtc.RTC().datetime = time.localtime(ntp_seconds - NTP_TO_UNIX + TZ_OFFSET_S)


# ---------- 3. Public API over HTTPS (socketpool + ssl) ----------
# data.gov.sg real-time APIs (NEA). Docs: https://guide.data.gov.sg/
#   PSI:   https://data.gov.sg/datasets/d_fe37906a0182569d891506e815e819b7/view
#   PM2.5: https://data.gov.sg/datasets/d_e1058d6974c877257e32048ab128ad83/view
API_HOST = "api-open.data.gov.sg"
PSI_PATH = "/v2/real-time/api/psi"
PM25_PATH = "/v2/real-time/api/pm25"
REGIONS = ("north", "south", "east", "west", "central")


def dechunk(body):
    out = bytearray()
    i = 0
    while True:
        j = body.find(b"\r\n", i)
        size = int(bytes(body[i:j]).decode().split(";")[0].strip(), 16)
        if size == 0:
            return out
        out += body[j + 2:j + 2 + size]
        i = j + 2 + size + 2


def https_get_json(host, path):
    # No API key is needed. Same headers curl sends by default; note that curl's
    # -H 'x-api-key: ' (empty value) removes that header rather than sending it blank.
    request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: curl/8.5.0\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n" % (path, host))

    addr = pool.getaddrinfo(host, 443)[0][4]
    sock = ssl_context.wrap_socket(pool.socket(pool.AF_INET, pool.SOCK_STREAM),
                                   server_hostname=host)
    data = bytearray()
    buf = bytearray(1024)
    try:
        sock.settimeout(15)
        sock.connect(addr)
        sock.send(request.encode())
        while True:
            try:
                n = sock.recv_into(buf, len(buf))
            except OSError:
                if data:
                    break  # some servers end TLS abruptly after Connection: close
                raise
            if n == 0:
                break
            data += buf[:n]
    finally:
        sock.close()

    split = data.find(b"\r\n\r\n")
    head = bytes(data[:split]).decode().lower()
    status_code = int(head.split(" ", 2)[1])
    body = data[split + 4:]
    if "transfer-encoding: chunked" in head:
        body = dechunk(body)
    if status_code != 200:
        print("Response headers:\n" + head)
        print("Response body:", bytes(body[:300]))
        raise RuntimeError("HTTP %d %r" % (status_code, bytes(body[:60])))
    return json.loads(bytes(body).decode())


def get_readings(path, key):
    """Return the {region: value} dict for readings[key] in the latest item."""
    return https_get_json(API_HOST, path)["data"]["items"][0]["readings"][key]


def cell(value):
    """Right-align a reading in a 4-char cell so its last digit sits under the
    region letter: '   7', '  42', ' 175'. Missing values show as '-'."""
    if value is None:
        return "   -"
    return "%4d" % round(value)


def fetch_data():
    """Return the PSI and PM2.5 {region: value} dicts."""
    psi = get_readings(PSI_PATH, "psi_twenty_four_hourly")
    pm25 = get_readings(PM25_PATH, "pm25_one_hourly")
    return psi, pm25


# ---------- Backlight: dim after DIM_AFTER_S, Button 2 (board.BUTTON1, GPIO14) wakes ----------
# BUTTON0 is GPIO0 (boot). keypad scans the pin and queues presses; the button
# pulls the pin low when pressed.
buttons = keypad.Keys((board.BUTTON1,), value_when_pressed=False, pull=True)
FULL_BRIGHTNESS = 1.0
display.brightness = FULL_BRIGHTNESS
awake_since = time.monotonic()
dimmed = False


# ---------- Main loop ----------
last_sync = None
last_fetch = None
last_hhmm = None

while True:
    now = time.monotonic()

    if last_sync is None or now - last_sync >= NTP_RESYNC_S:
        try:
            sync_time()
            print("NTP sync OK")
            last_sync = now
            last_hhmm = None  # force a redraw
        except Exception as e:
            print("NTP sync failed:", e)
            last_sync = now - NTP_RESYNC_S + 60  # retry in a minute

    if last_fetch is None or now - last_fetch >= DATA_REFRESH_S:
        try:
            psi, pm25 = fetch_data()
            psi_line.set("PSI")
            pm25_line.set("PM2.5")
            extra_line.set("")
            show_cells(psi_cells, psi, PSI_BANDS)
            show_cells(pm25_cells, pm25, PM25_BANDS)
            # Icon follows the worst (highest) PSI across N/S/E/W/C only
            psi_values = [v for v in psi.values() if v is not None]
            level = None
            if psi_values:
                worst = band_color(round(max(psi_values)), PSI_BANDS)
                level = {GREEN: "green", BLUE: "blue", YELLOW: "yellow",
                         ORANGE: "orange", RED: "red"}[worst]
            show_icon(level)
            last_fetch = now
        except Exception as e:
            print("Fetch failed:", repr(e))
            show_error(repr(e))
            last_fetch = now - DATA_REFRESH_S + 60  # retry in a minute

    t = time.localtime()
    if (t.tm_hour, t.tm_min) != last_hhmm:
        last_hhmm = (t.tm_hour, t.tm_min)
        hour12 = t.tm_hour % 12 or 12
        ampm = "AM" if t.tm_hour < 12 else "PM"
        time_line.set("%02d:%02d%s" % (hour12, t.tm_min, ampm))

    event = buttons.events.get()
    if event and event.pressed:
        display.brightness = FULL_BRIGHTNESS
        dimmed = False
        awake_since = time.monotonic()
    elif not dimmed and time.monotonic() - awake_since >= DIM_AFTER_S:
        display.brightness = DIM_BRIGHTNESS
        dimmed = True

    # Short sleep so a button press is handled promptly. Nothing is redrawn
    # more often: the clock redraws once a minute and the data every 10 min.
    time.sleep(0.1)
