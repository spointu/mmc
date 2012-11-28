# -*- coding: utf-8; -*-
#
# (c) 2012 Mandriva, http://www.mandriva.com
#
# This file is part of Mandriva Management Console (MMC).
#
# MMC is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# MMC is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with MMC; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
Inventory proxy Pulse -> GLPI using Fusion Inventory plugin.
"""

import urllib2
from xml.dom.minidom import parseString

class _ErrorHandler :
    """ 
    Abstract class to implement error handling of XML responses from GLPI.
    """
    _message = []

    def __init__(self, response):
        """
        @param response: XML response from Fusion Inventory plugin on GLPI
        @type response: string
        """
        response = response.strip()
        self._parse(response)

    def _parse (self, response):
        """ Parsing the XML response """
        raise NotImplemented
       
    def __iter__(self):
        for msg in self._message :
            yield msg

    def __len__(self):
        return len(self._message)


class FusionErrorHandler (_ErrorHandler):
    """
    Response parsing on check of occurence an error element on XML format.
    """
    # An example of error message :

    # <?xml version="1.0" encoding="UTF-8"?>
    # <REPLY>
    #     <ERROR>XML not well formed!</ERROR>
    # </REPLY>

    def _parse (self, response):
        try:
            dom = parseString(response)
            for node in dom.getElementsByTagName('ERROR'):
                if node.nodeType == node.ELEMENT_NODE :
                    self._message.append(node.firstChild.nodeValue)
        except Exception, exc :
            self._message.append("Unable to parse response from GLPI")
            self._message.append('Getted response: "%s"' % str(exc))



class GlpiProxy :
    """ Sending inventories to GLPI with an error handling."""

    HEADER = {"Pragma": "no-cache",
              "User-Agent": "wwFusionInventory-Agent_v2.2.3",
              "Content-Type": "application/x-compress",
             }

    def __init__(self, url, ErrorHandler=FusionErrorHandler):
        """
        @param url: URL to sending Fusion Inventory XML
        @type url: string

        @param ErrorHandler: parser for the feedback receieved from GLPI
        @type ErrorHandler: _ErrorHandler
        """
        self.url = url

        if ErrorHandler and issubclass(ErrorHandler, _ErrorHandler):
            self.ErrorHandler = ErrorHandler

        self._result = []

    def send(self, content):
        """
        Sending the inventory to Fusion Inventory plugin by the POST method.

        @param content: inventory on XML format
        @type content: string
        """
        try:
            request = urllib2.Request(self.url, content, self.HEADER)
            response = urllib2.urlopen(request)

        except Exception, exc:
            self._result += "GlpiProxy: Impossible to sending inventory."
            self._result += 'Getted response: "%s"' % str(exc)

            return

        # parsing response
        if self.ErrorHandler :
            xml_response = response.read()
            self._result += self.ErrorHandler(xml_response)

    @property
    def result(self):
        """ 
        @returns: list of error messages
        @rtype: list
        """
        if self.ErrorHandler :
            return self._result
        else :
            self._result += "Unable to parse response from GLPI"

        
        