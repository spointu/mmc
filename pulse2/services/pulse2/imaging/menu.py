# the menu timeout#
# (c) 2009-2010 Nicolas Rueff / Mandriva, http://www.mandriva.com/
#
# $Id$
#
# This file is part of Pulse 2, http://pulse2.mandriva.org
#
# Pulse 2 is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Pulse 2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pulse 2; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.

"""
    client menu handling classes
"""

import pulse2.utils
import re
import os.path
import os
import logging
import time

class ImagingMenu():
    """
        hold an imaging menu
    """

    mac = None # the client MAC Address
    config = None # the server configuration

    # menu items
    timeout = 0; # the menu timeout
    default_item = 0; # the menu default entry
    splashscreen = "/bootloader/bootloader.xpm"; # the menu splashscreen
    colors = { # menu colors
        'normal' : { 'fg': 7, 'bg': 1 },
        'highlight' : { 'fg': 15, 'bg': 3 }
    }
    keyboard = None # the menu keymap, None is C
    hidden = False # do we hide the menu ?

    menuitems = dict()

    additionnal = list() # additionnal keywords, put in the menu 'as is'

    replacements = list() # list of replacements to perform;
    # a replacement is using the following structure :
    # key 'from' : the PCRE to look for
    # key 'to' : the replacement to perform
    # key 'when' : when to perform the replacement (only 'global' for now)

    def __init__(self, config, macaddress):
        """
            Initialize this object.
            config is a ImagingConfig object
            macAddress is the client MAC Address
        """
        self.config = config
        assert pulse2.utils.isMacAddress(macaddress)
        self.mac = macaddress

    def _applyReplacement(self, string, condition):
        """
            Private func, to apply a remlpacement into a given string
        """
        output = string
        for replacement in self.replacements:
            for (f, t, w) in replacement:
                if w == condition :
                    output = re.sub(f, t, output)
        return output

    def write(self):
        """
            write the client menu
        """
        # takes global items, one by one

        buffer  = '# Auto-generated by Pulse 2 Imaging Server on %s \n\n' % time.asctime()

        buffer += 'timeout %s\n' % self.timeout
        buffer += 'default %s\n' % self.default_item
        buffer += self._applyReplacement('splashscreen %s\n' % self.splashscreen)
        buffer += 'color %d/%d %d/%d\n' % (
            self.colors['normal']['fg'],
            self.colors['normal']['bg'],
            self.colors['highlight']['fg'],
            self.colors['highlight']['bg']
        )
        if self.keyboard == 'fr':
            buffer += 'keybfr\n'

        if self.hidden:
            buffer += 'hide\n'

        buffer += '\n'.join(self.additionnal)

        # then write items
        for menuitem in self.menuitems:
            output = menuitem.getEntry()
            buffer += '\n'
            buffer += self._applyReplacement(output)

        filename = os.path.join(self.config.bootmenus_folder, pulse2.utils.reduceMacAddr(self.mac))
        backup = os.tempnam(self.config.bootmenus_folder, pulse2.utils.reduceMacAddr(self.mac))

        try:
            os.rename(filename, backup)
            file = open(filename, 'w+')
            file.write(buffer)
            file.close()
            os.remove(backup)
        except OsError, e:
            logging.getLogger.error("While writing boot menu for %s : %s" % (self.mac, e))
            return False

        return True

    def read(self):
        """
            read the client menu
            don't expect the summoned structure to be usable :
            menu.lst <-> menu.conf if far for beeing a bijection
        """
        pass

    def setTimeout(self, value):
        """
            set the default timeout
        """
        self.timeout = value

    def getTimeout(self):
        """
            get the default timeout
        """
        return self.timeout

    def setDefaultItem(self, value):
        """
            set the default item number
        """
        self.default_item = value

    def getDefaultItem(self):
        """
            get the default item number
        """
        return self.default_item

    def addEntry(self, entry, position = None):
        """
            add the ImagingEntry entry to our menu
            if position is None, add it at the first slot available
        """
        pass

    def removeEntry(self, position = None):
        """
            remove the entrey at position
            if position is None, remove the last image
        """
        pass

    def setKeyboard(self, map = None):
        """
            set keyboard map
            if map is none, do not set keymap
        """
        if map in ['fr']:
            self.keyboard = map

    def hideMenu(self):
        """
            Do hide the menu
        """
        self.hidden = True

    def showMenu(self):
        """
            Do show the menu
        """
        self.hidden = False

class ImagingMenuItem():
    """
        hold an imaging menu item
    """

    title = None # the item title
    desc = None # the item desc
    uri = None # the uri to call

    def __init__(self, title, desc = None):
        """
            Initialize this object.
            title is mandatory, desc optionnal
            (in this case, desc takes the value of title)
        """
        assert type(title) == str
        self.title = title
        assert type(desc) in [str, NoneType]
        self.desc = desc

    def setUri(self, uri):
        """
            set the bootservice URI
        """
        assert type(uri) == str
        self.uri = uri

    def getEntry(self):
        """
            return the entry, in a grub compatible format
        """
        buffer  = ''
        buffer += 'title %s\n' % self.title
        if self.desc:
            buffer += 'desc %s\n' % self.desc
        if self.uri:
            buffer += self.uri
        return buffer

