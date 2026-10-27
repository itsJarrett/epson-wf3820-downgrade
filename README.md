# epson-wf3820-downgrade

Install an older **official** Epson firmware on a WorkForce Pro **WF-3820** from
**Linux over USB**, in normal operating mode. You don't need recovery mode, Windows, or a paid key.

Tested on one WF-3820 (October 2026): the printer went from `04.65.SI22Q6` (July 2026)
to `04.51.SI09K9`, then to `04.52.SI13LC`. Other models that use the same update
protocol may work, but none have been tested.

> **Status:** method and `epsonfw.py` verified on hardware — repeated `info`/`extract`/`flash`
> cycles over USB on a real WF-3820 (SI22Q6 → SI09K9 → SI13LC and back). `netinfo` (SNMP) tested too.
> This is an **incremental** tool: USB `.rcx` flashing already exists in
> [`abrasive/epson-reversing`](https://github.com/abrasive/epson-reversing)'s `epsflasher`; the new
> bits here are the credit-handling fix, the version-skip behaviour, and a dependency-free rewrite.

> **Use at your own risk.** Flashing firmware can leave a printer unusable. Only use
> unmodified firmware published by Epson, for your model. This project does not
> distribute firmware.

## Why

From 2022 on, Epson firmware updates for the WF-3820 stopped some third-party and
remanufactured cartridges from working. The usual rollback guides need Epson's Windows
updater and a hidden touch-panel combination that boots the printer into recovery mode.
On current firmware that combination is unreliable; it never worked on the unit tested here.

## Findings

* **The printer accepts older firmware in normal mode.** The printer doesn't check the
  version when the `EPSON-CTRL` command `gm 1` (enter update mode) is sent over USB; Epson's
  Windows updater does that check itself. After `gm 1` the panel shows *"Preparing for firmware
  update"* and the printer waits for the image on the `EPSON-DATA` channel.
* **Send credit has to be requested in small amounts.** In IEEE 1284.4, the printer has to
  grant credit before the host can send each packet. The WF-3820 allows at most 4 credits
  outstanding and answers **0** to a request for 256 (`0x100`). The existing open-source
  flasher [`abrasive/epson-reversing`](https://github.com/abrasive/epson-reversing)
  (`epsflasher`) asks for 256, so it stalls until the printer times out and reboots.
  Asking for 1–4 at a time fixes it.
* **The printer skips a same-version write.** If the image's version equals the running
  version, the printer accepts the transfer but no-ops the write (finishes in ~1 min instead
  of ~5, and `vi` is unchanged) — it compares the firmware's own version, not the `.rcx`
  header. To reflash the *same* version, first flash a different version, then flash the one
  you want (e.g. SI09K9 → SI13LC). Downgrades and upgrades both write; only equal versions skip.
* **It doesn't work over the network.** `gm 1` is accepted over SNMP (Epson OID
  `1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1`), but the printer then leaves Wi-Fi. Use USB.
* **SI09K9 broke the scanner on the tested unit.** It caused error `E02 100016` ("Scanner
  error"; Web Config: "Scanner is out of order"). **SI13LC** fixed it. If you get 100016
  after a downgrade, try SI13LC.
* **The update server is `epsonpfu.ebz.epson.net`** (found in the SI13LC firmware).

## Firmware

Download the Windows updater from Epson. The tool reads the `.exe` directly; you don't run
it.

| Version | Date | File | SHA-256 |
|---|---|---|---|
| SI13LC | 2021-12 | [`FWCJ07TL_SI13LC.exe`](https://ftp.epson.com/drivers/FWCJ07TL_SI13LC.exe) (Epson) | `4dcc4efb7e494fee888cc090c8c120e10a2bf8cdc8cd8bb9199c9a026a19775d` |
| SI09K9 | 2020-09 | not on Epson's current servers — locate a copy and **verify the SHA-256** | `a84de3432101c20808dc5f28c4543f7e2e66a623cb9b9948e8ba211d0679ceb8` (`.exe` inside the zip) |

Which versions accept which third-party cartridges depends on the cartridge chip. On the
tested unit, SI13LC read the remanufactured black cartridge normally. Reports elsewhere
conflict.

## Usage

Requires Linux with the `usblp` driver (`/dev/usb/lp0`) and Python 3.8+. No other dependencies.

Give your user access to the printer's USB device (or use the `lp` group):

```sh
echo 'SUBSYSTEM=="usbmisc", ATTRS{idVendor}=="04b8", ATTRS{idProduct}=="117a", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-epson-flash.rules
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
```

Then:

```sh
./epsonfw.py info                        # firmware version, status, ink readings
./epsonfw.py flash FWCJ07TL_SI13LC.exe   # asks for confirmation, then flashes
```

The printer must be on, idle, and connected by USB; an ink error on screen is fine.
The image takes about 90 s to send. The printer then shows *"Updating Firmware... Do not turn
the power off"*, writes the image for about 4 minutes, and restarts by itself. The tool waits
and prints the new version. **Don't power the printer off during this.**

Other commands: `netinfo HOST` (version over SNMP), `extract FW.exe -o FW.rcx`.

Remove the udev rule when done: `sudo rm /etc/udev/rules.d/99-epson-flash.rules`.

## Keeping it from updating again

* Panel: **Settings → General Settings → Firmware Update → Notification: Off**.
* Don't install Epson Software Updater; it can update the printer over USB or the network.
* To block it on the network, block `epsonpfu.ebz.epson.net` in your DNS (e.g. Pi-hole), or set
  the printer's DNS to an address that doesn't answer (Web Config → Network → Basic →
  DNS: Manual, e.g. `192.0.2.1`). Local printing and scanning still work; Epson Connect
  and cloud features stop.

## Protocol notes

* USB: IEEE 1284.4 (Dot4), entered with `ESC 01 @EJL 1284.4`. Services `EPSON-CTRL`
  (commands) and `EPSON-DATA` (image).
* `EPSON-CTRL` commands are two ASCII letters, a u16le length, and arguments.
  `vi 04` → version; `st 01` → status (`@BDC ST2`); `fl 01`, `gm 01` → enter update mode;
  `gm 03` → finish (`fl:03:OK`).
* Image: the `.exe` is a zip containing `FWCJ07TL.efu`, itself a zip containing `FWCJ07TL.rcx`.
  The `.rcx` has an INI-style header ending in `0x0C`; send only the body after the header.

## Credits & legal

Protocol knowledge builds on [abrasive/epson-reversing](https://github.com/abrasive/epson-reversing).
This repository contains no code from it and no Epson software. All code here is an independent
implementation for interoperability with a device you own. "Epson" and "WorkForce" are trademarks of
Seiko Epson Corporation; this project is not affiliated with Epson.

This tool installs **only official, unmodified, Epson-signed firmware** using Epson's own
update commands; it changes which version is installed and performs **no circumvention** of
any technological protection measure, signing check, or cartridge/DRM mechanism. It
redistributes no firmware (download links and SHA-256 only). Intended for repair and
interoperability of hardware you own.

License: MIT.
