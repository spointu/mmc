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

import os
import urllib2
import logging
from xml.dom.minidom import parseString

from configobj import ConfigObj

from mmc.client import sync
from mmc.site import mmcconfdir


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
              "User-Agent": "Proxy:FusionInventory/Pulse2/GLPI",
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
            self._result.append("GlpiProxy: Impossible to sending inventory.")
            self._result.append('Getted response: "%s"' % str(exc))

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
            self._result.append("Unable to parse response from GLPI")


class ConfigReader :
    """Read and parse config files."""
    def __init__(self):
        self._base_ini = os.path.join(mmcconfdir, 
                                      "plugins", 
                                      "base.ini")
        self._glpi_ini = os.path.join(mmcconfdir, 
                                      "plugins", 
                                      "glpi.ini")
        self._pkg_server_ini = os.path.join(mmcconfdir, 
                                            "pulse2",  
                                            "package-server", 
                                            "package-server.ini")
 
    @classmethod
    def get_config(cls, inifile):
        """ 
        Get the configuration from config file
        
        @param inifile: path to config file
        @type inifile: string
    
        @return: configobj.ConfigObj instance 
        """
        logging.getLogger().debug("Load config file %s" % inifile)
        if not os.path.exists(inifile) :

            logging.getLogger().warn("Error while reading the config file:")
            logging.getLogger().warn("Not found.")

            raise IOError, "Config file '%s' not found" % inifile

        return ConfigObj(inifile)
 
    @property
    def base_config(self):
        """ 
        Get the configuration from base.ini
        @return: configobj.ConfigObj instance 
        """
        return self.get_config(self._base_ini)

    @property
    def glpi_config(self):
        """ 
        Get the configuration from glpi.ini
        @return: configobj.ConfigObj instance 
        """
        return self.get_config(self._glpi_ini)

    @property
    def pkg_server_config(self):
        """ 
        Get the configuration from package-server.ini
        @return: configobj.ConfigObj instance 
        """
        return self.get_config(self._pkg_server_ini)

# --- section of resolving the machine UUID on GLPI side ---

class MMCProxy :
    """ Provider to connect at mmc-agent """
    def __init__(self): 

        config = ConfigReader()
        
        self.glpi_config = config.glpi_config
        self.base_config = config.base_config
        self.pkg_server_config = config.pkg_server_config
        
        self._failure = False

        self._url = None
        self._proxy = None

        self._build_url()

        if not self._failure :
            password = self._get_ldap_password()

        if not self._failure :
            self._build_proxy(password)

    def _build_url(self):
        """ URL building for XML-RPC proxy """
       
        if not "mmc_agent" in self.pkg_server_config :
            logging.getLogger().warn("Error while reading the config file:")
            logging.getLogger().warn("Section 'mmc_agent' not exists")
            self._failure = True
            return

        mmc_section =  self.pkg_server_config["mmc_agent"]

        for option in ["username", "password", "host", "port"] :

            if option not in mmc_section :
                logging.getLogger().warn("Error while reading section 'mmc_agent':")
                logging.getLogger().warn("Option '%s' not exists" % option)

                self._failure = True
                return
 
        username = mmc_section["username"]
        host = mmc_section["host"]
        password = mmc_section["password"]
        port = mmc_section["port"]
        
        logging.getLogger().debug("Building the connection URL at mmc-agent") 

        self._url = 'https://%s:%s@%s:%s' % (username, password, host, port)

    @property
    def failure (self):
        """ 
        Failure flag to indicate the incorrect build of proxy 
        @returns: bool
        """
        return self._failure

    def _get_ldap_password(self):
        """ 
        Password for LDAP authentification 
        @return: string
        """
       
        if not "ldap" in self.base_config :
            logging.getLogger().warn("Error while reading the config file: Section 'ldap'")
            self._failure = True
            return
                                
        return self.base_config["ldap"]["password"]

    def _build_proxy (self, password):
        """ Builds the XML-RPC proxy to MMC agent. """
        try :
            self._proxy = sync.Proxy(self._url)

            logging.getLogger().debug("LDAP authentification")
            self._proxy.base.ldapAuth('root', password)
            logging.getLogger().debug("Create a mmc-agent proxy") 

        except Exception, err :
            logging.getLogger().error("Error while connecting to mmc-agent : %s" % err)
            self._failure = True
            

    @property
    def proxy (self):
        """
        Get the XML-RPC proxy to MMC agent.
        @return: mmc.client.sync.Proxy
        """
        return self._proxy
 

def resolveGlpiMachineUUIDByMAC (mac):
    """
    Get the machine UUID from GLPI DB layer.

    @param mac: MAC address of inventroied machine
    @type mac: str

    @return: UUID of machine
    @rtype: str
    """
    mmc = MMCProxy()
    if not mmc.failure :
        proxy = mmc.proxy
        uuid = proxy.glpi.getMachineUUIDByMacAddress(mac)
        return uuid
    return None
 

