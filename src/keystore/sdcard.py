from .core import KeyStoreError, PinError
from .flash import FlashKeyStore
import platform
from rng import get_random_bytes
from bitcoin import bip39
from gui.screens import Alert, Progress, Menu, MnemonicScreen, InputScreen, Prompt
import asyncio
from io import BytesIO
from helpers import tagged_hash
from binascii import hexlify
import os


class SDKeyStore(FlashKeyStore):
    """
    KeyStore that can store secrets
    in internal flash or on a removable SD card.
    SD card is required to unlock the device.
    Bitcoin key is encrypted with internal MCU secret,
    so the attacker needs to get both the device and the SD card.
    When correct PIN is entered the key can be loaded
    from SD card to the RAM of the MCU.
    """

    NAME = "Internal storage"
    NOTE = """Recovery phrase can be stored ecnrypted on the external SD card. Only this device will be able to read it."""
    # Button to go to storage menu
    # Menu is implemented in async storage_menu function
    storage_button = "Flash & SD card storage"
    load_button = "Load key"

    @property
    def sdpath(self):
        return platform.fpath("/sd")

    def fileprefix(self, path):
        if path is self.flashpath:
            return 'reckless'

        hexid = hexlify(tagged_hash("sdid", self.secret)[:4]).decode()
        return "specterdiy%s" % hexid

    async def get_keypath(self, title="Select media", only_if_exist=True, **kwargs):
        # enable / disable buttons
        enable_flash = (not only_if_exist) or platform.file_exists(self.flashpath)
        enable_sd = False
        if platform.is_sd_present():
            platform.mount_sdcard()
            enable_sd = (not only_if_exist) or platform.file_exists(self.sdpath)
            platform.unmount_sdcard()
        buttons = [
            (None, "Make your choice"),
            (self.flashpath, "Internal flash", enable_flash),
            (self.sdpath, "SD card", enable_sd),
        ]
        scr = Menu(buttons, title=title, last=(None,), **kwargs)
        res = await self.show(scr)
        return res

    async def save_mnemonic(self, filename, path=None):
        if self.is_locked:
            raise KeyStoreError("Keystore is locked")
        if self.mnemonic is None:
            raise KeyStoreError("Recovery phrase is not loaded")
        if path is None:
            path = await self.get_keypath(
                title="Where to save?", only_if_exist=False, note="Select media"
            )
            if path is None:
                return False
        if path == self.sdpath:
            if not platform.is_sd_present():
                raise KeyStoreError("SD card is not present")
            platform.mount_sdcard()

        if platform.file_exists("%s/%s.%s" % (path, self.fileprefix(path), filename)):
            scr = Prompt(
                "\n\nFile already exists: %s\n" % filename,
                "Would you like to overwrite this file?",
            )
            res = await self.show(scr)
            if res is False:
                return None

        self.save_aead("%s/%s.%s" % (path, self.fileprefix(path), filename), plaintext=self.mnemonic.encode(),
                       key=self.enc_secret)
        if path == self.sdpath:
            platform.unmount_sdcard()
        # check it's ok
        await self.load_mnemonic(path, "%s.%s" % (self.fileprefix(path), filename))
        return True

    @property
    def is_key_saved(self):
        flash_exists = platform.file_exists(self.flashpath)
        if not platform.is_sd_present():
            return flash_exists
        platform.mount_sdcard()
        sd_exists = platform.file_exists(self.sdpath)
        platform.unmount_sdcard()
        return sd_exists or flash_exists

    async def load_mnemonic(self, path=None, file=None):
        if self.is_locked:
            raise KeyStoreError("Keystore is locked")
        if path is None:
            path = await self.get_keypath(
                title="From where to load?", note="Select media"
            )
            if path is None:
                return False
        if path == self.sdpath:
            if not platform.is_sd_present():
                raise KeyStoreError("SD card is not present")
            platform.mount_sdcard()

        if file is None:
            file = await self.select_file(path, self.fileprefix(path))
            if file is None:
                return False

        print("loading "+ file)
        if not platform.file_exists("%s/%s" % (path, file)):
            raise KeyStoreError("Key is not saved")
        _, data = self.load_aead("%s/%s" % (path, file), self.enc_secret)

        if path == self.sdpath:
            platform.unmount_sdcard()
        self.set_mnemonic(data.decode(), "")
        return True

    async def select_file(self, path, prefix):
        files = []

        for file in os.ilistdir(path):
            if file[0].startswith(prefix):
                displayname = file[0].replace(prefix, "")
                if displayname is "":
                    displayname = "Default"
                else:
                    displayname = displayname[1:]  # strip first character
                files.append([file[0], displayname])

        if len(files) == 0:
            raise KeyStoreError("\n\nNo matching files found")

        files.sort()
        buttons = []
        for file in files:
            buttons += [(file[0], file[1])]

        fname = await self.show(Menu(buttons, title="Select a file", last=(None, "Cancel")))
        return fname

    async def delete_mnemonic(self, path=None):
        if path is None:
            path = await self.get_keypath(title="From where to delete?")
            if path is None:
                return False
        if path == self.sdpath:
            if not platform.is_sd_present():
                raise KeyStoreError("SD card is not present")
            platform.mount_sdcard()

        file = await self.select_file(path, self.fileprefix(path))
        if file is None:
            return False

        if not platform.file_exists("%s/%s" % (path, file)):
            raise KeyStoreError("File not found.")
        try:
            os.remove("%s/%s" % (path, file))
        except Exception as e:
            print(e)
            raise KeyStoreError("Failed to delete file '%s' from %s" % (file, path))
        finally:
            if path == self.sdpath:
                platform.unmount_sdcard()
            return True

    async def get_input(
            self,
            title="Enter a name for this seed",
            note="Naming your seeds allows you to store multiple.\n"
                 "Give each seed a unique name!",
            suggestion="",
    ):
        scr = InputScreen(title, note, suggestion)
        await self.show(scr)
        return scr.get_value()

    async def storage_menu(self):
        """Manage storage and display of the recovery phrase"""
        buttons = [
            # id, text
            (None, "Manage keys on SD card and internal flash"),
            (0, "Save key"),
            (1, "Load key"),
            (2, "Delete key"),
            (None, "Other"),
            (3, "Show recovery phrase"),
        ]

        # we stay in this menu until back is pressed
        while True:
            # wait for menu selection
            menuitem = await self.show(Menu(buttons, last=(255, None)))
            # process the menu button:
            # back button
            if menuitem == 255:
                return
            elif menuitem == 0:
                filename = await self.get_input()
                if filename is None:
                    return
                if filename is "":
                    await self.show(
                        Alert("Error!", "Please provide a valid name!\n\nYour file has NOT been saved.", button_text="OK")
                    )
                    return
                if await self.save_mnemonic(filename=filename):
                    await self.show(
                        Alert("Success!", "Your key is stored now.\n\nName: %s" % filename, button_text="OK")
                    )
            elif menuitem == 1:
                res = await self.load_mnemonic()
                print(res)
                if res:
                    await self.show(
                        Alert("Success!", "Your key is loaded.", button_text="OK")
                    )
            elif menuitem == 2:
                if await self.delete_mnemonic():
                    await self.show(
                        Alert("Success!", "Your key is deleted.", button_text="OK")
                    )
            elif menuitem == 3:
                await self.show_mnemonic()
