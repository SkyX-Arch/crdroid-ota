# Installation Guide

> [!WARNING]
> You are responsible for your own actions and device. Neither the ROM developer nor the author of this guide is responsible for any damage, data loss, or other issues that may occur during the installation process.

## Requirements

Before starting, download the following files:

* `preloader_plato.bin`
* Latest crDroid ROM package
* GApps package (optional)
* Latest Android Platform Tools (ADB/Fastboot)

Additionally:

* The bootloader **must be unlocked**.
* The engineering firmware preloader **must be flashed before installing the ROM**.

> [!IMPORTANT]
> Flashing the engineering firmware preloader is strongly recommended. It provides an additional recovery path and may allow restoring stock firmware without requiring service center assistance if something goes wrong.

---

## Step 1: Flash the Engineering Firmware Preloader

Boot the device into Fastboot mode by holding **Volume Down + Power** until the **FASTBOOT** screen appears.

Flash the preloader:

```bash
fastboot flash preloader1 preloader_plato.bin
fastboot flash preloader2 preloader_plato.bin
```

If the commands fail, use the alternative partition names:

```bash
fastboot flash preloader_raw_a preloader_plato.bin
fastboot flash preloader_raw_b preloader_plato.bin
```

---

## Step 2: Flash Boot Images

```bash
fastboot flash boot boot.img
fastboot flash vendor_boot vendor_boot.img
fastboot reboot recovery
```

The device should now boot into Recovery.

---

## Step 3: Format Data

Inside Recovery:

1. Select **Wipe all data**
2. Confirm the operation

> [!IMPORTANT]
> Formatting data is required when installing the ROM.

---

## Step 4: Flash crDroid

From your computer:

```bash
adb sideload crDroidAndroid-16.0-20260607-plato-v12.11.zip
```

If Recovery prompts for a reboot at approximately **47%**, confirm it.

---

## Step 5: Flash GApps (Optional)

If you require Google services, flash the GApps package after installing the ROM:

```bash
adb sideload <gapps-package>.zip
```

No additional packages are required.

---

## Step 6: Reboot

Once installation is complete:

```bash
Reboot System
```

Complete the Android setup process and enjoy crDroid.
